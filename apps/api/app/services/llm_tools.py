from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Collection, Dict, List, Optional, Tuple

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Dataset, GraphEdge, GraphEntity
from ..schemas import DatasetQuery
from .analytics import AnalyticsValidationError, current_dataset_version, execute_dataset_query
from .graph import _normalized, name_candidates
from .memory import recall
from .retrieval import Evidence, search_evidence

MAX_RESULT_CHARS = 4000


@dataclass(frozen=True)
class ToolContext:
    workspace_id: str
    user_id: str
    conversation_id: str
    #: The document this turn is happening beside, when the user asked from the
    #: chat panel in the document editor. It is what "this paragraph" refers to:
    #: the document tools fall back to it, so the model does not have to list
    #: documents and guess which one is on the user's screen.
    document_id: str = ""
    #: The same fact for the other two panels — what "this file" and "this chart"
    #: refer to. Three named fields rather than one polymorphic pair because the
    #: tools are not polymorphic: `edit_document` wants a document id and
    #: `fs_write` wants a project id, and a tool that had to inspect a kind
    #: before trusting an id would be one `if` away from the wrong table.
    project_id: str = ""
    dashboard_id: str = ""


@dataclass
class ToolResult:
    content: str
    evidence: List[Evidence] = field(default_factory=list)
    #: Files the call produced, as the JSON-safe descriptors
    #: `sandbox.outputs.persist_artifacts` returns. Separate from `content`
    #: because they are for the *reader*, not the model: `content` names them in
    #: a sentence a language model can act on, and is clipped to a character
    #: budget that would silently drop the last chart from a chatty run.
    artifacts: List[Dict[str, Any]] = field(default_factory=list)

    def bounded_content(self) -> str:
        return self.content[:MAX_RESULT_CHARS]


ToolExecutor = Callable[[Session, ToolContext, Dict[str, Any]], ToolResult]
# Renders what a call *would* do, without doing it. Runs at approval time so the
# user sees the change (a unified diff, a sentence) instead of raw arguments.
ToolPreview = Callable[[Session, ToolContext, Dict[str, Any]], str]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Dict[str, Any]
    executor: ToolExecutor
    read_only: bool = True
    preview: Optional[ToolPreview] = None
    #: Tighten-only approval flag. A custom sandbox tool with approval="always"
    #: sets this so `evaluate_policy` clamps any resulting `allow` to `ask` — it
    #: can only escalate an allow to a prompt, never loosen a deny.
    force_ask: bool = False


