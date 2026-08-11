"""Writing dashboards, templates and pins. One implementation, two callers.

Everything in this module is reached from both `api/dashboards.py` and
`services/dashboards/tools.py`, and that is the point: the agent authors
dashboards through exactly the code path a human's request travels, so a rule
that holds over HTTP holds for the model too. The alternative — a tool that
builds its own `Dashboard` row — is how a subsystem grows two spellings of
"valid" and starts refusing over one of them.

Nothing here raises `HTTPException`. The route maps these exceptions to status
codes and the tool maps them to sentences.
"""
from __future__ import annotations

import json
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ...models import Dashboard, DashboardPin, DashboardTemplate, new_id
from ...schemas import (
    DashboardColumnRequirement,
    DashboardOut,
    DashboardPinOut,
    DashboardPinUpdate,
    DashboardSpec,
    DashboardTemplateOut,
    DatasetColumn,
)
from ..analytics import current_dataset_version, execute_dataset_query
from ..workflows.validate import CompileError, CompileReport
from .binding import (
    DashboardBindError,
    bind_report,
    effective_bindings,
    rebind_spec,
    result_fields,
    template_report,
)

#: A 12-column grid, and a tile that is half of it. Two dashboards sit side by
#: side on a laptop at this size, which is what "pin a couple of things" should
#: look like before anyone has dragged anything.
GRID_COLUMNS = 12
DEFAULT_TILE_WIDTH = 6
DEFAULT_TILE_HEIGHT = 4


class DashboardNameTaken(RuntimeError):
    """A workspace already has something by this name."""


def dashboard_out(dashboard: Dashboard) -> DashboardOut:
    return DashboardOut(
        id=dashboard.id,
        name=dashboard.name,
        description=dashboard.description,
        dataset_id=dashboard.dataset_id,
        spec=DashboardSpec.model_validate(json.loads(dashboard.spec_json)),
        template_id=dashboard.template_id or "",
        bindings=json.loads(dashboard.bindings_json or "{}"),
        created_at=dashboard.created_at,
        updated_at=dashboard.updated_at,
    )


