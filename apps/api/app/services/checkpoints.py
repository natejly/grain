"""Before-state capture and restore for a run's writes — the undo substrate.

Every write-capable agent tool executes through
`agent_loop.execute_agent_tool_call`, and this module is what that chokepoint
calls on either side of the executor:

- `capture_before` reads the state a tool is about to change (the document's
  current content, the board's full snapshot, the project file's bytes) into a
  `PendingCapture`, *before* anything runs;
- `record_checkpoint` finalizes it after a successful execution (creations
  learn the created id here, by diffing the id set captured before) and adds a
  `RunCheckpoint` row for the run's undo trail.

The capture is deliberately NOT built on `AgentToolCall.arguments_json`: that
column is truncated to 4000 characters at write time, which makes it an
incomplete record of exactly the large writes an undo matters most for. State
is read from the rows themselves, and a capture that would exceed
`MAX_BEFORE_CHARS` is recorded `reversible=False` rather than clipped into a
restore that would corrupt.

Writes whose effects leave the workspace — MCP tools, sandbox execution,
uploads into a sandbox, SQL against a connected database, integrations — are
recorded as kind `external` with `reversible=False`: the row exists so the
undo endpoint can *say* what it cannot restore, instead of silently skipping.

Capture must never break the tool call it shadows: `execute_agent_tool_call`
wraps both entry points in a swallow-and-log, and the per-family helpers here
degrade to "no checkpoint" when the target cannot even be resolved (the
executor is about to fail the same resolution and mutate nothing).

`revert_run` applies a run's checkpoints newest-first so a create undone after
the writes into it works (files restored, then the created project deleted).
Documents are restored through `documents.replace_content`, which snapshots
the pre-undo content as a new `DocumentVersion` — an undo adds history, never
rewrites it. Some of the artifact services this delegates to commit
internally (the house style of `services/artifacts`); the route's final commit
covers everything else.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..models import (
    Board,
    BoardCard,
    BoardColumn,
    Dashboard,
    DashboardTemplate,
    Document,
    MemoryItem,
    Project,
    ProjectFile,
    Run,
    RunCheckpoint,
    Source,
)
from . import memory as memory_service
from .artifacts import boards, documents, todos
from .llm_tools import ToolContext
from .projects import store as project_store

# The dashboards package pulls in the workflow compiler, which imports
# agent_loop — and agent_loop imports this module. Deferred to call time to
# break the cycle; by then the app is fully loaded.


def _dashboard_store():
    from .dashboards import store

    return store

logger = logging.getLogger(__name__)

#: Generous ceiling for one checkpoint's serialized before-state. A capture
#: bigger than this is marked irreversible rather than clipped — a truncated
#: snapshot restored whole would be corruption wearing an undo button.
MAX_BEFORE_CHARS = 200_000


@dataclass
class PendingCapture:
    """One tool call's before-state, between capture and record."""

    kind: str
    reversible: bool = True
    before: Optional[Dict[str, Any]] = None
    #: For creations: runs after the executor to learn the created id by
    #: diffing against the id set captured before. Returns the final `before`
    #: dict, or None to record nothing (the create visibly did not happen).
    finalize: Optional[Callable[[Session], Optional[Dict[str, Any]]]] = None
    #: Id sets captured before execution, for the finalizers above.
    seen: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Capture


def _text(args: Dict[str, Any], key: str, default: str = "") -> str:
    value = args.get(key, default)
    return value.strip() if isinstance(value, str) else default


def _document_ids(db: Session, workspace_id: str) -> set[str]:
    return set(
        db.scalars(select(Document.id).where(Document.workspace_id == workspace_id))
    )


