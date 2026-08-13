"""Dashboards, the definitions behind them, and one person's home screen.

These used to live in `api/analytics.py`, beside the datasets they read. They
moved because a reachability audit found the whole subsystem stranded: routes
existed to list, create and run dashboards, and nothing in the product could
produce one. Splitting them out is half the fix — the other half is
`services/dashboards/tools.py`, which lets the agent author them, and the two
belong in one place now that "a dashboard" is a thing the product has rather
than a table it happens to own.

Two rules run through everything below.

*Workspace-scoped, always.* Dashboards and templates belong to a workspace and
every query says so.

*Pins are per user.* A workspace shares its dashboards; it does not share your
home screen. Every pin route filters on `actor.user_id` as well, and the pin
table's uniqueness is (user, dashboard) for the same reason — see the model.
"""
from __future__ import annotations

import json
from typing import Dict, List, Tuple

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Dashboard, DashboardTemplate
from ..schemas import (
    DashboardCreate,
    DashboardLayoutUpdate,
    DashboardOut,
    DashboardPinOut,
    DashboardPinUpdate,
    DashboardRunOut,
    DashboardSpec,
    DashboardTemplateBind,
    DashboardTemplateCreate,
    DashboardTemplateOut,
)
from ..services import conversations, subjects
from ..services.analytics import AnalyticsValidationError, execute_dataset_query
from ..services.audit import record_audit
from ..services.dashboards import store
from ..services.dashboards.binding import DashboardBindError
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api", tags=["dashboards"])


def _analytics_failure(exc: AnalyticsValidationError) -> HTTPException:
    """404 for "no such dataset", 422 for "that query is wrong".

    The split matters to the isolation sweep as much as to a caller: naming
    another tenant's dataset must read as *absent*, not as a rejected query.
    """
    status = 404 if str(exc) == "Dataset not found" else 422
    return HTTPException(status_code=status, detail=str(exc))


def _bind_failure(exc: DashboardBindError) -> HTTPException:
    """422 with every finding, not the first.

    Byte-for-byte the shape `api/workflows.py` returns for a graph that will not
    compile, because it is the same kind of mistake: a declared contract the
    supplied thing does not satisfy. One error shape means one renderer and one
    repair prompt.
    """
    return HTTPException(status_code=422, detail=exc.report.as_dict())


def _dashboard(db: Session, actor: Actor, dashboard_id: str) -> Dashboard:
    dashboard = db.scalar(
        select(Dashboard).where(
            Dashboard.id == dashboard_id,
            Dashboard.workspace_id == actor.workspace_id,
        )
    )
    if dashboard is None:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return dashboard


def _template(db: Session, actor: Actor, template_id: str) -> DashboardTemplate:
    template = db.scalar(
        select(DashboardTemplate).where(
            DashboardTemplate.id == template_id,
            DashboardTemplate.workspace_id == actor.workspace_id,
        )
    )
    if template is None:
        raise HTTPException(status_code=404, detail="Dashboard template not found")
    return template


# --------------------------------------------------------------------------
# Dashboards


@router.get("/dashboards", response_model=List[DashboardOut])
def list_dashboards(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[DashboardOut]:
    dashboards = db.scalars(
        select(Dashboard)
        .where(Dashboard.workspace_id == actor.workspace_id)
        .order_by(Dashboard.updated_at.desc())
    )
    return [store.dashboard_out(item) for item in dashboards]


@router.post("/dashboards", response_model=DashboardOut, status_code=201)
def create_dashboard(
    payload: DashboardCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DashboardOut:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="dashboard.create",
        key=key,
    )
    if replay:
        dashboard = db.scalar(
            select(Dashboard).where(
                Dashboard.id == replay.resource_id,
                Dashboard.workspace_id == actor.workspace_id,
            )
        )
        if dashboard is None:
            raise replayed_resource_gone()
        return store.dashboard_out(dashboard)
    try:
        dashboard = store.create_dashboard(
            db,
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            name=payload.name,
            description=payload.description,
            dataset_id=payload.dataset_id,
            spec=payload.spec,
        )
    except store.DashboardNameTaken as exc:
        raise HTTPException(
            status_code=409, detail="Dashboard name already exists"
        ) from exc
    except AnalyticsValidationError as exc:
        raise _analytics_failure(exc) from exc
    except DashboardBindError as exc:
        raise _bind_failure(exc) from exc
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="dashboard.create",
        key=key,
        resource_id=dashboard.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="dashboard.created",
        resource_type="dashboard",
        resource_id=dashboard.id,
        detail={"name": dashboard.name, "dataset_id": dashboard.dataset_id},
    )
    db.commit()
    db.refresh(dashboard)
    return store.dashboard_out(dashboard)


