"""Agent tools for long-term memory.

Memory used to be written only by the post-run extractor, so "remember that I
prefer X" worked only when the extractor happened to notice. These tools let the
model write and retract memories on purpose.

`remember` and `forget` are write-capable, so each carries a `preview`: the
approval card must show the exact text that will be stored, and — the one that
matters — *which* memories a forget would tombstone. Silently forgetting the
wrong memory is unrecoverable from the user's side, because nothing in the
conversation reveals that it happened.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import MemoryItem
from .llm_tools import ToolContext, ToolResult, ToolSpec
from .memory import (
    REMEMBER_KINDS,
    SHARED_OWNER,
    forget_memories,
    memory_owner,
    normalize_memory_content,
    recall,
    remember_memory,
    resolve_forget_targets,
)

MAX_SEARCH_LIMIT = 25
EXCERPT_CHARS = 160


def _text(args: Dict[str, Any], key: str) -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _kind(args: Dict[str, Any]) -> str:
    kind = _text(args, "kind").lower() or "fact"
    return kind if kind in REMEMBER_KINDS else "fact"


def _entities(args: Dict[str, Any]) -> List[str]:
    raw = args.get("entities")
    if not isinstance(raw, list):
        return []
    return [str(name).strip() for name in raw if str(name).strip()]


def _excerpt(item: MemoryItem) -> str:
    body = item.content
    return body if len(body) <= EXCERPT_CHARS else body[:EXCERPT_CHARS] + "…"


def _remember(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    content = _text(args, "content")
    if not content:
        return ToolResult(content="Error: content is required.")
    result = remember_memory(
        db,
        workspace_id=context.workspace_id,
        conversation_id=context.conversation_id,
        user_id=context.user_id,
        content=content,
        kind=_kind(args),
        entities=_entities(args),
    )
    verb = {
        "created": "Stored",
        "updated": "Already knew this; reinforced",
        "restored": "Restored a previously forgotten",
    }[result.outcome]
    return ToolResult(
        content=(
            f"{verb} {result.item.kind} memory (id {result.item.id}): "
            f"{result.item.content}"
        )
    )


def _preview_remember(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    content = _text(args, "content")
    if not content:
        return "This will fail: content is required."
    stored = normalize_memory_content(content)
    lines = [f"Store a durable {_kind(args)} memory:", f"  {stored}"]
    entities = _entities(args)
    if entities:
        lines.append(f"  entities: {', '.join(entities[:16])}")
    # The card has to say who will be able to recall this, because after ADR 0010
    # that is no longer the same answer in every thread — and "the whole
    # workspace" is the half of it a person would want to catch before approving.
    if memory_owner(db, context.conversation_id, context.user_id) == SHARED_OWNER:
        lines.append(
            "It will be recallable by every member, in every future conversation "
            "in this workspace."
        )
    else:
        lines.append(
            "This thread is personal, so the memory is yours: it will be "
            "recallable in your future conversations and no other member's."
        )
    return "\n".join(lines)


def _forget_targets(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> Tuple[List[MemoryItem], str]:
    return resolve_forget_targets(
        db,
        workspace_id=context.workspace_id,
        memory_id=_text(args, "memory_id") or None,
        content=_text(args, "content") or None,
        # You can forget what you can recall: the workspace's, and your own —
        # and, from a space's thread, that space's shelf.
        viewer_id=context.user_id,
        space_id=context.space_id,
    )


def _forget(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    targets, error = _forget_targets(db, context, args)
    if error:
        return ToolResult(content=f"Error: {error}")
    # Snapshot before tombstoning: the report must name what was forgotten.
    forgotten = [{"id": item.id, "content": item.content} for item in targets]
    forget_memories(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        items=targets,
    )
    return ToolResult(content=json.dumps({"forgotten": forgotten}))


def _preview_forget(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    targets, error = _forget_targets(db, context, args)
    if error:
        return f"This will fail: {error}"
    plural = "memory" if len(targets) == 1 else "memories"
    lines = [f"Forget {len(targets)} {plural}:"]
    lines.extend(f"  - ({item.kind}) {_excerpt(item)}" for item in targets)
    lines.append("They stop being recalled and will not be re-learned automatically.")
    return "\n".join(lines)


def _limit(args: Dict[str, Any], default: int) -> int:
    raw = args.get("limit")
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(1, min(MAX_SEARCH_LIMIT, value))


def _search_memory(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    query = _text(args, "query")
    if not query:
        return ToolResult(content="Error: query is required.")
    settings = get_settings()
    limit = _limit(args, settings.memory_recall_limit)
    found = recall(
        db,
        workspace_id=context.workspace_id,
        conversation_id=context.conversation_id,
        query=query,
        viewer_id=context.user_id,
        # model_copy keeps the cached Settings singleton unmutated; only the
        # recall depth differs from what the turn was injected with.
        settings=settings.model_copy(update={"memory_recall_limit": limit}),
    )
    if not found.items:
        return ToolResult(content="No stored memories match that query.")
    return ToolResult(
        content=json.dumps(
            {
                "memories": [
                    {"id": item.id, "kind": item.kind, "content": item.content}
                    for item in found.items
                ],
                "graph": found.graph_digest,
            }
        )
    )


def registry_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    return {
        "remember": ToolSpec(
            name="remember",
            description=(
                "Store a durable fact or preference the user asked you to keep, or "
                "that will matter in later conversations. Use it when the user says "
                "to remember something — do not rely on automatic extraction. "
                "Storing the same text twice reinforces the existing memory instead "
                "of duplicating it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "The memory, written as a standalone sentence that will "
                            "still make sense months from now."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": list(REMEMBER_KINDS),
                        "description": (
                            "'preference' for how the user wants things done; "
                            "'fact' for anything else. Default 'fact'."
                        ),
                    },
                    "entities": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "People, projects, or systems this is about.",
                    },
                },
                "required": ["content"],
            },
            executor=_remember,
            read_only=False,
            preview=_preview_remember,
        ),
        "forget": ToolSpec(
            name="forget",
            description=(
                "Retract a stored memory so it stops being recalled. Give either "
                "memory_id (from search_memory) or content — distinctive text that "
                "appears in the memory. Prefer memory_id; matching by content "
                "forgets every active memory containing that text."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "description": "Exact id of the memory to forget.",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Text contained in the memory to forget, if no id is "
                            "known. Matching is case-insensitive substring."
                        ),
                    },
                },
            },
            executor=_forget,
            read_only=False,
            preview=_preview_forget,
        ),
        "search_memory": ToolSpec(
            name="search_memory",
            description=(
                "Search long-term memory with an explicit depth. Use it when the "
                "memories already in context were not enough — raise `limit` to dig "
                "further back. Returns memory ids, which `forget` accepts."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "description": f"How many memories to return (max {MAX_SEARCH_LIMIT}).",
                    },
                },
                "required": ["query"],
            },
            executor=_search_memory,
        ),
    }


__all__ = ["registry_tools"]