def _search_sources(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(content="Error: query is required.")
    evidence = search_evidence(db, workspace_id=context.workspace_id, query=query)
    if not evidence:
        return ToolResult(content="No matching passages in the indexed sources.")
    return ToolResult(content="", evidence=evidence)


def _list_datasets(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    datasets = list(
        db.scalars(
            select(Dataset).where(Dataset.workspace_id == context.workspace_id)
        )
    )
    out = []
    for dataset in datasets:
        try:
            _dataset, version = current_dataset_version(
                db, workspace_id=context.workspace_id, dataset_id=dataset.id
            )
        except AnalyticsValidationError:
            version = None
        out.append(
            {
                "id": dataset.id,
                "name": dataset.name,
                "description": dataset.description,
                "row_count": version.row_count if version else 0,
                "columns": json.loads(version.schema_json) if version else [],
            }
        )
    return ToolResult(content=json.dumps({"datasets": out}))


def _query_dataset(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    dataset_id = str(args.get("dataset_id") or "")
    raw_query = args.get("query") or {}
    try:
        query = DatasetQuery.model_validate(raw_query)
    except ValidationError as exc:
        return ToolResult(content=f"Invalid query: {exc.errors()[:3]}")
    try:
        result = execute_dataset_query(
            db,
            workspace_id=context.workspace_id,
            dataset_id=dataset_id,
            query=query,
        )
    except AnalyticsValidationError as exc:
        return ToolResult(content=f"Query rejected: {exc}")
    return ToolResult(
        content=json.dumps(
            {
                "columns": result.columns,
                "rows": result.rows[:50],
                "row_count": result.row_count,
                "truncated": result.truncated or len(result.rows) > 50,
            },
            default=str,
        )
    )


def _graph_lookup(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    name = _normalized(str(args.get("entity") or ""))
    if not name:
        return ToolResult(content="Error: entity is required.")
    entity = db.scalar(
        select(GraphEntity)
        .where(
            GraphEntity.workspace_id == context.workspace_id,
            # 'the atlas' and 'atlas' may be one merged node; try both spellings
            # and prefer the exact one when both survived separately.
            GraphEntity.normalized_name.in_(name_candidates(name)),
        )
        .order_by((GraphEntity.normalized_name == name).desc())
    )
    if entity is None:
        return ToolResult(content=f"No graph entity named “{args.get('entity')}”.")
    edges = list(
        db.scalars(
            select(GraphEdge)
            .where(
                GraphEdge.workspace_id == context.workspace_id,
                (GraphEdge.from_entity_id == entity.id)
                | (GraphEdge.to_entity_id == entity.id),
            )
            .order_by(GraphEdge.weight.desc())
            .limit(12)
        )
    )
    neighbor_ids = {edge.from_entity_id for edge in edges} | {
        edge.to_entity_id for edge in edges
    }
    # The workspace filter is not redundant. `neighbor_ids` comes from
    # workspace-scoped edges, so today both endpoints are in this workspace —
    # but nothing in the schema enforces that (the FK points at graph_entities,
    # not at (workspace_id, id)), and this query is what would turn one
    # cross-workspace edge into another tenant's entity name in the model's
    # context. graph._entities_by_id already filters; this now matches it.
    names = {
        row.id: row.name
        for row in db.scalars(
            select(GraphEntity).where(
                GraphEntity.workspace_id == context.workspace_id,
                GraphEntity.id.in_(neighbor_ids),
            )
        )
    }
    relations = [
        {
            "from": names.get(edge.from_entity_id, "?"),
            "to": names.get(edge.to_entity_id, "?"),
            "relation": edge.relation,
            "weight": edge.weight,
        }
        for edge in edges
    ]
    return ToolResult(
        content=json.dumps(
            {
                "entity": entity.name,
                "type": entity.entity_type,
                "mentions": entity.mention_count,
                "relations": relations,
            }
        )
    )


def _recall_memory(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    query = str(args.get("query") or "").strip()
    if not query:
        return ToolResult(content="Error: query is required.")
    context_result = recall(
        db,
        workspace_id=context.workspace_id,
        conversation_id=context.conversation_id,
        query=query,
    )
    if context_result.empty:
        return ToolResult(content="No stored memories match that query.")
    payload = {
        "memories": [
            {"kind": item.kind, "content": item.content}
            for item in context_result.items
        ],
        "graph": context_result.graph_digest,
    }
    return ToolResult(content=json.dumps(payload))


def registry_families(
    db: Session, context: ToolContext
) -> List[Tuple[str, Dict[str, ToolSpec]]]:
    """Every tool the registry would offer, grouped under the family it ships
    with. The names are UI-facing: the provisioning checklist groups by them
    rather than dumping sixty flat checkboxes. `build_registry` flattens this
    same list, so the catalogue and the live registry cannot disagree."""
    core = {
        "search_sources": ToolSpec(
            name="search_sources",
            description=(
                "Search the workspace's indexed source documents. Returns numbered "
                "passages that can be cited with [n]."
            ),
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            executor=_search_sources,
        ),
        "list_datasets": ToolSpec(
            name="list_datasets",
            description="List the workspace's tabular datasets with their schemas.",
            parameters={"type": "object", "properties": {}},
            executor=_list_datasets,
        ),
        "query_dataset": ToolSpec(
            name="query_dataset",
            description=(
                "Run a typed aggregation over a dataset. `query` supports filters "
                "(field/operator/value), group_by, metrics (count/sum/avg/min/max), "
                "order_by, and limit. No SQL."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "dataset_id": {"type": "string"},
                    "query": {"type": "object"},
                },
                "required": ["dataset_id", "query"],
            },
            executor=_query_dataset,
        ),
        "graph_lookup": ToolSpec(
            name="graph_lookup",
            description="Look up an entity in the workspace knowledge graph with its relations.",
            parameters={
                "type": "object",
                "properties": {"entity": {"type": "string"}},
                "required": ["entity"],
            },
            executor=_graph_lookup,
        ),
        "recall_memory": ToolSpec(
            name="recall_memory",
            description="Search long-term memories saved from earlier conversations.",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            executor=_recall_memory,
        ),
    }
    return [
        ("core", core),
        ("memory", agentic_memory_tools(db, context)),
        ("graph", graph_walk_tools(db, context)),
        ("artifacts", artifact_tools(db, context)),
        ("projects", project_tools(db, context)),
        ("dashboards", dashboard_tools(db, context)),
        ("integrations", integration_tools(db, context)),
        ("databases", database_tools(db, context)),
        ("mcp", mcp_tools(db, context)),
        ("sandbox", sandbox_tools(db, context)),
        ("sandbox_tools", sandbox_custom_tools(db, context)),
    ]


def build_registry(
    db: Session, context: ToolContext, allowed: Optional[Collection[str]] = None
) -> Dict[str, ToolSpec]:
    """The tools this turn may be offered. `allowed` is an agent's provisioned
    subset: a pure intersection, so it can only narrow what the registry holds —
    a name it grants that no family ships resolves to nothing. Workspace
    `ToolPolicy` (`resolve_policy`) still applies to every surviving tool; the
    subset decides what the model *sees*, never what it is *permitted*."""
    registry: Dict[str, ToolSpec] = {}
    for _family, tools in registry_families(db, context):
        registry.update(tools)
    if allowed is not None:
        names = set(allowed)
        registry = {name: spec for name, spec in registry.items() if name in names}
    return registry


def agentic_memory_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """Deliberate memory writes (remember/forget) and deep search, next to the
    read-only recall_memory above."""
    from .memory_tools import registry_tools

    return registry_tools(db, context)


def graph_walk_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """Multi-hop walks over the knowledge graph, next to the one-hop graph_lookup."""
    from .graph_tools import registry_tools

    return registry_tools(db, context)


def artifact_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """Documents and kanban boards the agent can author and revise."""
    from .artifacts import registry_tools

    return registry_tools(db, context)


def project_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """The virtual filesystem behind multi-file code projects."""
    from .projects import registry_tools

    return registry_tools(db, context)


def dashboard_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """Saved charts over the datasets `query_dataset` above explores, and the
    reusable definitions behind them. Authoring only — pinning one to a home
    screen is the user's call, not the model's."""
    from .dashboards.tools import registry_tools

    return registry_tools(db, context)


def integration_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """Tools for connected external accounts (Gmail, Strava). Extended in 3B."""
    from .connectors import registry_tools

    return registry_tools(db, context)


def database_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """SQL over the workspace's connected databases. Empty when none are configured."""
    from .dbconnect import registry_tools

    return registry_tools(db, context)


def mcp_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """Tools discovered on the workspace's configured MCP servers."""
    from .mcp import registry_tools

    return registry_tools(db, context)


def sandbox_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """Server-side execution in a hosted microVM (ADR 0005).

    Empty when SANDBOX_ENABLED=0, so a deployment without an execution provider
    simply has no run tools rather than tools that fail on first use.
    """
    from .sandbox import registry_tools

    return registry_tools(db, context)


def sandbox_custom_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """Workspace-defined tools executed in the session sandbox (0036).

    A separate family from the builtin run_* tools: these are authored per
    workspace, each carrying its own egress allowlist and approval policy. Empty
    when SANDBOX_ENABLED=0 for the same reason the builtin family is — no
    execution provider means no tool that could run.
    """
    from .sandbox.custom import registry_tools

    return registry_tools(db, context)