@router.post("/dashboards/{dashboard_id}/run", response_model=DashboardRunOut)
def run_dashboard(
    dashboard_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DashboardRunOut:
    dashboard = _dashboard(db, actor, dashboard_id)
    spec = DashboardSpec.model_validate(json.loads(dashboard.spec_json))
    try:
        result = execute_dataset_query(
            db,
            workspace_id=actor.workspace_id,
            dataset_id=dashboard.dataset_id,
            query=spec.query,
        )
    except AnalyticsValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return DashboardRunOut(dashboard=store.dashboard_out(dashboard), result=result)


@router.delete("/dashboards/{dashboard_id}", status_code=204)
def delete_dashboard(
    dashboard_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> Response:
    dashboard = _dashboard(db, actor, dashboard_id)
    # The side-panel thread goes with it, exactly as a document's does: it was
    # only ever about this dashboard's spec, and the Chat rail filters scoped
    # threads out, so an orphan here is unreachable rather than merely untidy.
    conversations.purge_for_subject(
        db,
        workspace_id=actor.workspace_id,
        subject_kind=subjects.DASHBOARD,
        subject_id=dashboard.id,
    )
    store.delete_dashboard(db, dashboard)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="dashboard.deleted",
        resource_type="dashboard",
        resource_id=dashboard_id,
        detail={"name": dashboard.name},
    )
    db.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------
# Templates


@router.get("/dashboard-templates", response_model=List[DashboardTemplateOut])
def list_dashboard_templates(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[DashboardTemplateOut]:
    templates = db.scalars(
        select(DashboardTemplate)
        .where(DashboardTemplate.workspace_id == actor.workspace_id)
        .order_by(DashboardTemplate.updated_at.desc())
    )
    return [store.template_out(item) for item in templates]


@router.post(
    "/dashboard-templates", response_model=DashboardTemplateOut, status_code=201
)
def create_dashboard_template(
    payload: DashboardTemplateCreate,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DashboardTemplateOut:
    try:
        template = store.create_template(
            db,
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            name=payload.name,
            description=payload.description,
            required=payload.required_columns,
            spec=payload.spec,
        )
    except store.DashboardNameTaken as exc:
        raise HTTPException(
            status_code=409, detail="Dashboard template name already exists"
        ) from exc
    except DashboardBindError as exc:
        raise _bind_failure(exc) from exc
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="dashboard_template.created",
        resource_type="dashboard_template",
        resource_id=template.id,
        detail={"name": template.name},
    )
    db.commit()
    db.refresh(template)
    return store.template_out(template)


@router.delete("/dashboard-templates/{template_id}", status_code=204)
def delete_dashboard_template(
    template_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> Response:
    template = _template(db, actor, template_id)
    store.delete_template(db, template)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="dashboard_template.deleted",
        resource_type="dashboard_template",
        resource_id=template_id,
        detail={"name": template.name},
    )
    db.commit()
    return Response(status_code=204)


@router.post(
    "/dashboard-templates/{template_id}/bind",
    response_model=DashboardOut,
    status_code=201,
)
def bind_dashboard_template(
    template_id: str,
    payload: DashboardTemplateBind,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DashboardOut:
    """Turn a definition and a dataset into a dashboard, or say why not.

    The 422 here is the whole feature. A binding that does not satisfy the
    template's declared shape is refused now, in front of whoever asked for it,
    rather than on the morning somebody opens the chart and reads an empty one.
    """
    template = _template(db, actor, template_id)
    try:
        dashboard = store.bind_template(
            db,
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            template=template,
            dataset_id=payload.dataset_id,
            name=payload.name,
            description=payload.description,
            bindings=payload.column_bindings,
        )
    except store.DashboardNameTaken as exc:
        raise HTTPException(
            status_code=409, detail="Dashboard name already exists"
        ) from exc
    except AnalyticsValidationError as exc:
        raise _analytics_failure(exc) from exc
    except DashboardBindError as exc:
        raise _bind_failure(exc) from exc
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="dashboard.bound",
        resource_type="dashboard",
        resource_id=dashboard.id,
        detail={
            "name": dashboard.name,
            "template_id": template.id,
            "dataset_id": payload.dataset_id,
        },
    )
    db.commit()
    db.refresh(dashboard)
    return store.dashboard_out(dashboard)


# --------------------------------------------------------------------------
# Pins — the caller's own home screen, and nobody else's


@router.get("/dashboard-pins", response_model=List[DashboardPinOut])
def list_dashboard_pins(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[DashboardPinOut]:
    return [
        store.pin_out(pin, dashboard)
        for pin, dashboard in store.list_pins(
            db, workspace_id=actor.workspace_id, user_id=actor.user_id
        )
    ]


@router.put("/dashboards/{dashboard_id}/pin", response_model=DashboardPinOut)
def pin_dashboard(
    dashboard_id: str,
    payload: DashboardPinUpdate,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DashboardPinOut:
    dashboard = _dashboard(db, actor, dashboard_id)
    pin = store.pin_dashboard(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        dashboard=dashboard,
        placement=payload,
    )
    db.commit()
    db.refresh(pin)
    return store.pin_out(pin, dashboard)


@router.delete("/dashboards/{dashboard_id}/pin", status_code=204)
def unpin_dashboard(
    dashboard_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> Response:
    pin = store.find_pin(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        dashboard_id=dashboard_id,
    )
    if pin is None:
        raise HTTPException(status_code=404, detail="Dashboard is not pinned")
    db.delete(pin)
    db.commit()
    return Response(status_code=204)


@router.put("/dashboard-pins/layout", response_model=List[DashboardPinOut])
def save_dashboard_layout(
    payload: DashboardLayoutUpdate,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[DashboardPinOut]:
    """Save the whole grid in one write. See `DashboardLayoutUpdate`."""
    placements: Dict[str, Tuple[int, int, int, int]] = {
        tile.dashboard_id: (tile.grid_x, tile.grid_y, tile.grid_w, tile.grid_h)
        for tile in payload.tiles
    }
    try:
        store.apply_layout(
            db,
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            placements=placements,
        )
    except KeyError as exc:
        # A layout naming a dashboard this user has not pinned. 404 rather than
        # 422: from the caller's side the tile they are moving does not exist,
        # and saying anything more would tell them whether it exists for
        # somebody else.
        raise HTTPException(status_code=404, detail="Dashboard is not pinned") from exc
    db.commit()
    return list_dashboard_pins(actor=actor, db=db)
