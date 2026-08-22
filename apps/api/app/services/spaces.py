"""Spaces: a group of threads with standing context of its own.

A space hands the threads inside it three things, each through an existing
chokepoint rather than a new mechanism: instructions appended to the system
prompt (`agent_loop.resolve_directives`), knowledge files whose retrieval is
scoped to the space (`retrieval.search_evidence`), and a memory shelf
(`memory._active`). This module owns the object itself and the two questions
every one of those callers asks — "which space is this conversation in?" and
"is that space still real?" — so that a deleted or foreign space degrades to
"no space" in one place instead of three.

Workspace-shared, never personal: a space is a relevance boundary, and privacy
stays on the axes ADR 0010 gave it. Deletion is destructive — see
`delete_space` — because the alternative fails toward wider retrieval.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..models import Conversation, MemoryItem, Run, Source, Space
from . import conversations
from .ingestion import purge_source

MAX_NAME_CHARS = 120
#: Bounds the block `resolve_directives` appends to every turn's system prompt.
#: Generous next to what instructions need, tiny next to the context window —
#: the cap exists so one space cannot quietly become half of every prompt.
MAX_INSTRUCTIONS_CHARS = 8_000
#: A ceiling on the workspace, like `folders.MAX_FOLDERS`: the list is fetched
#: whole, rendered whole, and a runaway client should hit a sentence, not a
#: scrollbar of a thousand rows.
MAX_SPACES = 100


class SpaceError(ValueError):
    """A user-facing problem with a space operation."""


def list_spaces(db: Session, *, workspace_id: str) -> List[Space]:
    """Every space in the workspace, in name order."""
    return list(
        db.scalars(
            select(Space)
            .where(Space.workspace_id == workspace_id)
            .order_by(func.lower(Space.name), Space.id)
        )
    )


def get_space(db: Session, *, workspace_id: str, space_id: str) -> Space:
    space = db.scalar(
        select(Space).where(Space.id == space_id, Space.workspace_id == workspace_id)
    )
    if space is None:
        raise SpaceError("No space with that id")
    return space


def _clean_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise SpaceError("A space needs a name")
    if len(name) > MAX_NAME_CHARS:
        raise SpaceError(f"A space name is at most {MAX_NAME_CHARS} characters")
    if "\n" in name or "\r" in name:
        raise SpaceError("A space name is a single line")
    return name


def _clean_instructions(instructions: str) -> str:
    instructions = instructions.strip()
    if len(instructions) > MAX_INSTRUCTIONS_CHARS:
        raise SpaceError(
            f"Space instructions are at most {MAX_INSTRUCTIONS_CHARS} characters"
        )
    return instructions


def _refuse_duplicate(spaces: List[Space], *, name: str, ignore_id: str = "") -> None:
    wanted = name.strip().lower()
    for space in spaces:
        if space.id != ignore_id and space.name.strip().lower() == wanted:
            raise SpaceError(f"A space named “{space.name}” already exists")


def create_space(
    db: Session,
    *,
    workspace_id: str,
    name: str,
    instructions: str = "",
    created_by: str = "",
) -> Space:
    spaces = list_spaces(db, workspace_id=workspace_id)
    if len(spaces) >= MAX_SPACES:
        raise SpaceError(f"A workspace holds at most {MAX_SPACES} spaces")
    clean = _clean_name(name)
    _refuse_duplicate(spaces, name=clean)
    space = Space(
        workspace_id=workspace_id,
        name=clean,
        instructions=_clean_instructions(instructions),
        created_by=created_by,
    )
    db.add(space)
    db.flush()
    return space


def update_space(
    db: Session,
    *,
    workspace_id: str,
    space_id: str,
    name: Optional[str] = None,
    instructions: Optional[str] = None,
) -> Space:
    """Rename and/or rewrite the instructions.

    `None` means "leave it alone"; `""` for `instructions` means "clear them",
    which is a real edit — the next turn in the space simply gets no block.
    """
    space = get_space(db, workspace_id=workspace_id, space_id=space_id)
    if name is not None:
        clean = _clean_name(name)
        _refuse_duplicate(
            list_spaces(db, workspace_id=workspace_id), name=clean, ignore_id=space.id
        )
        space.name = clean
    if instructions is not None:
        space.instructions = _clean_instructions(instructions)
    db.flush()
    return space


@dataclass
class SpaceTeardown:
    """What a space deletion removed, for the audit row and the disk sweep."""

    thread_count: int = 0
    source_count: int = 0
    memory_count: int = 0
    #: `object_key` of every purged source — the files to unlink once the
    #: transaction has held, never before.
    object_keys: List[str] = field(default_factory=list)


def delete_space(db: Session, *, workspace_id: str, space_id: str) -> SpaceTeardown:
    """Delete a space and everything scoped to it. Does not commit.

    Destructive on purpose, where `folders.delete_folder` refuses: a folder's
    documents live happily at the top level, but a space's contents have no
    "outside" to return to. Re-labelling its sources `space_id = ""` would drop
    them into the workspace library — retrievable in every general chat, which
    is precisely what scoping them promised would not happen — and its
    memories would go from "recalled here" to "recalled everywhere". The one
    direction scoping may never fail toward is wider, so the contents go with
    the container, and the route's confirmation says so in words.

    Children first, the space row last, so a crash mid-way leaves a delete
    that can simply be run again.
    """
    space = get_space(db, workspace_id=workspace_id, space_id=space_id)
    teardown = SpaceTeardown()

    thread_ids = list(
        db.scalars(
            select(Conversation.id).where(
                Conversation.workspace_id == workspace_id,
                Conversation.space_id == space.id,
            )
        )
    )
    for conversation_id in thread_ids:
        conversations.purge(db, workspace_id=workspace_id, conversation_id=conversation_id)
    teardown.thread_count = len(thread_ids)

    source_ids = list(
        db.scalars(
            select(Source.id).where(
                Source.workspace_id == workspace_id,
                Source.space_id == space.id,
                Source.deleted_at.is_(None),
            )
        )
    )
    for source_id in source_ids:
        purged = purge_source(db, workspace_id=workspace_id, source_id=source_id)
        if purged is not None:
            teardown.source_count += 1
            teardown.object_keys.append(purged.object_key)

    # Hard delete, not the user-facing tombstone: `status="deleted"` exists so
    # a person's "forget that" is auditable, but these rows are structural
    # casualties — already unreachable to recall the moment the space is gone.
    # execute() on a DML statement returns a CursorResult at runtime; the base
    # Result type mypy infers has no `rowcount`, so name the runtime type.
    deleted_memories = cast(
        "CursorResult[Any]",
        db.execute(
            delete(MemoryItem).where(
                MemoryItem.workspace_id == workspace_id,
                MemoryItem.space_id == space.id,
            )
        ),
    )
    teardown.memory_count = deleted_memories.rowcount or 0

    db.delete(space)
    return teardown


def counts_for(db: Session, *, workspace_id: str) -> Dict[str, Tuple[int, int]]:
    """`{space_id: (threads, live sources)}` for the list view, in two queries."""
    counts: Dict[str, Tuple[int, int]] = {}
    thread_rows = db.execute(
        select(Conversation.space_id, func.count())
        .where(
            Conversation.workspace_id == workspace_id,
            Conversation.space_id != "",
        )
        .group_by(Conversation.space_id)
    )
    for space_id, threads in thread_rows:
        counts[space_id] = (int(threads), 0)
    source_rows = db.execute(
        select(Source.space_id, func.count())
        .where(
            Source.workspace_id == workspace_id,
            Source.space_id != "",
            Source.deleted_at.is_(None),
        )
        .group_by(Source.space_id)
    )
    for space_id, sources in source_rows:
        threads, _ = counts.get(space_id, (0, 0))
        counts[space_id] = (threads, int(sources))
    return counts


def space_id_for_conversation(
    db: Session, *, workspace_id: str, conversation_id: str
) -> str:
    """The space this conversation is in, or "" — and "" for every failure.

    "" rather than an error for a missing conversation, a conversation with no
    space, or a `space_id` whose Space row is gone or belongs elsewhere: the
    callers are a turn's retrieval and recall scope, where the degraded answer
    must always be "general scope", never a failed turn. The Space row is
    re-proved under the caller's workspace rather than trusted, the same
    belt-and-braces `resolve_directives` applies to `run.agent_id`.
    """
    if not conversation_id:
        return ""
    row = db.scalar(
        select(Conversation.space_id).where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == workspace_id,
        )
    )
    if not row:
        return ""
    live = db.scalar(
        select(Space.id).where(Space.id == row, Space.workspace_id == workspace_id)
    )
    return live or ""


def for_run(db: Session, run: Run) -> Optional[Space]:
    """The Space a run's conversation is in, workspace-proved, or None."""
    space_id = space_id_for_conversation(
        db, workspace_id=run.workspace_id, conversation_id=run.conversation_id
    )
    if not space_id:
        return None
    return db.scalar(
        select(Space).where(Space.id == space_id, Space.workspace_id == run.workspace_id)
    )


def space_block(space: Space) -> str:
    """The instructions block `resolve_directives` appends for a space turn."""
    return (
        f"This conversation is part of the “{space.name}” space. "
        "Standing instructions for this space:\n\n"
        f"{space.instructions.strip()}"
    )
