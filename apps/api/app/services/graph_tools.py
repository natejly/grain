"""Agent tools that walk the knowledge graph instead of peeking at one hop.

`graph_lookup` answers "what touches this entity"; these answer "what is near it"
and "how are these two connected". Both are read-only projections of an already
rebuilt graph, and both bound their output: a hub with hundreds of edges returns a
ranked slice with `truncated: true`, never the whole neighbourhood.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import GraphEdge, GraphEntity, GraphProjection
from .graph import (
    DEFAULT_NEIGHBOR_HOPS,
    DEFAULT_NEIGHBOR_RESULTS,
    DEFAULT_PATH_HOPS,
    MAX_NEIGHBOR_HOPS,
    MAX_NEIGHBOR_RESULTS,
    MAX_PATH_HOPS,
    EntityMatch,
    neighbors,
    resolve_entity,
    shortest_path,
)
from .llm_tools import MAX_RESULT_CHARS, ToolContext, ToolResult, ToolSpec

# Enough provenance for the model to cite a connection without pasting the whole
# id list a hub edge can accumulate.
MAX_PROVENANCE_PER_STEP = 3

# `graph_export` bounds. The entity cap matches GET /api/graph's ceiling; edges
# are capped at four per entity, same as that route. Both are row-fetch caps,
# not payload promises: every delivery path clips ToolResult.content to
# MAX_RESULT_CHARS, so the payload is refitted to that budget before it is
# serialized (`_fit_within`) and the truncated flags stay honest.
DEFAULT_EXPORT_ENTITIES = 50
MAX_EXPORT_ENTITIES = 200

# When both lists compete for the character budget, entities get first claim on
# this share of it — the map matters more than any one link — and whatever they
# leave unspent flows to the edges.
ENTITY_BUDGET_SHARE = 0.6


def _fit_within(items: List[str], budget: int) -> tuple[int, int]:
    """How many of the pre-serialized items survive in `budget` chars, and how
    many chars they spend. Joining is json.dumps' default ", " (2 chars), and
    the enclosing brackets are already priced into the envelope."""
    spent = 0
    for index, item in enumerate(items):
        cost = len(item) + (2 if index else 0)
        if spent + cost > budget:
            return index, spent
        spent += cost
    return len(items), spent


def _int_arg(args: Dict[str, Any], key: str, default: int) -> int:
    value = args.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        # JSON permits Infinity/NaN, and int() raises OverflowError on the former.
        # A malformed bound falls back to the default rather than out of the tool.
        return default


def _missing(name: str, match: EntityMatch) -> ToolResult:
    if match.suggestions:
        return ToolResult(
            content=f"No graph entity named “{name}”. Closest names: "
            + ", ".join(match.suggestions)
        )
    return ToolResult(content=f"No graph entity named “{name}”.")


def _graph_neighbors(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> ToolResult:
    name = str(args.get("entity") or "").strip()
    if not name:
        return ToolResult(content="Error: entity is required.")
    match = resolve_entity(db, context.workspace_id, name)
    if match.entity is None:
        return _missing(name, match)
    hops = min(max(_int_arg(args, "hops", DEFAULT_NEIGHBOR_HOPS), 1), MAX_NEIGHBOR_HOPS)
    limit = min(
        max(_int_arg(args, "limit", DEFAULT_NEIGHBOR_RESULTS), 1), MAX_NEIGHBOR_RESULTS
    )
    found, truncated = neighbors(
        db, context.workspace_id, match.entity, max_hops=hops, limit=limit
    )
    if not found:
        return ToolResult(
            content=f"“{match.entity.name}” has no connections in the graph."
        )
    envelope = {
        "entity": match.entity.name,
        "type": match.entity.entity_type,
        "hops": hops,
        "truncated": False,
        "neighbors": [],
    }
    items = [
        json.dumps(
            {
                "name": item.name,
                "type": item.entity_type,
                "distance": item.distance,
                "relation": item.relation,
                "via": item.via,
                "weight": item.weight,
                "confidence": round(item.confidence, 2),
            }
        )
        for item in found
    ]
    # A full result at the row cap can outgrow the transport clip, so the list
    # is refitted to the character budget the same way `graph_export` is.
    kept, _ = _fit_within(items, MAX_RESULT_CHARS - len(json.dumps(envelope)))
    envelope["truncated"] = truncated or kept < len(items)
    return ToolResult(
        content=json.dumps(
            {**envelope, "neighbors": [json.loads(item) for item in items[:kept]]}
        )
    )


def _graph_path(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    start_name = str(args.get("from_entity") or "").strip()
    goal_name = str(args.get("to_entity") or "").strip()
    if not start_name or not goal_name:
        return ToolResult(content="Error: from_entity and to_entity are required.")
    start = resolve_entity(db, context.workspace_id, start_name)
    if start.entity is None:
        return _missing(start_name, start)
    goal = resolve_entity(db, context.workspace_id, goal_name)
    if goal.entity is None:
        return _missing(goal_name, goal)
    hops = min(max(_int_arg(args, "max_hops", DEFAULT_PATH_HOPS), 1), MAX_PATH_HOPS)
    path = shortest_path(
        db, context.workspace_id, start.entity, goal.entity, max_hops=hops
    )
    if path is None:
        return ToolResult(
            content=json.dumps(
                {
                    "from": start.entity.name,
                    "to": goal.entity.name,
                    "found": False,
                    "reason": f"No path within {hops} hops.",
                }
            )
        )
    steps: List[Dict[str, Any]] = [
        {
            "from": step.from_name,
            "relation": step.relation,
            "to": step.to_name,
            "weight": step.weight,
            "confidence": round(step.confidence, 2),
            "source_ids": step.source_ids[:MAX_PROVENANCE_PER_STEP],
            "chunk_ids": step.chunk_ids[:MAX_PROVENANCE_PER_STEP],
            "memory_ids": step.memory_ids[:MAX_PROVENANCE_PER_STEP],
        }
        for step in path
    ]
    return ToolResult(
        content=json.dumps(
            {
                "from": start.entity.name,
                "to": goal.entity.name,
                "found": True,
                "hops": len(steps),
                "path": steps,
            }
        )
    )


def _graph_export(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> ToolResult:
    """A shareable snapshot of the whole graph, not a walk from one entity.

    Edges name their endpoints instead of carrying entity ids: ids are not
    stable across rebuilds (ADR 0002), names are. Provenance id lists stay out
    on purpose — an export is for reading the shape of what the workspace
    knows; a caller who wants the passages behind one link asks `graph_path`.
    """
    limit = min(
        max(_int_arg(args, "limit", DEFAULT_EXPORT_ENTITIES), 1), MAX_EXPORT_ENTITIES
    )
    projection = db.scalar(
        select(GraphProjection).where(
            GraphProjection.workspace_id == context.workspace_id
        )
    )
    entities = list(
        db.scalars(
            select(GraphEntity)
            .where(GraphEntity.workspace_id == context.workspace_id)
            .order_by(GraphEntity.mention_count.desc(), GraphEntity.name.asc())
            .limit(limit + 1)
        )
    )
    more_entities = len(entities) > limit
    entities = entities[:limit]
    names = {entity.id: entity.name for entity in entities}
    edges: List[GraphEdge] = []
    more_edges = False
    if names:
        edge_cap = limit * 4
        edges = list(
            db.scalars(
                select(GraphEdge)
                .where(
                    GraphEdge.workspace_id == context.workspace_id,
                    GraphEdge.from_entity_id.in_(names),
                    GraphEdge.to_entity_id.in_(names),
                )
                .order_by(GraphEdge.weight.desc(), GraphEdge.id.asc())
                .limit(edge_cap + 1)
            )
        )
        more_edges = len(edges) > edge_cap
        edges = edges[:edge_cap]

    # Refit to the transport clip. The envelope is priced with both flags at
    # their longest spelling (false), so the item budget can only be generous
    # by a character, never short.
    envelope: Dict[str, Any] = {
        "status": projection.status if projection else "empty",
        "version": projection.version if projection else "",
        "built_at": (
            projection.built_at.isoformat()
            if projection and projection.built_at
            else None
        ),
        "entities_truncated": False,
        "edges_truncated": False,
        "entities": [],
        "edges": [],
    }
    item_budget = MAX_RESULT_CHARS - len(json.dumps(envelope))
    entity_items = [
        json.dumps(
            {
                "name": entity.name,
                "type": entity.entity_type,
                "mentions": entity.mention_count,
            }
        )
        for entity in entities
    ]
    entity_budget = (
        item_budget if not edges else int(item_budget * ENTITY_BUDGET_SHARE)
    )
    kept_entities, entity_chars = _fit_within(entity_items, entity_budget)
    kept_names = {entity.id: entity.name for entity in entities[:kept_entities]}
    edge_items = [
        json.dumps(
            {
                "from": kept_names[edge.from_entity_id],
                "to": kept_names[edge.to_entity_id],
                "relation": edge.relation,
                "weight": edge.weight,
                "confidence": round(edge.confidence, 2),
            }
        )
        for edge in edges
        # An edge whose endpoint fell out of the refit goes with it.
        if edge.from_entity_id in kept_names and edge.to_entity_id in kept_names
    ]
    kept_edges, _ = _fit_within(edge_items, item_budget - entity_chars)
    envelope["entities_truncated"] = more_entities or kept_entities < len(entities)
    # From the caller's seat an edge dropped for its endpoint is as gone as one
    # dropped for space, so the flag covers both against the fetched window.
    envelope["edges_truncated"] = more_edges or kept_edges < len(edges)
    return ToolResult(
        content=json.dumps(
            {
                **envelope,
                "entities": [json.loads(item) for item in entity_items[:kept_entities]],
                "edges": [json.loads(item) for item in edge_items[:kept_edges]],
            }
        )
    )


def registry_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    return {
        "graph_neighbors": ToolSpec(
            name="graph_neighbors",
            description=(
                "Entities within N hops of one entity in the knowledge graph, with "
                "the relation each was reached by. hops defaults to 2 (max 3); the "
                "result is a ranked slice and sets truncated when more exist."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "hops": {"type": "integer", "minimum": 1, "maximum": MAX_NEIGHBOR_HOPS},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_NEIGHBOR_RESULTS,
                    },
                },
                "required": ["entity"],
            },
            executor=_graph_neighbors,
        ),
        "graph_path": ToolSpec(
            name="graph_path",
            description=(
                "Shortest chain of relations connecting two entities in the "
                "knowledge graph, with the passages each link came from, or a "
                "clear no-path answer."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "from_entity": {"type": "string"},
                    "to_entity": {"type": "string"},
                    "max_hops": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_PATH_HOPS,
                    },
                },
                "required": ["from_entity", "to_entity"],
            },
            executor=_graph_path,
        ),
        "graph_export": ToolSpec(
            name="graph_export",
            description=(
                "The whole knowledge graph as one shareable snapshot: the most-"
                "mentioned entities (limit defaults to "
                f"{DEFAULT_EXPORT_ENTITIES}, max {MAX_EXPORT_ENTITIES}) and the "
                "relations among them, named by entity rather than id. Sets "
                "entities_truncated/edges_truncated when the graph holds more."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_EXPORT_ENTITIES,
                    },
                },
                "required": [],
            },
            executor=_graph_export,
        ),
    }
