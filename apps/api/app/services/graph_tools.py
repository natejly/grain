"""Agent tools that walk the knowledge graph instead of peeking at one hop.

`graph_lookup` answers "what touches this entity"; these answer "what is near it"
and "how are these two connected". Both are read-only projections of an already
rebuilt graph, and both bound their output: a hub with hundreds of edges returns a
ranked slice with `truncated: true`, never the whole neighbourhood.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from sqlalchemy.orm import Session

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
    }
