"""Before-state capture and restore for a run's writes — the undo substrate.

Every write-capable agent tool executes through
`agent_loop.execute_agent_tool_call`, and this module is what that chokepoint
calls on either side of the executor:

- `capture_before` reads the state a tool is about to change (the document's
  current content, the board's full snapshot, the project file's bytes) into a
  `PendingCapture`, *before* anything runs;
- `record_checkpoint` finalizes it after a successful execution (creations
  learn the created id from the executor's own `ToolResult.created_ids` — never
  by set-diffing workspace ids, which would attribute a concurrent creation by
  someone else to this run) and adds a `RunCheckpoint` row for the run's undo
  trail. For the destructively-restored families it also snapshots the
  *after*-state, so `revert_run` can refuse to clobber work that landed on the
  same resource after the run.

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
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, cast

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
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
from .llm_tools import ToolContext, ToolResult
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
    #: For creations: runs after the executor to learn the created id from the
    #: executor's own result (`ToolResult.created_ids`). Returns the final
    #: `before` dict, or None to record nothing (the create visibly did not
    #: happen — or did not say what it made, which must not become a guess).
    finalize: Optional[Callable[[Session, ToolResult], Optional[Dict[str, Any]]]] = None
    #: Runs after the executor to snapshot the state the tool left behind.
    #: Stored under `before["after"]`, it is the undo's clobber guard: at
    #: revert time the resource must still look exactly like this, or the
    #: restore is skipped rather than destroying work that landed since.
    after: Optional[Callable[[Session], Optional[Dict[str, Any]]]] = None


def _created_id(result: Optional[ToolResult]) -> str:
    """The one row the executor reported creating, or ''."""
    if result is None or not result.created_ids:
        return ""
    return str(result.created_ids[0])


# --------------------------------------------------------------------------
# Capture


def _text(args: Dict[str, Any], key: str, default: str = "") -> str:
    value = args.get(key, default)
    return value.strip() if isinstance(value, str) else default


def _capture_create_document(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> PendingCapture:
    def finalize(db: Session, result: ToolResult) -> Optional[Dict[str, Any]]:
        document_id = _created_id(result)
        if not document_id:
            return None
        document = db.scalar(
            select(Document).where(
                Document.id == document_id,
                Document.workspace_id == context.workspace_id,
            )
        )
        if document is None:
            return None
        return {
            "existed": False,
            "document_id": document.id,
            # Undoing this create is a hard delete (versions included), so the
            # guard snapshot must prove nobody touched it since.
            "after": {"title": document.title, "content": document.content},
        }

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


def _board_after(
    db: Session, workspace_id: str, board_id: str
) -> Optional[Dict[str, Any]]:
    board = db.scalar(
        select(Board).where(Board.id == board_id, Board.workspace_id == workspace_id)
    )
    if board is None:
        return None
    return {"board": _board_state(db, board)}


def _capture_create_board(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> PendingCapture:
    def finalize(db: Session, result: ToolResult) -> Optional[Dict[str, Any]]:
        board_id = _created_id(result)
        if not board_id:
            return None
        after = _board_after(db, context.workspace_id, board_id)
        if after is None:
            return None
        return {"existed": False, "board_id": board_id, "after": after}

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
        after=lambda db: _board_after(db, context.workspace_id, board.id),
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
    board_id = board.id
    return PendingCapture(
        kind="todo",
        before={
            "existed": True,
            "board_id": board_id,
            "board": _board_state(db, board),
        },
        after=lambda db: _board_after(db, context.workspace_id, board_id),
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
    board_id = board.id
    return PendingCapture(
        kind="todo",
        before={
            "existed": True,
            "board_id": board_id,
            "board": _board_state(db, board),
        },
        after=lambda db: _board_after(db, context.workspace_id, board_id),
    )


def _project_files(db: Session, project_id: str) -> Dict[str, str]:
    """Every file of the project, path → content — the create-undo guard."""
    return {
        file.path: file.content
        for file in db.scalars(
            select(ProjectFile).where(ProjectFile.project_id == project_id)
        )
    }


def _capture_create_project(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> PendingCapture:
    def finalize(db: Session, result: ToolResult) -> Optional[Dict[str, Any]]:
        project_id = _created_id(result)
        if not project_id:
            return None
        project = db.scalar(
            select(Project).where(
                Project.id == project_id,
                Project.workspace_id == context.workspace_id,
            )
        )
        if project is None:
            return None
        return {
            "existed": False,
            "project_id": project_id,
            # Undoing this create deletes the whole project; the guard snapshot
            # is every seeded file, so a file added or edited since refuses it.
            "after": {"files": _project_files(db, project_id)},
        }

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
        after=lambda db: _file_after(db, project.id, path),
    )


def _file_after(db: Session, project_id: str, path: str) -> Dict[str, Any]:
    file = project_store.find_file(db, project_id=project_id, path=path)
    return {"files": {path: file.content if file is not None else None}}


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
        after=lambda db: _file_after(db, project.id, plan.path),
    )


def _dashboard_fields(dashboard: Dashboard) -> Dict[str, Any]:
    return {
        "name": dashboard.name,
        "description": dashboard.description,
        "dataset_id": dashboard.dataset_id,
        "spec_json": dashboard.spec_json,
        "bindings_json": dashboard.bindings_json,
        "template_id": dashboard.template_id,
    }


def _dashboard_after(
    db: Session, workspace_id: str, dashboard_id: str
) -> Optional[Dict[str, Any]]:
    dashboard = db.scalar(
        select(Dashboard).where(
            Dashboard.id == dashboard_id, Dashboard.workspace_id == workspace_id
        )
    )
    if dashboard is None:
        return None
    return _dashboard_fields(dashboard)


def _capture_create_dashboard(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> PendingCapture:
    def finalize(db: Session, result: ToolResult) -> Optional[Dict[str, Any]]:
        dashboard_id = _created_id(result)
        if not dashboard_id:
            return None
        after = _dashboard_after(db, context.workspace_id, dashboard_id)
        if after is None:
            return None
        return {"existed": False, "dashboard_id": dashboard_id, "after": after}

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
    dashboard_id = dashboard.id
    return PendingCapture(
        kind="dashboard",
        before={
            "existed": True,
            "dashboard_id": dashboard_id,
            **_dashboard_fields(dashboard),
        },
        after=lambda db: _dashboard_after(db, context.workspace_id, dashboard_id),
    )


def _capture_create_dashboard_template(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> PendingCapture:
    def finalize(db: Session, result: ToolResult) -> Optional[Dict[str, Any]]:
        template_id = _created_id(result)
        if not template_id:
            return None
        template = db.scalar(
            select(DashboardTemplate).where(
                DashboardTemplate.id == template_id,
                DashboardTemplate.workspace_id == context.workspace_id,
            )
        )
        if template is None:
            return None
        return {
            "existed": False,
            "template": True,
            "template_id": template_id,
            "after": {
                "name": template.name,
                "description": template.description,
                "required_columns_json": template.required_columns_json,
                "spec_json": template.spec_json,
            },
        }

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

    def finalize(db: Session, result: ToolResult) -> Optional[Dict[str, Any]]:
        # No id from the result needed here: the (owner, space, kind, key)
        # lookup is exact, so it can only name the row this call upserted.
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


def _capture_sandbox_download(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> PendingCapture:
    def finalize(db: Session, result: ToolResult) -> Optional[Dict[str, Any]]:
        created = [str(source_id) for source_id in (result.created_ids if result else [])]
        if not created:
            return None
        return {"existed": False, "source_ids": created}

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
    db: Session,
    *,
    run: Run,
    tool_call_id: str,
    name: str,
    pending: PendingCapture,
    result: ToolResult,
) -> None:
    """Finalize a capture after a successful execution and add its row.

    Flushes, never commits — `execute_agent_tool_call`'s closing commit is the
    transaction boundary, exactly as it is for the audit row beside it.
    """
    before = pending.before
    if pending.finalize is not None:
        before = pending.finalize(db, result)
        if before is None:
            # The create this capture waited on visibly did not happen (the
            # executor answered with an error sentence, or did not report what
            # it made): nothing this undo could honestly claim.
            return
    if before is not None and pending.after is not None:
        try:
            after = pending.after(db)
        except Exception:
            # A guard snapshot is a convenience on top of a convenience; a
            # failure records the checkpoint unguarded rather than losing it.
            logger.info("checkpoint after-state skipped for %s", name, exc_info=True)
            after = None
        if after is not None:
            before = {**before, "after": after}
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


def _changed_since(current: Any, recorded: Any) -> bool:
    """Does the live state no longer match the checkpoint's after-snapshot?

    Both sides are JSON-safe dicts; the round-trip normalizes the live side the
    same way the snapshot was normalized when it was stored.
    """
    return bool(json.loads(json.dumps(current)) != recorded)


def _refuse_if_changed(current: Any, recorded: Any, what: str) -> None:
    """The clobber guard: a restore only applies to the state the run left.

    `recorded` is the checkpoint's after-snapshot, or None for a row written
    before the guard existed (restore proceeds unguarded, as it always did).
    A mismatch means later runs or humans wrote to the same resource; restoring
    over them would destroy work this run never touched, so the row is skipped
    with an honest reason instead.
    """
    if recorded is None:
        return
    if _changed_since(current, recorded):
        raise ValueError(
            f"the {what} changed after this run; skipped to protect the later edits"
        )


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
        # Undo a creation by deleting it; already gone is already undone. The
        # delete is hard (versions included), so it only applies while the
        # document still looks exactly as the run left it.
        created = db.scalar(
            select(Document).where(
                Document.id == document_id, Document.workspace_id == workspace_id
            )
        )
        if created is not None:
            _refuse_if_changed(
                {"title": created.title, "content": created.content},
                before.get("after"),
                "document",
            )
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
        created = db.scalar(
            select(Board).where(
                Board.id == board_id, Board.workspace_id == workspace_id
            )
        )
        if created is not None:
            _refuse_if_changed(
                {"board": _board_state(db, created)}, before.get("after"), "board"
            )
        try:
            boards.delete_board(db, workspace_id=workspace_id, board_id=board_id)
        except boards.BoardError:
            pass
        return
    state = before.get("board") or {}
    board = db.scalar(
        select(Board).where(Board.id == board_id, Board.workspace_id == workspace_id)
    )
    # The restore below is delete-and-recreate — the one family whose restore
    # destroys whatever is on the board NOW — so it only applies while the
    # board still matches the state this run left it in.
    if board is not None:
        _refuse_if_changed(
            {"board": _board_state(db, board)}, before.get("after"), "board"
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
        # Undo a create by deleting the whole project — but only while it still
        # holds exactly the files the create seeded. A file added or edited
        # since belongs to someone else's work, not to this run's undo.
        created = db.scalar(
            select(Project).where(
                Project.id == project_id, Project.workspace_id == workspace_id
            )
        )
        if created is not None:
            _refuse_if_changed(
                {"files": _project_files(db, project_id)},
                before.get("after"),
                "project",
            )
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
    # Guard before any write, so a refusal leaves every file untouched.
    after_files = (before.get("after") or {}).get("files")
    if isinstance(after_files, dict):
        for path in files:
            file = project_store.find_file(db, project_id=project_id, path=path)
            current = file.content if file is not None else None
            if path in after_files and current != after_files[path]:
                raise ValueError(
                    f"the file {path} changed after this run; "
                    "skipped to protect the later edits"
                )
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
                _refuse_if_changed(
                    {
                        "name": template.name,
                        "description": template.description,
                        "required_columns_json": template.required_columns_json,
                        "spec_json": template.spec_json,
                    },
                    before.get("after"),
                    "dashboard template",
                )
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
            _refuse_if_changed(
                _dashboard_fields(dashboard), before.get("after"), "dashboard"
            )
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
    _refuse_if_changed(_dashboard_fields(dashboard), before.get("after"), "dashboard")
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

    Returns (reverted, skipped). Each row is consumed by a conditional UPDATE
    on `reverted_at IS NULL` — the `consume_email_token` shape — *immediately
    before its restore*: two concurrent undos of the same run cannot both
    apply a row (the loser sees rowcount 0 and skips), and a crash mid-undo
    leaves at most the row in flight stamped, with every later row untouched
    for a retry to pick up. Irreversible rows and rows whose restore fails
    land in `skipped` with a reason but are still consumed — consumption is
    about the undo, which happens once, not about each row's fortunes. A
    failure never aborts the rest of the undo. Flushes only; the route commits
    (though the document and project services this calls commit internally,
    per their own house style).
    """
    reverted: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []
    moment = utcnow()
    for row in rows:
        claimed = cast(
            "CursorResult[Any]",
            db.execute(
                update(RunCheckpoint)
                .where(
                    RunCheckpoint.id == row.id,
                    RunCheckpoint.reverted_at.is_(None),
                )
                .values(reverted_at=moment)
            ),
        ).rowcount
        if claimed != 1:
            skipped.append(
                {
                    "tool_name": row.tool_name,
                    "reason": "already consumed by a concurrent undo",
                }
            )
            continue
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
