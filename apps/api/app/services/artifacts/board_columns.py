"""Column shape and card ordering for kanban boards.

`boards` owns the board itself and the dense-position primitives; this module is
the layer above it — the operations that change a board's *structure* (which
columns exist, in what order) and the exact slot a card sits in. Every operation
here leaves both columns and cards numbered 0..n-1 with no gaps.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from ...models import Board, BoardCard, BoardColumn
from .boards import (
    BoardError,
    cards_in,
    columns_for,
    find_card,
    place_card,
    renumber_cards,
    renumber_columns,
    resolve_column,
)

MAX_COLUMNS_PER_BOARD = 12


def column_names(db: Session, board: Board) -> List[str]:
    return [column.name for column in columns_for(db, board.id)]


def describe_columns(db: Session, board: Board) -> str:
    return ", ".join(column_names(db, board)) or "(none)"


def card_slot(db: Session, card: BoardCard) -> int:
    """The card's index inside its own column, for before/after previews."""
    ids = [item.id for item in cards_in(db, card.column_id)]
    return ids.index(card.id) if card.id in ids else 0


def _clean_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise BoardError("A column needs a name")
    return cleaned[:80]


def _reject_duplicate(
    db: Session, board: Board, name: str, *, ignore_id: str = ""
) -> None:
    wanted = name.strip().lower()
    for column in columns_for(db, board.id):
        if column.id != ignore_id and column.name.strip().lower() == wanted:
            raise BoardError(f"This board already has a column named “{column.name}”")


def plan_add_column(
    db: Session, *, board: Board, name: str, index: Optional[int] = None
) -> Tuple[str, int]:
    """Validate an add and return (cleaned name, the slot it lands in).

    Every `plan_*` here is the single definition of what is legal, called by the
    operation *and* by its tool preview. A preview that re-implemented the rules
    would drift from them, and the drift shows up as an approval card promising
    a change the executor then refuses.
    """
    cleaned = _clean_name(name)
    _reject_duplicate(db, board, cleaned)
    existing = columns_for(db, board.id)
    if len(existing) >= MAX_COLUMNS_PER_BOARD:
        raise BoardError(f"A board can hold {MAX_COLUMNS_PER_BOARD} columns at most")
    if index is not None and not 0 <= index <= len(existing):
        raise BoardError(
            f"Index {index} is out of range; this board has {len(existing)} columns"
        )
    return cleaned, len(existing) if index is None else index


def add_column(
    db: Session,
    *,
    workspace_id: str,
    board: Board,
    name: str,
    index: Optional[int] = None,
) -> BoardColumn:
    cleaned, slot = plan_add_column(db, board=board, name=name, index=index)
    existing = columns_for(db, board.id)
    column = BoardColumn(
        workspace_id=workspace_id,
        board_id=board.id,
        name=cleaned,
        position=len(existing),
    )
    db.add(column)
    db.flush()
    if slot != len(existing):
        order = [item.id for item in existing]
        order.insert(slot, column.id)
        renumber_columns(db, board.id, order)
    db.commit()
    db.expire_all()
    return column


def plan_rename_column(
    db: Session, *, board: Board, column: str, name: str
) -> Tuple[BoardColumn, str]:
    """Validate a rename and return (the column, its cleaned new name)."""
    target = resolve_column(db, board, column)
    cleaned = _clean_name(name)
    _reject_duplicate(db, board, cleaned, ignore_id=target.id)
    return target, cleaned


def rename_column(db: Session, *, board: Board, column: str, name: str) -> BoardColumn:
    target, cleaned = plan_rename_column(db, board=board, column=column, name=name)
    target.name = cleaned
    db.commit()
    return target


