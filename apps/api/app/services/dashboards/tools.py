"""The agent tools that make dashboards exist.

Until these, nothing could create one. The models, the typed query engine and
the routes were all in place and there was no path from a person asking for a
chart to a chart, which is the definition of an unreachable subsystem.

The line drawn here is the product's: **the agent authors, the user curates.**
Every tool below writes a *definition* — a dashboard, a template, a binding —
and none of them pins anything. Which dashboards sit on your home screen, and
where, is the one part of this that is nobody's decision but yours; a model that
could rearrange it would be answering a question you did not ask.

All the write tools are `read_only=False`, so they inherit the standing approval
gate rather than carrying an opinion about it, and all of them preview: what the
user approves is a sentence describing the chart, not a JSON blob.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Dashboard, DashboardTemplate
from ...schemas import DashboardColumnRequirement, DashboardSpec
from ..analytics import AnalyticsValidationError
from ..llm_tools import ToolContext, ToolResult, ToolSpec
from . import store
from .binding import DashboardBindError, bind_report, effective_bindings, rebind_spec

#: The spec a model writes, as JSON Schema. Deliberately the same vocabulary as
#: `query_dataset`'s `query` argument: a model that has already explored a
#: dataset with that tool can turn the query it liked into a dashboard by
#: copying it, which is the whole authoring path in one step.
_SPEC_PROPERTIES: Dict[str, Any] = {
    "visualization": {
        "type": "string",
        "enum": ["table", "bar", "line", "donut"],
        "description": "How to draw the answer. Default 'table'.",
    },
    "query": {
        "type": "object",
        "description": (
            "A typed query, same shape as query_dataset: filters, group_by, "
            "metrics (count/sum/avg/min/max with a label), order_by, limit."
        ),
    },
    "x_field": {
        "type": "string",
        "description": (
            "The category axis. Must be a column the query returns — the "
            "group_by, or a metric's label."
        ),
    },
    "y_fields": {
        "type": "array",
        "items": {"type": "string"},
        "description": "Value series. Each must be a column the query returns.",
    },
}


def _text(args: Mapping[str, Any], key: str, default: str = "") -> str:
    value = args.get(key)
    return str(value).strip() if isinstance(value, (str, int, float)) else default


def _spec(args: Mapping[str, Any]) -> DashboardSpec:
    raw = {key: args.get(key) for key in _SPEC_PROPERTIES if args.get(key) is not None}
    return DashboardSpec.model_validate(raw)


def _requirements(args: Mapping[str, Any]) -> List[DashboardColumnRequirement]:
    raw = args.get("required_columns")
    if not isinstance(raw, list):
        raise ValueError("required_columns must be a list of {name, type} objects")
    return [DashboardColumnRequirement.model_validate(item) for item in raw]


def _bindings(args: Mapping[str, Any]) -> Dict[str, str]:
    raw = args.get("column_bindings") or {}
    if not isinstance(raw, dict):
        raise ValueError("column_bindings must be an object of declared -> column")
    return {str(key): str(value) for key, value in raw.items()}


def _describe(spec: DashboardSpec) -> str:
    """One sentence for what a spec draws, for previews and confirmations."""
    metrics = ", ".join(metric.label for metric in spec.query.metrics)
    if spec.query.group_by and metrics:
        body = f"{metrics} by {spec.query.group_by}"
    elif spec.query.group_by:
        body = f"row count by {spec.query.group_by}"
    elif metrics:
        body = metrics
    else:
        body = f"up to {spec.query.limit} rows"
    filters = "; ".join(
        f"{item.field} {item.operator} {item.value}" for item in spec.query.filters
    )
    return f"a {spec.visualization} of {body}" + (f", where {filters}" if filters else "")


def _find_dataset_name(db: Session, *, workspace_id: str, dataset_id: str) -> str:
    from ...models import Dataset

    dataset = db.scalar(
        select(Dataset).where(
            Dataset.id == dataset_id, Dataset.workspace_id == workspace_id
        )
    )
    return dataset.name if dataset else dataset_id


def _find_template(
    db: Session, context: ToolContext, args: Mapping[str, Any]
) -> Optional[DashboardTemplate]:
    """By id, or by name — a model that just listed templates has the name."""
    template_id = _text(args, "template_id")
    if template_id:
        return db.scalar(
            select(DashboardTemplate).where(
                DashboardTemplate.id == template_id,
                DashboardTemplate.workspace_id == context.workspace_id,
            )
        )
    name = _text(args, "template")
    if not name:
        return None
    return db.scalar(
        select(DashboardTemplate).where(
            DashboardTemplate.workspace_id == context.workspace_id,
            DashboardTemplate.name == name,
        )
    )


# --------------------------------------------------------------------------
# Reads


def _list_dashboards(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> ToolResult:
    dashboards = db.scalars(
        select(Dashboard)
        .where(Dashboard.workspace_id == context.workspace_id)
        .order_by(Dashboard.updated_at.desc())
    )
    out = []
    for dashboard in dashboards:
        spec = DashboardSpec.model_validate(json.loads(dashboard.spec_json))
        out.append(
            {
                "id": dashboard.id,
                "name": dashboard.name,
                "description": dashboard.description,
                "dataset_id": dashboard.dataset_id,
                "shows": _describe(spec),
                "from_template": dashboard.template_id or "",
            }
        )
    if not out:
        return ToolResult(content="This workspace has no dashboards yet.")
    return ToolResult(content=json.dumps({"dashboards": out}))


def _list_templates(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> ToolResult:
    templates = db.scalars(
        select(DashboardTemplate)
        .where(DashboardTemplate.workspace_id == context.workspace_id)
        .order_by(DashboardTemplate.updated_at.desc())
    )
    out = []
    for template in templates:
        spec = DashboardSpec.model_validate(json.loads(template.spec_json))
        out.append(
            {
                "id": template.id,
                "name": template.name,
                "description": template.description,
                "shows": _describe(spec),
                "requires": [
                    {"name": item.name, "type": item.type, "description": item.description}
                    for item in store.template_requirements(template)
                ],
            }
        )
    if not out:
        return ToolResult(content="This workspace has no dashboard templates yet.")
    return ToolResult(content=json.dumps({"templates": out}))


# --------------------------------------------------------------------------
# Writes


def _create_dashboard(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> ToolResult:
    try:
        spec = _spec(args)
    except ValidationError as exc:
        return ToolResult(content=f"Error: invalid spec: {exc.errors()[:3]}")
    try:
        dashboard = store.create_dashboard(
            db,
            workspace_id=context.workspace_id,
            user_id=context.user_id,
            name=_text(args, "name"),
            description=_text(args, "description"),
            dataset_id=_text(args, "dataset_id"),
            spec=spec,
        )
    except store.DashboardNameTaken as exc:
        return ToolResult(content=f"Error: a dashboard named “{exc}” already exists.")
    except AnalyticsValidationError as exc:
        return ToolResult(content=f"Error: {exc}")
    except DashboardBindError as exc:
        return ToolResult(content=f"Dashboard rejected:\n{exc.report.render()}")
    return ToolResult(
        content=f"Created dashboard “{dashboard.name}” (id {dashboard.id}) showing "
        f"{_describe(spec)}. Pin it from the dashboards screen to keep it in view."
    )


def _preview_create_dashboard(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> str:
    try:
        spec = _spec(args)
    except ValidationError as exc:
        return f"Invalid dashboard spec: {exc.errors()[:3]}"
    dataset = _find_dataset_name(
        db, workspace_id=context.workspace_id, dataset_id=_text(args, "dataset_id")
    )
    return (
        f"Create dashboard “{_text(args, 'name') or 'Untitled'}” over {dataset}: "
        f"{_describe(spec)}."
    )


def _create_template(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> ToolResult:
    try:
        spec = _spec(args)
        required = _requirements(args)
    except (ValidationError, ValueError) as exc:
        return ToolResult(content=f"Error: invalid template: {exc}")
    try:
        template = store.create_template(
            db,
            workspace_id=context.workspace_id,
            user_id=context.user_id,
            name=_text(args, "name"),
            description=_text(args, "description"),
            required=required,
            spec=spec,
        )
    except store.DashboardNameTaken as exc:
        return ToolResult(content=f"Error: a template named “{exc}” already exists.")
    except DashboardBindError as exc:
        return ToolResult(content=f"Template rejected:\n{exc.report.render()}")
    shape = ", ".join(f"{item.name}:{item.type}" for item in required)
    return ToolResult(
        content=f"Created template “{template.name}” (id {template.id}) showing "
        f"{_describe(spec)}. It requires a dataset with {shape}."
    )


def _preview_create_template(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> str:
    try:
        spec = _spec(args)
        required = _requirements(args)
    except (ValidationError, ValueError) as exc:
        return f"Invalid dashboard template: {exc}"
    shape = ", ".join(f"{item.name} ({item.type})" for item in required)
    return (
        f"Create dashboard template “{_text(args, 'name') or 'Untitled'}”: "
        f"{_describe(spec)}. Any dataset it is bound to must supply {shape}."
    )


def _bind_template(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    template = _find_template(db, context, args)
    if template is None:
        return ToolResult(content="Error: no such dashboard template in this workspace.")
    try:
        bindings = _bindings(args)
    except ValueError as exc:
        return ToolResult(content=f"Error: {exc}")
    try:
        dashboard = store.bind_template(
            db,
            workspace_id=context.workspace_id,
            user_id=context.user_id,
            template=template,
            dataset_id=_text(args, "dataset_id"),
            name=_text(args, "name") or template.name,
            description=_text(args, "description"),
            bindings=bindings,
        )
    except store.DashboardNameTaken as exc:
        return ToolResult(content=f"Error: a dashboard named “{exc}” already exists.")
    except AnalyticsValidationError as exc:
        return ToolResult(content=f"Error: {exc}")
    except DashboardBindError as exc:
        # The report is the repair prompt: every finding, each naming the column
        # it is about, so the next attempt fixes all of them at once.
        return ToolResult(
            content=f"Binding refused — this dataset does not satisfy "
            f"“{template.name}”:\n{exc.report.render()}"
        )
    return ToolResult(
        content=f"Bound “{template.name}” to that dataset as dashboard "
        f"“{dashboard.name}” (id {dashboard.id})."
    )


def _preview_bind_template(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> str:
    template = _find_template(db, context, args)
    if template is None:
        return "No such dashboard template in this workspace."
    dataset_id = _text(args, "dataset_id")
    dataset = _find_dataset_name(
        db, workspace_id=context.workspace_id, dataset_id=dataset_id
    )
    try:
        bindings = _bindings(args)
        required = store.template_requirements(template)
        columns = store.dataset_columns(
            db, workspace_id=context.workspace_id, dataset_id=dataset_id
        )
    except (ValueError, AnalyticsValidationError) as exc:
        return f"Bind “{template.name}” to {dataset}: cannot be checked — {exc}"
    report = bind_report(required, bindings=bindings, columns=columns)
    if not report.ok:
        # A preview that showed the arrows and hid the refusal would ask someone
        # to approve a bind that cannot happen.
        return (
            f"Bind “{template.name}” to {dataset} — this would be refused:\n"
            f"{report.render()}"
        )
    applied = effective_bindings(required, bindings)
    spec = rebind_spec(
        DashboardSpec.model_validate(json.loads(template.spec_json)), applied
    )
    arrows = ", ".join(
        f"{name} → {column}" for name, column in sorted(applied.items()) if name != column
    )
    return (
        f"Bind “{template.name}” to {dataset} as “{_text(args, 'name') or template.name}”: "
        f"{_describe(spec)}." + (f" Mapping {arrows}." if arrows else "")
    )


# --------------------------------------------------------------------------

_REQUIRED_COLUMNS_SCHEMA: Dict[str, Any] = {
    "type": "array",
    "description": (
        "The dataset shape this template requires. A dataset that does not "
        "supply every one of these, with a compatible type, is refused at bind "
        "time rather than when someone opens the chart."
    ),
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "type": {
                "type": "string",
                "enum": ["boolean", "integer", "number", "string", "date", "datetime"],
            },
            "description": {"type": "string"},
        },
        "required": ["name", "type"],
    },
}


def registry_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    return {
        "list_dashboards": ToolSpec(
            name="list_dashboards",
            description=(
                "List the workspace's saved dashboards with what each one shows."
            ),
            parameters={"type": "object", "properties": {}},
            executor=_list_dashboards,
        ),
        "create_dashboard": ToolSpec(
            name="create_dashboard",
            description=(
                "Save a chart over a dataset. The query runs before the dashboard "
                "is saved, so a spec that cannot execute is refused here. Use "
                "list_datasets for ids and column names."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "dataset_id": {"type": "string"},
                    **_SPEC_PROPERTIES,
                },
                "required": ["name", "dataset_id"],
            },
            executor=_create_dashboard,
            read_only=False,
            preview=_preview_create_dashboard,
        ),
        "list_dashboard_templates": ToolSpec(
            name="list_dashboard_templates",
            description=(
                "List reusable dashboard definitions and the dataset shape each "
                "one requires."
            ),
            parameters={"type": "object", "properties": {}},
            executor=_list_templates,
        ),
        "create_dashboard_template": ToolSpec(
            name="create_dashboard_template",
            description=(
                "Save a dashboard definition that is not tied to one dataset. "
                "Write the spec against the column names you declare in "
                "required_columns, then bind it to real data with "
                "bind_dashboard_template. Use this when the same view is wanted "
                "over several datasets."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "required_columns": _REQUIRED_COLUMNS_SCHEMA,
                    **_SPEC_PROPERTIES,
                },
                "required": ["name", "required_columns"],
            },
            executor=_create_template,
            read_only=False,
            preview=_preview_create_template,
        ),
        "bind_dashboard_template": ToolSpec(
            name="bind_dashboard_template",
            description=(
                "Create a dashboard by pointing a template at a dataset. Map any "
                "declared column whose name differs in this dataset with "
                "column_bindings, e.g. {\"region\": \"territory\"}; columns you "
                "leave out bind to the identical name. A dataset that does not "
                "satisfy the template is refused with the reasons."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "template_id": {"type": "string"},
                    "template": {"type": "string", "description": "Template name, if no id."},
                    "dataset_id": {"type": "string"},
                    "name": {"type": "string", "description": "Name for the new dashboard."},
                    "description": {"type": "string"},
                    "column_bindings": {
                        "type": "object",
                        "description": "Declared column name -> this dataset's column name.",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["dataset_id"],
            },
            executor=_bind_template,
            read_only=False,
            preview=_preview_bind_template,
        ),
    }


__all__ = ["registry_tools"]
