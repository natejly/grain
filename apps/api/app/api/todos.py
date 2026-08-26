"""Todo lists, which are one-column boards read as checkboxes.

Deliberately thin, and deliberately not a parallel /api/boards. Only three of
these routes do something the board routes could not: `GET /api/todos` collects
every list-shaped board so a person can see what is outstanding without opening
any of them, `POST /api/todos` makes a board with the single column that shape
requires, and `PATCH /api/todos/items/{item_id}` ticks an item off *without a
board id* — which is the interaction a kanban has no way to offer. Everything
else about a list is board machinery, reached through the board routes.

See services/artifacts/todos.py for why a list is a view rather than an entity.
"""
from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Board, BoardCard
from ..schemas import (
    TodoItemCreate,
    TodoItemOut,
    TodoItemUpdate,
    TodoListCreate,
    TodoListOut,
)
from ..services import coworking
from ..services.artifacts import boards, todos
from ..services.audit import record_audit

router = APIRouter(prefix="/api/todos", tags=["todos"])


def _list_out(db: Session, board: Board) -> TodoListOut:
    return TodoListOut.model_validate(todos.snapshot(db, board))


def _item_out(card: BoardCard) -> TodoItemOut:
    return TodoItemOut.model_validate(todos.item_snapshot(card))


def _load_item(db: Session, actor: Actor, item_id: str) -> BoardCard:
    try:
        return todos.get_item(db, workspace_id=actor.workspace_id, item_id=item_id)
    except boards.BoardError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("", response_model=List[TodoListOut])
def list_todos(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[TodoListOut]:
    """Every list in the workspace, with its items. The "without opening a
    board" half of the feature — the other half is the PATCH below."""
    return [
        _list_out(db, board)
        for board in todos.list_todo_lists(db, workspace_id=actor.workspace_id)
    ]


@router.post("", response_model=TodoListOut, status_code=201)
def create_todo_list(
    payload: TodoListCreate,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> TodoListOut:
    try:
        board = todos.create_todo_list(
            db,
            workspace_id=actor.workspace_id,
            name=payload.name,
            created_by=actor.user_id,
        )
    except boards.BoardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="todo_list.created",
        resource_type="board",
        resource_id=board.id,
        detail={"name": board.name},
    )
    db.commit()
    return _list_out(db, board)


@router.post("/{list_id}/items", response_model=TodoListOut, status_code=201)
def add_item(
    list_id: str,
    payload: TodoItemCreate,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> TodoListOut:
    """Add an item. No column argument: a list has exactly one, by definition."""
    try:
        board = todos.resolve_list(db, workspace_id=actor.workspace_id, list_id=list_id)
        todos.add_item(
            db,
            workspace_id=actor.workspace_id,
            board=board,
            title=payload.title,
            body=payload.body,
        )
    except boards.BoardError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _list_out(db, board)


@router.patch("/items/{item_id}", response_model=TodoItemOut)
def update_item(
    item_id: str,
    payload: TodoItemUpdate,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> TodoItemOut:
    """Tick an item off, or rename it. The item id is the whole address.

    Returns the item rather than the list it came from, unlike the board routes,
    which return the whole board because a drag rearranges cards the client did
    not touch. A checkbox changes exactly one thing, and re-sending the list
    around every tick would make a long list feel slower the longer it got.
    """
    card = _load_item(db, actor, item_id)
    if payload.title is not None or payload.body is not None:
        todos.update_item(db, card=card, title=payload.title, body=payload.body)
    if payload.done is not None:
        todos.set_done(db, card=card, done=payload.done)
        record_audit(
            db,
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            action="todo_item.checked" if payload.done else "todo_item.unchecked",
            resource_type="board_card",
            resource_id=card.id,
            detail={"title": card.title, "list_id": card.board_id},
        )
        db.commit()
        # Best-effort: the tick is committed; the coworking stream's frame must
        # not turn a finished PATCH into a 409 if the sequence race is lost
        # twice. A missed frame costs one refresh, not a fact.
        for _ in (1, 2):
            try:
                coworking.append_workspace_event(
                    db,
                    workspace_id=actor.workspace_id,
                    event_type="todo.checked" if payload.done else "todo.reopened",
                    payload={
                        "item_id": card.id,
                        "list_id": card.board_id,
                        "title": card.title,
                        "actor_id": actor.user_id,
                        "actor_kind": "user",
                        "actor_label": actor.user_name,
                    },
                )
                db.commit()
                break
            except IntegrityError:
                db.rollback()
    return _item_out(card)


@router.delete("/items/{item_id}", status_code=204)
def delete_item(
    item_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    card = _load_item(db, actor, item_id)
    title = todos.delete_item(db, workspace_id=actor.workspace_id, card=card)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="todo_item.deleted",
        resource_type="board_card",
        resource_id=item_id,
        detail={"title": title},
    )
    db.commit()
