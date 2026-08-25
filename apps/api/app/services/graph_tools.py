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
from .llm_tools import ToolContext, ToolResult, ToolSpec

# Enough provenance for the model to cite a connection without pasting the whole
# id list a hub edge can accumulate.
MAX_PROVENANCE_PER_STEP = 3

# `graph_export` bounds. The entity cap matches GET /api/graph's ceiling; edges
# are capped at four per entity, same as that route.
DEFAULT_EXPORT_ENTITIES = 50
MAX_EXPORT_ENTITIES = 200


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
    return ToolResult(
        content=json.dumps(
            {
                "entity": match.entity.name,
                "type": match.entity.entity_type,
                "hops": hops,
                "truncated": truncated,
                "neighbors": [
                    {
                        "name": item.name,
                        "type": item.entity_type,
                        "distance": item.distance,
                        "relation": item.relation,
                        "via": item.via,
                        "weight": item.weight,
                        "confidence": round(item.confidence, 2),
                    }
                    for item in found
                ],
            }
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
    entities_truncated = len(entities) > limit
    entities = entities[:limit]
    names = {entity.id: entity.name for entity in entities}
    edges: List[GraphEdge] = []
    edges_truncated = False
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
                .order_by(GraphEdge.weight.desc())
                .limit(edge_cap + 1)
            )
        )
        edges_truncated = len(edges) > edge_cap
        edges = edges[:edge_cap]
    return ToolResult(
        content=json.dumps(
            {
                "status": projection.status if projection else "empty",
                "version": projection.version if projection else "",
                "built_at": (
                    projection.built_at.isoformat()
                    if projection and projection.built_at
                    else None
                ),
                "entities_truncated": entities_truncated,
                "edges_truncated": edges_truncated,
                "entities": [
                    {
                        "name": entity.name,
                        "type": entity.entity_type,
                        "mentions": entity.mention_count,
                    }
                    for entity in entities
                ],
                "edges": [
                    {
                        "from": names[edge.from_entity_id],
                        "to": names[edge.to_entity_id],
                        "relation": edge.relation,
                        "weight": edge.weight,
                        "confidence": round(edge.confidence, 2),
                    }
                    for edge in edges
                ],
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
