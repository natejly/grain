"""Agent tools for documents and boards.

Every write tool carries a `preview`, so the M1 approval card shows the actual
change — a unified diff for a document edit, a sentence for a card move —
instead of a blob of JSON arguments.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from ..llm_tools import ToolContext, ToolResult, ToolSpec
from . import board_columns, boards, documents

MAX_DOCUMENT_TOOL_CHARS = 12_000


def _text(args: Dict[str, Any], key: str, default: str = "") -> str:
    value = args.get(key, default)
    return value if isinstance(value, str) else default


def _labels(args: Dict[str, Any]) -> List[str] | None:
    value = args.get("labels")
    if not isinstance(value, list):
        return None
    return [str(item) for item in value]


# --------------------------------------------------------------------------
# Documents


def _list_documents(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    rows = documents.list_documents(db, workspace_id=context.workspace_id)
    if not rows:
        return ToolResult(content="No documents yet.")
    return ToolResult(
        content=json.dumps(
            {
                "documents": [
                    {
                        "id": row.id,
                        "title": row.title,
                        "kind": row.kind,
                        "characters": len(row.content),
                    }
                    for row in rows
                ]
            }
        )
    )


def _read_document(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        document = documents.resolve(
            db,
            workspace_id=context.workspace_id,
            document_id=_text(args, "document_id"),
            title=_text(args, "title"),
        )
    except documents.DocumentError as exc:
        return ToolResult(content=f"Error: {exc}")
    body = document.content
    truncated = len(body) > MAX_DOCUMENT_TOOL_CHARS
    if truncated:
        body = body[:MAX_DOCUMENT_TOOL_CHARS] + "\n…(truncated)"
    return ToolResult(
        content=json.dumps(
            {
                "id": document.id,
                "title": document.title,
                "kind": document.kind,
                "content": body,
                "truncated": truncated,
            }
        )
    )


def _create_document(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        document = documents.create_document(
            db,
            workspace_id=context.workspace_id,
            title=_text(args, "title"),
            content=_text(args, "content"),
            kind=_text(args, "kind", "markdown") or "markdown",
            created_by=context.user_id,
        )
    except documents.DocumentError as exc:
        return ToolResult(content=f"Error: {exc}")
    return ToolResult(
        content=f"Created “{document.title}” ({document.kind}, id {document.id})."
    )


def _preview_create_document(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> str:
    title = _text(args, "title") or "Untitled"
    kind = _text(args, "kind", "markdown") or "markdown"
    return documents.render_diff("", _text(args, "content"), title=f"{title} ({kind})")


def _edit_document(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        document, diff, count = documents.edit_document(
            db,
            workspace_id=context.workspace_id,
            document_id=_text(args, "document_id"),
            title=_text(args, "title"),
            find=_text(args, "find"),
            replace=_text(args, "replace"),
            replace_all=bool(args.get("replace_all")),
            summary=_text(args, "summary"),
        )
    except documents.DocumentError as exc:
        return ToolResult(content=f"Error: {exc}")
    plural = "occurrence" if count == 1 else "occurrences"
    return ToolResult(
        content=f"Edited “{document.title}” ({count} {plural} replaced).\n\n{diff}"
    )


def _preview_edit_document(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> str:
    try:
        return documents.preview_edit(
            db,
            workspace_id=context.workspace_id,
            document_id=_text(args, "document_id"),
            title=_text(args, "title"),
            find=_text(args, "find"),
            replace=_text(args, "replace"),
            replace_all=bool(args.get("replace_all")),
        )
    except documents.DocumentError as exc:
        return f"This edit will fail: {exc}"


# --------------------------------------------------------------------------
# Boards


def _read_board(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        board = boards.resolve(
            db,
            workspace_id=context.workspace_id,
            board_id=_text(args, "board_id"),
            name=_text(args, "name"),
        )
    except boards.BoardError as exc:
        known = boards.list_boards(db, workspace_id=context.workspace_id)
        names = [item.name for item in known]
        hint = f" Available boards: {', '.join(names)}." if names else ""
        return ToolResult(content=f"Error: {exc}.{hint}")
    return ToolResult(content=json.dumps(boards.snapshot(db, board)))


def _create_board(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    columns = args.get("columns")
    try:
        board = boards.create_board(
            db,
            workspace_id=context.workspace_id,
            name=_text(args, "name"),
            columns=[str(item) for item in columns] if isinstance(columns, list) else None,
            created_by=context.user_id,
        )
    except boards.BoardError as exc:
        return ToolResult(content=f"Error: {exc}")
    names = [column.name for column in boards.columns_for(db, board.id)]
    return ToolResult(
        content=f"Created board “{board.name}” with columns: {', '.join(names)}."
    )


def _preview_create_board(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    columns = args.get("columns")
    names = (
        [str(item) for item in columns]
        if isinstance(columns, list) and columns
        else list(boards.DEFAULT_COLUMNS)
    )
    return f"Create board “{_text(args, 'name')}” with columns: {', '.join(names)}"


def _with_board(db: Session, context: ToolContext, args: Dict[str, Any]):
    return boards.resolve(
        db,
        workspace_id=context.workspace_id,
        board_id=_text(args, "board_id"),
        name=_text(args, "board"),
    )


def _add_card(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        board = _with_board(db, context, args)
        card = boards.add_card(
            db,
            workspace_id=context.workspace_id,
            board=board,
            column=_text(args, "column"),
            title=_text(args, "title"),
            body=_text(args, "body"),
            labels=_labels(args),
        )
    except boards.BoardError as exc:
        return ToolResult(content=f"Error: {exc}")
    return ToolResult(content=f"Added “{card.title}” (id {card.id}).")


def _preview_add_card(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    return (
        f"Add card “{_text(args, 'title')}” to the "
        f"“{_text(args, 'column')}” column"
    )


def _move_card(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        board = _with_board(db, context, args)
        card = boards.move_card(
            db, board=board, card=_text(args, "card"), column=_text(args, "column")
        )
    except boards.BoardError as exc:
        return ToolResult(content=f"Error: {exc}")
    return ToolResult(content=f"Moved “{card.title}” to “{_text(args, 'column')}”.")


def _preview_move_card(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    try:
        board = _with_board(db, context, args)
        card = boards.find_card(db, board=board, card=_text(args, "card"))
        current = boards.resolve_column(db, board, card.column_id)
        target = boards.resolve_column(db, board, _text(args, "column"))
        return f"Move “{card.title}” from “{current.name}” to “{target.name}”"
    except boards.BoardError as exc:
        return f"This move will fail: {exc}"


def _update_card(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        board = _with_board(db, context, args)
        card = boards.update_card(
            db,
            board=board,
            card=_text(args, "card"),
            title=args.get("title") if isinstance(args.get("title"), str) else None,
            body=args.get("body") if isinstance(args.get("body"), str) else None,
            labels=_labels(args),
        )
    except boards.BoardError as exc:
        return ToolResult(content=f"Error: {exc}")
    return ToolResult(content=f"Updated “{card.title}”.")


def _preview_update_card(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    try:
        board = _with_board(db, context, args)
        card = boards.find_card(db, board=board, card=_text(args, "card"))
    except boards.BoardError as exc:
        return f"This update will fail: {exc}"
    changes = []
    if isinstance(args.get("title"), str):
        changes.append(f"title → “{args['title']}”")
    if isinstance(args.get("body"), str):
        changes.append("body rewritten")
    if _labels(args) is not None:
        changes.append(f"labels → {', '.join(_labels(args) or []) or 'none'}")
    return f"Update “{card.title}”: " + ("; ".join(changes) or "no changes")


def _delete_card(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        board = _with_board(db, context, args)
        title = boards.delete_card(db, board=board, card=_text(args, "card"))
    except boards.BoardError as exc:
        return ToolResult(content=f"Error: {exc}")
    return ToolResult(content=f"Deleted “{title}”.")


def _preview_delete_card(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    try:
        board = _with_board(db, context, args)
        card = boards.find_card(db, board=board, card=_text(args, "card"))
    except boards.BoardError as exc:
        return f"This deletion will fail: {exc}"
    return f"Delete card “{card.title}” — this cannot be undone"


# --------------------------------------------------------------------------
# Board columns and ordering


def _index(args: Dict[str, Any], key: str) -> int | None:
    """Models hand back "2" as often as 2; anything else is a real mistake."""
    value = args.get(key)
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        # str.isdigit() is also true for characters int() rejects — superscripts
        # and circled digits — so the conversion, not the test, has the say.
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise boards.BoardError(f"{key} must be a whole number")


def _order(args: Dict[str, Any]) -> List[str]:
    value = args.get("order")
    if not isinstance(value, list) or not value:
        raise boards.BoardError("order must be the full list of column names")
    return [str(item) for item in value]


def _add_column(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        board = _with_board(db, context, args)
        column = board_columns.add_column(
            db,
            workspace_id=context.workspace_id,
            board=board,
            name=_text(args, "name"),
            index=_index(args, "index"),
        )
    except boards.BoardError as exc:
        return ToolResult(content=f"Error: {exc}")
    return ToolResult(
        content=(
            f"Added column “{column.name}”. Columns are now: "
            f"{board_columns.describe_columns(db, board)}."
        )
    )


def _preview_add_column(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    # The preview asks board_columns what it would do rather than guessing, so a
    # call the executor will refuse never renders as a confident before → after.
    try:
        board = _with_board(db, context, args)
        before = board_columns.column_names(db, board)
        name, slot = board_columns.plan_add_column(
            db, board=board, name=_text(args, "name"), index=_index(args, "index")
        )
    except boards.BoardError as exc:
        return f"This column will fail: {exc}"
    after = list(before)
    after.insert(slot, name)
    return f"Columns: {', '.join(before)} → {', '.join(after)}"


def _rename_column(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        board = _with_board(db, context, args)
        before = boards.resolve_column(db, board, _text(args, "column")).name
        column = board_columns.rename_column(
            db, board=board, column=_text(args, "column"), name=_text(args, "name")
        )
    except boards.BoardError as exc:
        return ToolResult(content=f"Error: {exc}")
    return ToolResult(content=f"Renamed column “{before}” to “{column.name}”.")


def _preview_rename_column(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> str:
    try:
        board = _with_board(db, context, args)
        column, cleaned = board_columns.plan_rename_column(
            db, board=board, column=_text(args, "column"), name=_text(args, "name")
        )
    except boards.BoardError as exc:
        return f"This rename will fail: {exc}"
    return f"Rename column “{column.name}” → “{cleaned}”"


def _delete_column(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        board = _with_board(db, context, args)
        name, moved = board_columns.delete_column(
            db,
            board=board,
            column=_text(args, "column"),
            move_cards_to=_text(args, "move_cards_to"),
        )
    except boards.BoardError as exc:
        return ToolResult(content=f"Error: {exc}")
    tail = (
        f" {moved} card{'' if moved == 1 else 's'} moved to "
        f"“{_text(args, 'move_cards_to')}”."
        if moved
        else ""
    )
    return ToolResult(
        content=(
            f"Deleted column “{name}”.{tail} Columns are now: "
            f"{board_columns.describe_columns(db, board)}."
        )
    )


def _preview_delete_column(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> str:
    try:
        board = _with_board(db, context, args)
        column = boards.resolve_column(db, board, _text(args, "column"))
        wanted = _text(args, "move_cards_to").strip()
        held = boards.cards_in(db, column.id)
        if held and not wanted:
            # Said in the preview's own words: the model can fix this by naming a
            # destination, which is more use than repeating the column list.
            return (
                f"This deletion will fail: “{column.name}” still holds "
                f"{len(held)} card(s); name a column to move them to"
            )
        target, destination, cards = board_columns.plan_delete_column(
            db, board=board, column=_text(args, "column"), move_cards_to=wanted
        )
    except boards.BoardError as exc:
        return f"This deletion will fail: {exc}"
    after = [
        item.name for item in boards.columns_for(db, board.id) if item.id != target.id
    ]
    fate = (
        f"; its {len(cards)} card(s) move to “{destination.name}”"
        if cards and destination is not None
        else "; it is empty"
    )
    return f"Delete column “{target.name}”{fate}. Columns: {', '.join(after)}"


def _reorder_columns(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> ToolResult:
    try:
        board = _with_board(db, context, args)
        columns = board_columns.reorder_columns(db, board=board, order=_order(args))
    except boards.BoardError as exc:
        return ToolResult(content=f"Error: {exc}")
    return ToolResult(
        content="Columns are now: " + ", ".join(column.name for column in columns) + "."
    )


def _preview_reorder_columns(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> str:
    try:
        board = _with_board(db, context, args)
        before = board_columns.column_names(db, board)
        wanted = board_columns.plan_reorder_columns(db, board=board, order=_order(args))
    except boards.BoardError as exc:
        return f"This reorder will fail: {exc}"
    after = [column.name for column in wanted]
    return f"Columns: {', '.join(before)} → {', '.join(after)}"


def _reorder_card(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    try:
        board = _with_board(db, context, args)
        index = _index(args, "index")
        if index is None:
            raise boards.BoardError("index is required (0 is the top of the column)")
        card = board_columns.reorder_card(
            db,
            board=board,
            card=_text(args, "card"),
            index=index,
            column=_text(args, "column"),
        )
        column = boards.resolve_column(db, board, card.column_id)
    except boards.BoardError as exc:
        return ToolResult(content=f"Error: {exc}")
    return ToolResult(
        content=(
            f"“{card.title}” is now #{board_columns.card_slot(db, card) + 1} "
            f"in “{column.name}”."
        )
    )


def _preview_reorder_card(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> str:
    try:
        index = _index(args, "index")
        if index is None:
            return "This move will fail: index is required"
        board = _with_board(db, context, args)
        card, target = board_columns.plan_reorder_card(
            db,
            board=board,
            card=_text(args, "card"),
            index=index,
            column=_text(args, "column"),
        )
        current = boards.resolve_column(db, board, card.column_id)
    except boards.BoardError as exc:
        return f"This move will fail: {exc}"
    return (
        f"Move “{card.title}” from “{current.name}” "
        f"#{board_columns.card_slot(db, card) + 1} "
        f"to “{target.name}” #{index + 1}"
    )


_DOC_TARGET = {
    "document_id": {"type": "string", "description": "Document id (or use title)."},
    "title": {"type": "string", "description": "Document title, if no id."},
}
_BOARD_TARGET = {
    "board_id": {"type": "string", "description": "Board id (or use board)."},
    "board": {"type": "string", "description": "Board name, if no id."},
}


def registry_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    return {
        "list_documents": ToolSpec(
            name="list_documents",
            description="List the workspace's documents with their ids and kinds.",
            parameters={"type": "object", "properties": {}},
            executor=_list_documents,
        ),
        "read_document": ToolSpec(
            name="read_document",
            description="Read a document's full text by id or title.",
            parameters={"type": "object", "properties": dict(_DOC_TARGET)},
            executor=_read_document,
        ),
        "create_document": ToolSpec(
            name="create_document",
            description=(
                "Create a document. kind is 'markdown' or 'latex'; both support "
                "LaTeX math in $…$ and $$…$$."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "kind": {"type": "string", "enum": ["markdown", "latex"]},
                },
                "required": ["title", "content"],
            },
            executor=_create_document,
            read_only=False,
            preview=_preview_create_document,
        ),
        "edit_document": ToolSpec(
            name="edit_document",
            description=(
                "Replace an exact string in a document. `find` must appear exactly "
                "once unless replace_all is true — include surrounding text to make "
                "it unique. The user sees a diff and approves before it applies."
            ),
            parameters={
                "type": "object",
                "properties": {
                    **_DOC_TARGET,
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                    "summary": {
                        "type": "string",
                        "description": "Short description of the change.",
                    },
                },
                "required": ["find", "replace"],
            },
            executor=_edit_document,
            read_only=False,
            preview=_preview_edit_document,
        ),
        "read_board": ToolSpec(
            name="read_board",
            description=(
                "Read a kanban board's columns and cards. Omit both arguments when "
                "the workspace has a single board."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "board_id": {"type": "string"},
                    "name": {"type": "string"},
                },
            },
            executor=_read_board,
        ),
        "create_board": ToolSpec(
            name="create_board",
            description="Create a kanban board. Defaults to Todo / In progress / Done.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name"],
            },
            executor=_create_board,
            read_only=False,
            preview=_preview_create_board,
        ),
        "board_add_card": ToolSpec(
            name="board_add_card",
            description="Add a card to a board column.",
            parameters={
                "type": "object",
                "properties": {
                    **_BOARD_TARGET,
                    "column": {"type": "string", "description": "Column name or id."},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["column", "title"],
            },
            executor=_add_card,
            read_only=False,
            preview=_preview_add_card,
        ),
        "board_move_card": ToolSpec(
            name="board_move_card",
            description="Move a card to a different column, by card id or title.",
            parameters={
                "type": "object",
                "properties": {
                    **_BOARD_TARGET,
                    "card": {"type": "string", "description": "Card id or title."},
                    "column": {"type": "string", "description": "Target column."},
                },
                "required": ["card", "column"],
            },
            executor=_move_card,
            read_only=False,
            preview=_preview_move_card,
        ),
        "board_update_card": ToolSpec(
            name="board_update_card",
            description="Change a card's title, body, or labels.",
            parameters={
                "type": "object",
                "properties": {
                    **_BOARD_TARGET,
                    "card": {"type": "string"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "labels": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["card"],
            },
            executor=_update_card,
            read_only=False,
            preview=_preview_update_card,
        ),
        "board_delete_card": ToolSpec(
            name="board_delete_card",
            description="Delete a card from a board.",
            parameters={
                "type": "object",
                "properties": {**_BOARD_TARGET, "card": {"type": "string"}},
                "required": ["card"],
            },
            executor=_delete_card,
            read_only=False,
            preview=_preview_delete_card,
        ),
        "board_add_column": ToolSpec(
            name="board_add_column",
            description=(
                "Add a column to a board. It goes last unless `index` says where "
                "(0 is the leftmost column)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    **_BOARD_TARGET,
                    "name": {"type": "string"},
                    "index": {
                        "type": "integer",
                        "description": "0-based slot among the columns.",
                    },
                },
                "required": ["name"],
            },
            executor=_add_column,
            read_only=False,
            preview=_preview_add_column,
        ),
        "board_rename_column": ToolSpec(
            name="board_rename_column",
            description="Rename a board column. Cards stay where they are.",
            parameters={
                "type": "object",
                "properties": {
                    **_BOARD_TARGET,
                    "column": {"type": "string", "description": "Column name or id."},
                    "name": {"type": "string", "description": "The new name."},
                },
                "required": ["column", "name"],
            },
            executor=_rename_column,
            read_only=False,
            preview=_preview_rename_column,
        ),
        "board_delete_column": ToolSpec(
            name="board_delete_column",
            description=(
                "Delete a board column. A column holding cards is refused unless "
                "move_cards_to names the column that inherits them."
            ),
            parameters={
                "type": "object",
                "properties": {
                    **_BOARD_TARGET,
                    "column": {"type": "string"},
                    "move_cards_to": {
                        "type": "string",
                        "description": "Column that inherits the cards, if any.",
                    },
                },
                "required": ["column"],
            },
            executor=_delete_column,
            read_only=False,
            preview=_preview_delete_column,
        ),
        "board_reorder_columns": ToolSpec(
            name="board_reorder_columns",
            description=(
                "Reorder a board's columns left to right. `order` must list every "
                "column exactly once, by name or id."
            ),
            parameters={
                "type": "object",
                "properties": {
                    **_BOARD_TARGET,
                    "order": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["order"],
            },
            executor=_reorder_columns,
            read_only=False,
            preview=_preview_reorder_columns,
        ),
        "board_reorder_card": ToolSpec(
            name="board_reorder_card",
            description=(
                "Move a card to an exact slot: index 0 is the top of the column. "
                "Omit `column` to reorder within the card's current column."
            ),
            parameters={
                "type": "object",
                "properties": {
                    **_BOARD_TARGET,
                    "card": {"type": "string", "description": "Card id or title."},
                    "index": {"type": "integer", "description": "0-based slot."},
                    "column": {
                        "type": "string",
                        "description": "Target column; defaults to the current one.",
                    },
                },
                "required": ["card", "index"],
            },
            executor=_reorder_card,
            read_only=False,
            preview=_preview_reorder_card,
        ),
    }