def template_out(template: DashboardTemplate) -> DashboardTemplateOut:
    return DashboardTemplateOut(
        id=template.id,
        name=template.name,
        description=template.description,
        required_columns=[
            DashboardColumnRequirement.model_validate(item)
            for item in json.loads(template.required_columns_json)
        ],
        spec=DashboardSpec.model_validate(json.loads(template.spec_json)),
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


def template_requirements(
    template: DashboardTemplate,
) -> List[DashboardColumnRequirement]:
    return [
        DashboardColumnRequirement.model_validate(item)
        for item in json.loads(template.required_columns_json)
    ]


def pin_out(pin: DashboardPin, dashboard: Dashboard) -> DashboardPinOut:
    return DashboardPinOut(
        dashboard=dashboard_out(dashboard),
        grid_x=pin.grid_x,
        grid_y=pin.grid_y,
        grid_w=pin.grid_w,
        grid_h=pin.grid_h,
        pinned_at=pin.created_at,
    )


# --------------------------------------------------------------------------
# Dashboards


def create_dashboard(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    name: str,
    description: str,
    dataset_id: str,
    spec: DashboardSpec,
    template_id: Optional[str] = None,
    bindings: Optional[Mapping[str, str]] = None,
) -> Dashboard:
    """Validate a spec against real data, then save it.

    The query runs before the row is written. A dashboard that cannot execute is
    not a dashboard — it is a promise to fail later, in front of whoever opens
    it — so the cost of one query at authoring time buys the guarantee that
    every saved dashboard has answered at least once.

    Raises `AnalyticsValidationError` when the dataset or the query is wrong,
    `DashboardBindError` when the visualization reads a column the query does
    not return, and `DashboardNameTaken` on a collision.
    """
    name = name.strip()
    existing = db.scalar(
        select(Dashboard).where(
            Dashboard.workspace_id == workspace_id,
            Dashboard.name == name,
        )
    )
    if existing is not None:
        raise DashboardNameTaken(name)
    result = execute_dataset_query(
        db, workspace_id=workspace_id, dataset_id=dataset_id, query=spec.query
    )
    columns = result.columns
    unknown = {
        field: where for field, where in result_fields(spec).items() if field not in columns
    }
    if unknown:
        report = CompileReport(
            errors=[
                CompileError(
                    "spec_field_unknown",
                    f"{where} names “{field}”, which this query does not return; "
                    "it returns " + ", ".join(columns),
                    field,
                )
                for field, where in sorted(unknown.items())
            ]
        )
        raise DashboardBindError(report)
    dashboard = Dashboard(
        id=new_id(),
        workspace_id=workspace_id,
        created_by=user_id,
        dataset_id=dataset_id,
        name=name,
        description=description.strip(),
        spec_json=spec.model_dump_json(),
        template_id=template_id,
        bindings_json=json.dumps(dict(bindings or {}), sort_keys=True),
    )
    db.add(dashboard)
    db.flush()
    return dashboard


def delete_dashboard(db: Session, dashboard: Dashboard) -> None:
    """Remove a dashboard and every home screen it was on.

    Pins are deleted first and explicitly. Leaving them would put a tile on
    somebody's screen with nothing behind it, and the person who deleted the
    dashboard is rarely the person who pinned it.
    """
    db.execute(delete(DashboardPin).where(DashboardPin.dashboard_id == dashboard.id))
    db.delete(dashboard)


def dataset_columns(
    db: Session, *, workspace_id: str, dataset_id: str
) -> List[DatasetColumn]:
    """The current schema of a dataset. Raises `AnalyticsValidationError` when
    the dataset is not this workspace's, which is what keeps a bind from
    learning the column names of another tenant's data."""
    _dataset, version = current_dataset_version(
        db, workspace_id=workspace_id, dataset_id=dataset_id
    )
    return [
        DatasetColumn.model_validate(item) for item in json.loads(version.schema_json)
    ]


# --------------------------------------------------------------------------
# Templates


def create_template(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    name: str,
    description: str,
    required: Sequence[DashboardColumnRequirement],
    spec: DashboardSpec,
) -> DashboardTemplate:
    """Save a definition, once it is internally consistent.

    Raises `DashboardBindError` carrying every inconsistency, so a template that
    reads a column it never required is refused at authoring time rather than at
    every future bind.
    """
    report = template_report(required, spec)
    if not report.ok:
        raise DashboardBindError(report)
    name = name.strip()
    existing = db.scalar(
        select(DashboardTemplate).where(
            DashboardTemplate.workspace_id == workspace_id,
            DashboardTemplate.name == name,
        )
    )
    if existing is not None:
        raise DashboardNameTaken(name)
    template = DashboardTemplate(
        id=new_id(),
        workspace_id=workspace_id,
        created_by=user_id,
        name=name,
        description=description.strip(),
        required_columns_json=json.dumps(
            [item.model_dump() for item in required], sort_keys=True
        ),
        spec_json=spec.model_dump_json(),
    )
    db.add(template)
    db.flush()
    return template


def delete_template(db: Session, template: DashboardTemplate) -> None:
    """Drop a definition, leaving the dashboards it produced alone.

    A bound dashboard is a finished thing that runs on its own; deleting the
    definition it came from should not take a working tile off anyone's screen.
    The link is cleared rather than dangling so nothing later resolves a
    template id that no longer names a row.
    """
    db.execute(
        update(Dashboard)
        .where(Dashboard.template_id == template.id)
        .values(template_id=None)
    )
    db.delete(template)


def bind_template(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    template: DashboardTemplate,
    dataset_id: str,
    name: str,
    description: str,
    bindings: Mapping[str, str],
) -> Dashboard:
    """Point a template at a dataset, or refuse with every reason it cannot be.

    On success the result is an ordinary `Dashboard`: the template's spec with
    declared names rewritten to the dataset's own columns. Nothing downstream —
    running it, pinning it, deleting it — needs to know it came from a template.
    """
    required = template_requirements(template)
    report = bind_report(
        required,
        bindings=bindings,
        columns=dataset_columns(db, workspace_id=workspace_id, dataset_id=dataset_id),
    )
    if not report.ok:
        raise DashboardBindError(report)
    applied = effective_bindings(required, bindings)
    spec = rebind_spec(
        DashboardSpec.model_validate(json.loads(template.spec_json)), applied
    )
    return create_dashboard(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        name=name,
        description=description or template.description,
        dataset_id=dataset_id,
        spec=spec,
        template_id=template.id,
        bindings=applied,
    )


# --------------------------------------------------------------------------
# Pins — one person's home screen


def list_pins(
    db: Session, *, workspace_id: str, user_id: str
) -> List[Tuple[DashboardPin, Dashboard]]:
    """This user's tiles, in reading order: top row first, then left to right."""
    rows = db.execute(
        select(DashboardPin, Dashboard)
        .join(Dashboard, Dashboard.id == DashboardPin.dashboard_id)
        # Both sides are filtered. The pin's workspace is the one that scopes the
        # query, and the dashboard's is asserted rather than assumed: the FK
        # points at `dashboards.id`, not at (workspace_id, id), so nothing in
        # the schema alone stops a pin from naming a row in another tenant.
        .where(
            DashboardPin.workspace_id == workspace_id,
            DashboardPin.user_id == user_id,
            Dashboard.workspace_id == workspace_id,
        )
        .order_by(DashboardPin.grid_y, DashboardPin.grid_x, DashboardPin.created_at)
    ).all()
    return [(row[0], row[1]) for row in rows]


def find_pin(
    db: Session, *, workspace_id: str, user_id: str, dashboard_id: str
) -> Optional[DashboardPin]:
    return db.scalar(
        select(DashboardPin).where(
            DashboardPin.workspace_id == workspace_id,
            DashboardPin.user_id == user_id,
            DashboardPin.dashboard_id == dashboard_id,
        )
    )


def pin_dashboard(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    dashboard: Dashboard,
    placement: DashboardPinUpdate,
) -> DashboardPin:
    """Put a dashboard on this user's home screen, or move it if it is there.

    An unplaced pin lands below everything already pinned rather than at the
    origin, because the origin is occupied and a new tile that covers the one
    you were looking at reads as a bug.
    """
    pin = find_pin(
        db, workspace_id=workspace_id, user_id=user_id, dashboard_id=dashboard.id
    )
    if pin is None:
        pin = DashboardPin(
            id=new_id(),
            workspace_id=workspace_id,
            user_id=user_id,
            dashboard_id=dashboard.id,
            grid_x=0,
            grid_y=_next_free_row(db, workspace_id=workspace_id, user_id=user_id),
            grid_w=DEFAULT_TILE_WIDTH,
            grid_h=DEFAULT_TILE_HEIGHT,
        )
        db.add(pin)
    if placement.grid_x is not None:
        pin.grid_x = placement.grid_x
    if placement.grid_y is not None:
        pin.grid_y = placement.grid_y
    if placement.grid_w is not None:
        pin.grid_w = placement.grid_w
    if placement.grid_h is not None:
        pin.grid_h = placement.grid_h
    _clamp(pin)
    db.flush()
    return pin


def _next_free_row(db: Session, *, workspace_id: str, user_id: str) -> int:
    rows = db.scalars(
        select(DashboardPin).where(
            DashboardPin.workspace_id == workspace_id,
            DashboardPin.user_id == user_id,
        )
    )
    return max((pin.grid_y + pin.grid_h for pin in rows), default=0)


def _clamp(pin: DashboardPin) -> None:
    """Keep a tile inside the grid it is placed on.

    Width is bounded by the schema, but x + w is not: a 6-wide tile dropped at
    column 9 would extend past the last column and either overflow the page or
    silently reflow, depending on which grid renders it. Trimming the width
    keeps the tile where the user put it.
    """
    pin.grid_x = max(0, min(pin.grid_x, GRID_COLUMNS - 1))
    pin.grid_w = max(1, min(pin.grid_w, GRID_COLUMNS - pin.grid_x))


def apply_layout(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    placements: Mapping[str, Tuple[int, int, int, int]],
) -> List[DashboardPin]:
    """Move several tiles at once. Every id must already be pinned by this user.

    Raises `KeyError` naming the first dashboard that is not, which the caller
    turns into a 404: a layout naming a tile the user does not have is either a
    stale screen or another user's arrangement, and applying the rest of it
    would half-save a home screen.
    """
    pins = {
        pin.dashboard_id: pin
        for pin, _dashboard in list_pins(db, workspace_id=workspace_id, user_id=user_id)
    }
    missing = [name for name in placements if name not in pins]
    if missing:
        raise KeyError(sorted(missing)[0])
    for dashboard_id, (grid_x, grid_y, grid_w, grid_h) in placements.items():
        pin = pins[dashboard_id]
        pin.grid_x, pin.grid_y, pin.grid_w, pin.grid_h = grid_x, grid_y, grid_w, grid_h
        _clamp(pin)
    db.flush()
    return [pins[dashboard_id] for dashboard_id in placements]


def dashboards_by_id(
    db: Session, *, workspace_id: str, dashboard_ids: Sequence[str]
) -> Dict[str, Dashboard]:
    if not dashboard_ids:
        return {}
    rows = db.scalars(
        select(Dashboard).where(
            Dashboard.workspace_id == workspace_id,
            Dashboard.id.in_(list(dashboard_ids)),
        )
    )
    return {dashboard.id: dashboard for dashboard in rows}
