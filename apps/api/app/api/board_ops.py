"""Column and ordering operations for kanban boards.

Split from /api/boards because these are structural edits — the shape of the
board and the exact slot a card occupies — rather than card CRUD. Every route
returns the whole board, so a drag that lands can replace its state in one go
instead of guessing what the server did with the other cards.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Board
from ..schemas import BoardOut
from ..services.artifacts import board_columns, boards

router = APIRouter(prefix="/api/board-ops", tags=["boards"])


class ColumnRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    index: Optional[int] = None


class ColumnRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ColumnOrderRequest(BaseModel):
    # No upper bound here: MAX_COLUMNS_PER_BOARD caps what `add_column` will
    # create, but boards created with a longer column list predate that cap, and
    # capping the *reorder* would leave them permanently unsortable. The service
    # requires an exact-length permutation, which is the tighter check anyway.
    order: List[str] = Field(min_length=1)


class CardOrderRequest(BaseModel):
    index: int = Field(ge=0)
    column_id: str = ""


def _load_board(db: Session, actor: Actor, board_id: str) -> Board:
    try:
        return boards.get_board(db, workspace_id=actor.workspace_id, board_id=board_id)
    except boards.BoardError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _board_out(db: Session, board: Board) -> BoardOut:
    return BoardOut.model_validate(boards.snapshot(db, board))


@router.post("/{board_id}/columns", response_model=BoardOut, status_code=201)
def add_column(
    board_id: str,
    payload: ColumnRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> BoardOut:
    board = _load_board(db, actor, board_id)
    try:
        board_columns.add_column(
            db,
            workspace_id=actor.workspace_id,
            board=board,
            name=payload.name,
            index=payload.index,
        )
    except boards.BoardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _board_out(db, board)


@router.patch("/{board_id}/columns/{column_id}", response_model=BoardOut)
def rename_column(
    board_id: str,
    column_id: str,
    payload: ColumnRenameRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> BoardOut:
    board = _load_board(db, actor, board_id)
    try:
        board_columns.rename_column(
            db, board=board, column=column_id, name=payload.name
        )
    except boards.BoardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _board_out(db, board)


@router.delete("/{board_id}/columns/{column_id}", response_model=BoardOut)
def delete_column(
    board_id: str,
    column_id: str,
    move_cards_to: str = "",
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> BoardOut:
    board = _load_board(db, actor, board_id)
    try:
        board_columns.delete_column(
            db, board=board, column=column_id, move_cards_to=move_cards_to
        )
    except boards.BoardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _board_out(db, board)


@router.post("/{board_id}/columns/reorder", response_model=BoardOut)
def reorder_columns(
    board_id: str,
    payload: ColumnOrderRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> BoardOut:
    board = _load_board(db, actor, board_id)
    try:
        board_columns.reorder_columns(db, board=board, order=payload.order)
    except boards.BoardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _board_out(db, board)


@router.post("/{board_id}/cards/{card_id}/reorder", response_model=BoardOut)
def reorder_card(
    board_id: str,
    card_id: str,
    payload: CardOrderRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> BoardOut:
    board = _load_board(db, actor, board_id)
    try:
        board_columns.reorder_card(
            db,
            board=board,
            card=card_id,
            index=payload.index,
            column=payload.column_id,
        )
    except boards.BoardError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _board_out(db, board)