def plan_delete_column(
    db: Session, *, board: Board, column: str, move_cards_to: str = ""
) -> Tuple[BoardColumn, Optional[BoardColumn], List[BoardCard]]:
    """Validate a delete and return (column, destination or None, its cards)."""
    target = resolve_column(db, board, column)
    remaining = [item for item in columns_for(db, board.id) if item.id != target.id]
    if not remaining:
        raise BoardError("A board needs at least one column")
    cards = cards_in(db, target.id)
    destination: Optional[BoardColumn] = None
    if move_cards_to.strip():
        destination = resolve_column(db, board, move_cards_to)
        if destination.id == target.id:
            raise BoardError("Cards cannot move into the column being deleted")
    elif cards:
        plural = "card" if len(cards) == 1 else "cards"
        raise BoardError(
            f"Column “{target.name}” still holds {len(cards)} {plural}. "
            f"Pass move_cards_to (one of: {', '.join(item.name for item in remaining)}) "
            "or delete the cards first"
        )
    return target, destination, cards


def delete_column(
    db: Session, *, board: Board, column: str, move_cards_to: str = ""
) -> Tuple[str, int]:
    """Delete a column; returns (deleted name, cards relocated).

    A column with cards is refused unless the caller names where the cards go.
    Deleting a column is a layout decision, but its cards are the user's actual
    work — cascading the delete would destroy content the user never pointed at,
    and it cannot be undone. Refusing costs one extra argument.
    """
    target, destination, cards = plan_delete_column(
        db, board=board, column=column, move_cards_to=move_cards_to
    )
    remaining = [item for item in columns_for(db, board.id) if item.id != target.id]
    deleted_name = target.name
    if cards and destination is not None:
        order = [item.id for item in cards_in(db, destination.id)]
        for card in cards:
            card.column_id = destination.id
            order.append(card.id)
        db.flush()
        renumber_cards(db, destination.id, order)
    db.delete(target)
    db.flush()
    renumber_columns(db, board.id, [item.id for item in remaining])
    db.commit()
    db.expire_all()
    return deleted_name, len(cards)


def plan_reorder_columns(
    db: Session, *, board: Board, order: Sequence[str]
) -> List[BoardColumn]:
    """Validate a full permutation and return the columns in the wanted order."""
    current = columns_for(db, board.id)
    if len(order) != len(current):
        raise BoardError(
            f"Give all {len(current)} columns in the new order "
            f"(got {len(order)}): {', '.join(item.name for item in current)}"
        )
    resolved: List[BoardColumn] = []
    seen: set[str] = set()
    for entry in order:
        column = resolve_column(db, board, entry)
        if column.id in seen:
            raise BoardError(f"“{column.name}” is listed twice")
        seen.add(column.id)
        resolved.append(column)
    return resolved


def reorder_columns(
    db: Session, *, board: Board, order: Sequence[str]
) -> List[BoardColumn]:
    """Reorder by a full permutation — every column, named once, by id or name."""
    wanted = plan_reorder_columns(db, board=board, order=order)
    renumber_columns(db, board.id, [column.id for column in wanted])
    db.commit()
    db.expire_all()
    return columns_for(db, board.id)


def plan_reorder_card(
    db: Session, *, board: Board, card: str, index: int, column: str = ""
) -> Tuple[BoardCard, BoardColumn]:
    """Validate a slot move and return (card, destination column)."""
    target_card = find_card(db, board=board, card=card)
    target_column = (
        resolve_column(db, board, column)
        if column.strip()
        else resolve_column(db, board, target_card.column_id)
    )
    occupants = [
        item.id for item in cards_in(db, target_column.id) if item.id != target_card.id
    ]
    # len(occupants) is a legal index: it means "put it last".
    if not 0 <= index <= len(occupants):
        raise BoardError(
            f"Index {index} is out of range for “{target_column.name}” "
            f"(0–{len(occupants)})"
        )
    return target_card, target_column


def reorder_card(
    db: Session, *, board: Board, card: str, index: int, column: str = ""
) -> BoardCard:
    """Move a card to an exact slot, in its own column or another one."""
    target_card, target_column = plan_reorder_card(
        db, board=board, card=card, index=index, column=column
    )
    return place_card(db, card=target_card, column_id=target_column.id, index=index)