def _capture_create_document(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> PendingCapture:
    seen = _document_ids(db, context.workspace_id)

    def finalize(db: Session) -> Optional[Dict[str, Any]]:
        created = _document_ids(db, context.workspace_id) - seen
        if not created:
            return None
        return {"existed": False, "document_id": sorted(created)[0]}

    return PendingCapture(kind="document", finalize=finalize)


def _capture_edit_document(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> Optional[PendingCapture]:
    # Mirrors artifacts/tools._document_id: the id the model named, or the
    # document open beside the thread when no title was given either.
    document_id = _text(args, "document_id") or (
        context.document_id if not _text(args, "title") else ""
    )
    document = documents.resolve(
        db,
        workspace_id=context.workspace_id,
        document_id=document_id,
        title=_text(args, "title"),
    )
    return PendingCapture(
        kind="document",
        before={
            "existed": True,
            "document_id": document.id,
            "title": document.title,
            "kind": document.kind,
            "content": document.content,
        },
    )


def _board_state(db: Session, board: Board) -> Dict[str, Any]:
    """Full-fidelity board snapshot: ids, positions and done timestamps kept,
    so a restore reproduces the board rather than an approximation of it."""
    return {
        "id": board.id,
        "name": board.name,
        "columns": [
            {"id": column.id, "name": column.name, "position": column.position}
            for column in boards.columns_for(db, board.id)
        ],
        "cards": [
            {
                "id": card.id,
                "column_id": card.column_id,
                "title": card.title,
                "body": card.body,
                "labels_json": card.labels_json,
                "position": card.position,
                "done_at": card.done_at.isoformat() if card.done_at else None,
            }
            for card in boards.cards_for(db, board.id)
        ],
    }


def _board_ids(db: Session, workspace_id: str) -> set[str]:
    return set(db.scalars(select(Board.id).where(Board.workspace_id == workspace_id)))


def _capture_create_board(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> PendingCapture:
    seen = _board_ids(db, context.workspace_id)

    def finalize(db: Session) -> Optional[Dict[str, Any]]:
        created = _board_ids(db, context.workspace_id) - seen
        if not created:
            return None
        return {"existed": False, "board_id": sorted(created)[0]}

    return PendingCapture(kind="board", finalize=finalize)


def _capture_board_write(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> Optional[PendingCapture]:
    board = boards.resolve(
        db,
        workspace_id=context.workspace_id,
        board_id=_text(args, "board_id"),
        name=_text(args, "board") or _text(args, "name"),
    )
    return PendingCapture(
        kind="board",
        before={
            "existed": True,
            "board_id": board.id,
            "board": _board_state(db, board),
        },
    )


def _capture_add_todo(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> Optional[PendingCapture]:
    board = todos.resolve_list(
        db,
        workspace_id=context.workspace_id,
        list_id=_text(args, "list_id"),
        name=_text(args, "list"),
    )
    return PendingCapture(
        kind="todo",
        before={
            "existed": True,
            "board_id": board.id,
            "board": _board_state(db, board),
        },
    )


def _capture_todo_check(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> Optional[PendingCapture]:
    card = todos.find_item(
        db,
        workspace_id=context.workspace_id,
        item=_text(args, "item"),
        list_name=_text(args, "list"),
    )
    board = boards.get_board(
        db, workspace_id=context.workspace_id, board_id=card.board_id
    )
    return PendingCapture(
        kind="todo",
        before={
            "existed": True,
            "board_id": board.id,
            "board": _board_state(db, board),
        },
    )


def _project_ids(db: Session, workspace_id: str) -> set[str]:
    return set(
        db.scalars(select(Project.id).where(Project.workspace_id == workspace_id))
    )


def _capture_create_project(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> PendingCapture:
    seen = _project_ids(db, context.workspace_id)

    def finalize(db: Session) -> Optional[Dict[str, Any]]:
        created = _project_ids(db, context.workspace_id) - seen
        if not created:
            return None
        return {"existed": False, "project_id": sorted(created)[0]}

    return PendingCapture(kind="project_file", finalize=finalize)


def _resolve_project(db: Session, context: ToolContext, args: Dict[str, Any]) -> Project:
    """Mirrors projects/tools._target: named project, else the one on screen."""
    project_id = _text(args, "project_id")
    name = _text(args, "project")
    if not project_id and not name:
        project_id = context.project_id
    return project_store.resolve(
        db, workspace_id=context.workspace_id, project_id=project_id, name=name
    )


def _capture_project_file(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> Optional[PendingCapture]:
    project = _resolve_project(db, context, args)
    path = project_store.normalize_path(_text(args, "path"))
    file = project_store.find_file(db, project_id=project.id, path=path)
    return PendingCapture(
        kind="project_file",
        before={
            "existed": True,
            "project_id": project.id,
            "files": {path: file.content if file is not None else None},
        },
    )


def _capture_bib_add(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> Optional[PendingCapture]:
    from .projects import bibliography

    project = _resolve_project(db, context, args)
    fields = args.get("fields")
    plan = bibliography.plan_add_entry(
        db,
        project=project,
        entry_type=_text(args, "entry_type"),
        key=_text(args, "key"),
        fields={str(k): str(v) for k, v in fields.items()}
        if isinstance(fields, dict)
        else {},
        path=_text(args, "path"),
    )
    file = project_store.find_file(db, project_id=project.id, path=plan.path)
    return PendingCapture(
        kind="project_file",
        before={
            "existed": True,
            "project_id": project.id,
            "files": {plan.path: file.content if file is not None else None},
        },
    )


def _dashboard_ids(db: Session, workspace_id: str) -> set[str]:
    return set(
        db.scalars(select(Dashboard.id).where(Dashboard.workspace_id == workspace_id))
    )


def _capture_create_dashboard(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> PendingCapture:
    seen = _dashboard_ids(db, context.workspace_id)

    def finalize(db: Session) -> Optional[Dict[str, Any]]:
        created = _dashboard_ids(db, context.workspace_id) - seen
        if not created:
            return None
        return {"existed": False, "dashboard_id": sorted(created)[0]}

    return PendingCapture(kind="dashboard", finalize=finalize)


def _capture_update_dashboard(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> Optional[PendingCapture]:
    # Mirrors dashboards/tools._find_dashboard: id, name, or the one on screen.
    dashboard_id = _text(args, "dashboard_id")
    name = _text(args, "dashboard")
    if not dashboard_id and not name:
        dashboard_id = context.dashboard_id
    query = select(Dashboard).where(Dashboard.workspace_id == context.workspace_id)
    if dashboard_id:
        query = query.where(Dashboard.id == dashboard_id)
    elif name:
        query = query.where(Dashboard.name == name)
    else:
        return None
    dashboard = db.scalar(query)
    if dashboard is None:
        return None
    return PendingCapture(
        kind="dashboard",
        before={
            "existed": True,
            "dashboard_id": dashboard.id,
            "name": dashboard.name,
            "description": dashboard.description,
            "dataset_id": dashboard.dataset_id,
            "spec_json": dashboard.spec_json,
            "bindings_json": dashboard.bindings_json,
            "template_id": dashboard.template_id,
        },
    )


def _template_ids(db: Session, workspace_id: str) -> set[str]:
    return set(
        db.scalars(
            select(DashboardTemplate.id).where(
                DashboardTemplate.workspace_id == workspace_id
            )
        )
    )


def _capture_create_dashboard_template(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> PendingCapture:
    seen = _template_ids(db, context.workspace_id)

    def finalize(db: Session) -> Optional[Dict[str, Any]]:
        created = _template_ids(db, context.workspace_id) - seen
        if not created:
            return None
        return {"existed": False, "template": True, "template_id": sorted(created)[0]}

    return PendingCapture(kind="dashboard", finalize=finalize)


def _memory_row(item: MemoryItem) -> Dict[str, Any]:
    return {
        "id": item.id,
        "status": item.status,
        "normalized_key": item.normalized_key,
        "content": item.content,
        "importance": item.importance,
        "entity_names_json": item.entity_names_json,
    }


def _capture_remember(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> Optional[PendingCapture]:
    content = memory_service.normalize_memory_content(_text(args, "content"))
    if not content:
        return None
    kind = _text(args, "kind").lower() or "fact"
    if kind not in memory_service.REMEMBER_KINDS:
        kind = "fact"
    owner_id = memory_service.memory_owner(
        db, context.conversation_id, context.user_id
    )
    space_id = memory_service.memory_space(db, context.conversation_id)
    key = memory_service._content_key(content)

    def lookup(db: Session) -> Optional[MemoryItem]:
        return db.scalar(
            select(MemoryItem).where(
                MemoryItem.workspace_id == context.workspace_id,
                MemoryItem.owner_id == owner_id,
                MemoryItem.space_id == space_id,
                MemoryItem.kind == kind,
                MemoryItem.normalized_key == key,
            )
        )

    existing = lookup(db)
    if existing is not None:
        return PendingCapture(
            kind="memory",
            before={"existed": True, "item": _memory_row(existing)},
        )

    def finalize(db: Session) -> Optional[Dict[str, Any]]:
        created = lookup(db)
        if created is None:
            return None
        return {"existed": False, "memory_id": created.id}

    return PendingCapture(kind="memory", finalize=finalize)


def _capture_forget(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> Optional[PendingCapture]:
    targets, error = memory_service.resolve_forget_targets(
        db,
        workspace_id=context.workspace_id,
        memory_id=_text(args, "memory_id") or None,
        content=_text(args, "content") or None,
        viewer_id=context.user_id,
        space_id=context.space_id,
    )
    if error or not targets:
        return None
    return PendingCapture(
        kind="memory",
        before={
            "existed": True,
            "forgotten": [
                {
                    "id": item.id,
                    "status": item.status,
                    "normalized_key": item.normalized_key,
                }
                for item in targets
            ],
        },
    )


def _source_ids(db: Session, workspace_id: str) -> set[str]:
    return set(
        db.scalars(select(Source.id).where(Source.workspace_id == workspace_id))
    )


def _capture_sandbox_download(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> PendingCapture:
    seen = _source_ids(db, context.workspace_id)

    def finalize(db: Session) -> Optional[Dict[str, Any]]:
        created = _source_ids(db, context.workspace_id) - seen
        if not created:
            return None
        return {"existed": False, "source_ids": sorted(created)}

    return PendingCapture(kind="source", finalize=finalize)


_CAPTURES: Dict[str, Callable[[Session, ToolContext, Dict[str, Any]], Optional[PendingCapture]]] = {
    # Documents
    "create_document": _capture_create_document,
    "edit_document": _capture_edit_document,
    # Boards
    "create_board": _capture_create_board,
    "board_add_card": _capture_board_write,
    "board_move_card": _capture_board_write,
    "board_update_card": _capture_board_write,
    "board_delete_card": _capture_board_write,
    "board_add_column": _capture_board_write,
    "board_rename_column": _capture_board_write,
    "board_delete_column": _capture_board_write,
    "board_reorder_columns": _capture_board_write,
    "board_reorder_card": _capture_board_write,
    # Todos (a one-column board; same restore family, honest kind)
    "add_todo": _capture_add_todo,
    "todo_check": _capture_todo_check,
    # Project files
    "create_project": _capture_create_project,
    "fs_write": _capture_project_file,
    "fs_edit": _capture_project_file,
    "fs_delete": _capture_project_file,
    "bib_add": _capture_bib_add,
    # Dashboards
    "create_dashboard": _capture_create_dashboard,
    "update_dashboard": _capture_update_dashboard,
    "create_dashboard_template": _capture_create_dashboard_template,
    "bind_dashboard_template": _capture_create_dashboard,
    # Memory
    "remember": _capture_remember,
    "forget": _capture_forget,
    # Sandbox downloads create a workspace Source; reversible by deleting it.
    "sandbox_download": _capture_sandbox_download,
}


def capture_before(
    db: Session, context: ToolContext, name: str, arguments: Dict[str, Any]
) -> Optional[PendingCapture]:
    """The before-state of what `name` is about to do, or an honest fallback.

    Unknown write tools — every MCP tool, sandbox execution, custom sandbox
    tools, sql_execute, integrations — become an `external` irreversible
    checkpoint: recorded so the undo can say what it cannot restore. A known
    tool whose target cannot be resolved returns None (the executor is about
    to fail the same resolution and will mutate nothing).
    """
    helper = _CAPTURES.get(name)
    if helper is None:
        return PendingCapture(kind="external", reversible=False)
    try:
        return helper(db, context, arguments)
    except Exception:
        # Resolution refused (BoardError, DocumentError, ProjectError, ...) or
        # a capture bug: the tool call must proceed either way.
        logger.info("checkpoint capture skipped for %s", name, exc_info=True)
        return None


def record_checkpoint(
    db: Session, *, run: Run, tool_call_id: str, name: str, pending: PendingCapture
) -> None:
    """Finalize a capture after a successful execution and add its row.

    Flushes, never commits — `execute_agent_tool_call`'s closing commit is the
    transaction boundary, exactly as it is for the audit row beside it.
    """
    before = pending.before
    if pending.finalize is not None:
        before = pending.finalize(db)
        if before is None:
            # The create this capture waited on visibly did not happen (the
            # executor answered with an error sentence): nothing to undo.
            return
    reversible = pending.reversible
    before_json = ""
    if reversible and before is not None:
        before_json = json.dumps(before)
        if len(before_json) > MAX_BEFORE_CHARS:
            # A clipped snapshot restored whole is corruption; refuse honestly.
            before_json = ""
            reversible = False
    elif reversible:
        reversible = False
    db.add(
        RunCheckpoint(
            workspace_id=run.workspace_id,
            run_id=run.id,
            tool_call_id=tool_call_id,
            tool_name=name,
            kind=pending.kind,
            reversible=reversible,
            before_json=before_json,
        )
    )
    db.flush()


# --------------------------------------------------------------------------
# Restore


def _parse_done_at(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _revert_document(
    db: Session, *, workspace_id: str, actor_id: str, before: Dict[str, Any]
) -> None:
    document_id = str(before.get("document_id") or "")
    if not document_id:
        raise ValueError("checkpoint names no document")
    if not before.get("existed"):
        # Undo a creation by deleting it; already gone is already undone.
        try:
            documents.delete_document(
                db, workspace_id=workspace_id, document_id=document_id
            )
        except documents.DocumentError:
            pass
        return
    existing = db.scalar(
        select(Document).where(
            Document.id == document_id, Document.workspace_id == workspace_id
        )
    )
    content = str(before.get("content") or "")
    if existing is None:
        # The run (or someone since) deleted it; put the row back whole.
        db.add(
            Document(
                id=document_id,
                workspace_id=workspace_id,
                title=str(before.get("title") or "Restored document")[:200],
                kind=str(before.get("kind") or "markdown"),
                content=content,
                created_by=actor_id,
            )
        )
        db.flush()
        return
    # Through replace_content so the pre-undo content becomes a new
    # DocumentVersion: an undo adds history, never rewrites it.
    documents.replace_content(
        db,
        workspace_id=workspace_id,
        document_id=document_id,
        content=content,
        summary="Undo of an agent run",
        created_by=actor_id,
    )


def _revert_board(db: Session, *, workspace_id: str, before: Dict[str, Any]) -> None:
    board_id = str(before.get("board_id") or "")
    if not board_id:
        raise ValueError("checkpoint names no board")
    if not before.get("existed"):
        try:
            boards.delete_board(db, workspace_id=workspace_id, board_id=board_id)
        except boards.BoardError:
            pass
        return
    state = before.get("board") or {}
    board = db.scalar(
        select(Board).where(Board.id == board_id, Board.workspace_id == workspace_id)
    )
    if board is None:
        board = Board(
            id=board_id,
            workspace_id=workspace_id,
            name=str(state.get("name") or "Restored board")[:120],
        )
        db.add(board)
        db.flush()
    board.name = str(state.get("name") or board.name)[:120]
    db.execute(delete(BoardCard).where(BoardCard.board_id == board_id))
    db.execute(delete(BoardColumn).where(BoardColumn.board_id == board_id))
    db.flush()
    for column in state.get("columns") or []:
        db.add(
            BoardColumn(
                id=str(column.get("id")),
                workspace_id=workspace_id,
                board_id=board_id,
                name=str(column.get("name") or "")[:80],
                position=int(column.get("position") or 0),
            )
        )
    db.flush()
    for card in state.get("cards") or []:
        db.add(
            BoardCard(
                id=str(card.get("id")),
                workspace_id=workspace_id,
                board_id=board_id,
                column_id=str(card.get("column_id") or ""),
                title=str(card.get("title") or "")[:300],
                body=str(card.get("body") or ""),
                labels_json=str(card.get("labels_json") or "[]"),
                position=int(card.get("position") or 0),
                done_at=_parse_done_at(card.get("done_at")),
            )
        )
    db.flush()


def _revert_project_file(
    db: Session, *, workspace_id: str, before: Dict[str, Any]
) -> None:
    project_id = str(before.get("project_id") or "")
    if not project_id:
        raise ValueError("checkpoint names no project")
    if not before.get("existed"):
        try:
            project_store.delete_project(
                db, workspace_id=workspace_id, project_id=project_id
            )
        except project_store.ProjectError:
            pass
        return
    project = db.scalar(
        select(Project).where(
            Project.id == project_id, Project.workspace_id == workspace_id
        )
    )
    if project is None:
        raise ValueError("the project no longer exists")
    files = before.get("files") or {}
    for path, content in files.items():
        file = project_store.find_file(db, project_id=project_id, path=path)
        if content is None:
            if file is not None:
                db.delete(file)
        elif file is not None:
            file.content = content
        else:
            db.add(
                ProjectFile(
                    workspace_id=workspace_id,
                    project_id=project_id,
                    path=path,
                    content=content,
                )
            )
    db.flush()


def _revert_dashboard(
    db: Session, *, workspace_id: str, before: Dict[str, Any]
) -> None:
    if not before.get("existed"):
        if before.get("template"):
            template = db.scalar(
                select(DashboardTemplate).where(
                    DashboardTemplate.id == str(before.get("template_id") or ""),
                    DashboardTemplate.workspace_id == workspace_id,
                )
            )
            if template is not None:
                _dashboard_store().delete_template(db, template)
                db.flush()
            return
        dashboard = db.scalar(
            select(Dashboard).where(
                Dashboard.id == str(before.get("dashboard_id") or ""),
                Dashboard.workspace_id == workspace_id,
            )
        )
        if dashboard is not None:
            _dashboard_store().delete_dashboard(db, dashboard)
            db.flush()
        return
    dashboard = db.scalar(
        select(Dashboard).where(
            Dashboard.id == str(before.get("dashboard_id") or ""),
            Dashboard.workspace_id == workspace_id,
        )
    )
    if dashboard is None:
        raise ValueError("the dashboard no longer exists")
    dashboard.name = str(before.get("name") or dashboard.name)[:160]
    dashboard.description = str(before.get("description") or "")
    dashboard.dataset_id = str(before.get("dataset_id") or dashboard.dataset_id)
    dashboard.spec_json = str(before.get("spec_json") or dashboard.spec_json)
    dashboard.bindings_json = str(before.get("bindings_json") or "{}")
    dashboard.template_id = before.get("template_id") or None
    db.flush()


def _revert_memory(db: Session, *, workspace_id: str, before: Dict[str, Any]) -> None:
    if not before.get("existed"):
        # Undo a `remember` that created: tombstone it, exactly as forget does.
        item = db.scalar(
            select(MemoryItem).where(
                MemoryItem.id == str(before.get("memory_id") or ""),
                MemoryItem.workspace_id == workspace_id,
            )
        )
        if item is not None and item.status == "active":
            item.status = "deleted"
            item.normalized_key = memory_service.tombstone_key(db, item)
            item.updated_at = utcnow()
            db.flush()
        return
    if "forgotten" in before:
        # Undo a `forget`: restore each tombstoned row's status and the claim
        # key the tombstone vacated.
        for entry in before.get("forgotten") or []:
            item = db.scalar(
                select(MemoryItem).where(
                    MemoryItem.id == str(entry.get("id") or ""),
                    MemoryItem.workspace_id == workspace_id,
                )
            )
            if item is None:
                continue
            item.status = str(entry.get("status") or "active")
            item.normalized_key = str(entry.get("normalized_key") or item.normalized_key)
            item.updated_at = utcnow()
        db.flush()
        return
    # Undo a `remember` that reinforced/restored an existing row.
    state = before.get("item") or {}
    item = db.scalar(
        select(MemoryItem).where(
            MemoryItem.id == str(state.get("id") or ""),
            MemoryItem.workspace_id == workspace_id,
        )
    )
    if item is None:
        raise ValueError("the memory no longer exists")
    item.status = str(state.get("status") or item.status)
    item.normalized_key = str(state.get("normalized_key") or item.normalized_key)
    item.content = str(state.get("content") or item.content)
    item.importance = int(state.get("importance") or item.importance)
    item.entity_names_json = str(
        state.get("entity_names_json") or item.entity_names_json
    )
    item.updated_at = utcnow()
    db.flush()


def _revert_source(db: Session, *, workspace_id: str, before: Dict[str, Any]) -> None:
    for source_id in before.get("source_ids") or []:
        source = db.scalar(
            select(Source).where(
                Source.id == str(source_id), Source.workspace_id == workspace_id
            )
        )
        if source is not None and source.deleted_at is None:
            source.deleted_at = utcnow()
    db.flush()


def _skip_reason(row: RunCheckpoint) -> str:
    if row.kind == "external":
        return "external effects cannot be undone"
    return "no recorded before-state to restore"


def revert_run(
    db: Session, *, run: Run, actor_id: str, rows: List[RunCheckpoint]
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """Apply `rows` (this run's checkpoints, newest first) and consume them.

    Returns (reverted, skipped). Every row is stamped `reverted_at` whether it
    restored or was skipped — consumption is about the *undo*, which happens
    once, not about each row's individual fortunes. Irreversible rows and rows
    whose restore fails land in `skipped` with a reason; a failure never
    aborts the rest of the undo. Flushes only; the route commits (though the
    document and project services this calls commit internally, per their own
    house style).
    """
    reverted: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []
    moment = utcnow()
    for row in rows:
        row.reverted_at = moment
        if not row.reversible or not row.before_json:
            skipped.append({"tool_name": row.tool_name, "reason": _skip_reason(row)})
            continue
        try:
            before = json.loads(row.before_json)
            if not isinstance(before, dict):
                raise ValueError("malformed checkpoint")
            if row.kind == "document":
                _revert_document(
                    db,
                    workspace_id=run.workspace_id,
                    actor_id=actor_id,
                    before=before,
                )
            elif row.kind in ("board", "todo"):
                _revert_board(db, workspace_id=run.workspace_id, before=before)
            elif row.kind == "project_file":
                _revert_project_file(
                    db, workspace_id=run.workspace_id, before=before
                )
            elif row.kind == "dashboard":
                _revert_dashboard(db, workspace_id=run.workspace_id, before=before)
            elif row.kind == "memory":
                _revert_memory(db, workspace_id=run.workspace_id, before=before)
            elif row.kind == "source":
                _revert_source(db, workspace_id=run.workspace_id, before=before)
            else:
                raise ValueError(f"unknown checkpoint kind {row.kind!r}")
        except Exception as exc:
            logger.warning(
                "checkpoint restore failed for %s", row.tool_name, exc_info=True
            )
            skipped.append(
                {
                    "tool_name": row.tool_name,
                    "reason": f"restore failed: {str(exc)[:200]}",
                }
            )
            continue
        reverted.append({"tool_name": row.tool_name, "kind": row.kind})
    db.flush()
    return reverted, skipped
