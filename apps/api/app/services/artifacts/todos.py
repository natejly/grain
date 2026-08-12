"""Todo lists: a one-column board, read as checkboxes.

This is a *view*, not an entity, and the reuse is the design rather than a
shortcut taken to save a table:

- the agent's existing `board_*` tools work on a list the moment it exists,
  because a list is a board — no second set of write tools, no second set of
  approval previews, no second thing to keep in step with the first;
- an item that outgrows a checkbox graduates into a kanban card by adding a
  second column to its board, with no migration and no id changing hands;
- ordering is implemented once. `boards.renumber_cards` is the reason a reorder
  cannot half-apply, and a parallel todo table would have needed that argument
  made a second time, correctly, in a second place.

What a list is, precisely: **a board with exactly one column**. Derived, not
stored, because a stored flag would be a second source of truth for something
the columns already say, and it would let a board claim to be a list while
holding three columns. The cost of deriving it is that deleting two columns off a
kanban turns it into a list — which is the right answer, since a board with one
column has nothing to drag between and everything to tick off.

What the kanban could not do, and this adds: tick an item without opening its
board (`set_done` takes an item, not a board), and let the *agent* tick one as it
finishes the work (`app/services/artifacts/tools.py`, `todo_check`).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...clock import utcnow
from ...models import Board, BoardCard, BoardColumn
from . import boards
from .boards import BoardError

#: The sole column a list is created with. Never shown — a list renders as
#: checkboxes, and its one column has no heading to draw — but it is a real
#: BoardColumn, which is what makes the board it belongs to an ordinary board.
LIST_COLUMN = "To do"


def is_list(columns: List[BoardColumn]) -> bool:
    return len(columns) == 1


def sole_column(db: Session, board: Board) -> BoardColumn:
    """The column of a list-shaped board, or a refusal naming the reason."""
    columns = boards.columns_for(db, board.id)
    if not is_list(columns):
        raise BoardError(
            f"“{board.name}” is a board with {len(columns)} columns, not a todo "
            "list. Use the board tools, or name the column."
        )
    return columns[0]


def list_todo_lists(db: Session, *, workspace_id: str) -> List[Board]:
    """Every list-shaped board in the workspace, oldest first."""
    return [
        board
        for board in boards.list_boards(db, workspace_id=workspace_id)
        if is_list(boards.columns_for(db, board.id))
    ]


def create_todo_list(
    db: Session, *, workspace_id: str, name: str, created_by: str = ""
) -> Board:
    return boards.create_board(
        db,
        workspace_id=workspace_id,
        name=name,
        columns=[LIST_COLUMN],
        created_by=created_by,
    )


def resolve_list(
    db: Session, *, workspace_id: str, list_id: str = "", name: str = ""
) -> Board:
    """A list by id or name, refusing a board that is not one.

    Falls back to the single list in the workspace when neither is given, the
    same courtesy `boards.resolve` extends, and for the same reason: the common
    case is one list and the model should not have to name it.
    """
    if list_id or name:
        board = boards.resolve(
            db, workspace_id=workspace_id, board_id=list_id, name=name
        )
        sole_column(db, board)  # raises unless the board really is a list
        return board
    candidates = list_todo_lists(db, workspace_id=workspace_id)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise BoardError("There are no todo lists yet")
    names = ", ".join(item.name for item in candidates)
    raise BoardError(f"Say which list. There are: {names}")


def add_item(
    db: Session, *, workspace_id: str, board: Board, title: str, body: str = ""
) -> BoardCard:
    return boards.add_card(
        db,
        workspace_id=workspace_id,
        board=board,
        column=sole_column(db, board).id,
        title=title,
        body=body,
    )


def get_item(db: Session, *, workspace_id: str, item_id: str) -> BoardCard:
    """One item by id alone.

    The board is deliberately not a parameter. Ticking something off is the one
    interaction a checklist has, and requiring the caller to already know which
    list it came from is what makes a kanban a bad place to keep a checklist.
    The workspace filter is what keeps that safe: an id from another tenant
    misses here exactly as a made-up one does.
    """
    card = db.scalar(
        select(BoardCard).where(
            BoardCard.id == item_id, BoardCard.workspace_id == workspace_id
        )
    )
    if card is None:
        raise BoardError("No item with that id")
    return card


def find_item(db: Session, *, workspace_id: str, item: str, list_name: str = "") -> BoardCard:
    """An item by id, or by title across the workspace's lists.

    For the agent, which quotes titles back rather than carrying ids. An
    ambiguous title is refused rather than guessed at: ticking off the wrong
    "Email the client" is a mistake nobody notices until it matters.
    """
    wanted = item.strip()
    if not wanted:
        raise BoardError("Provide an item")
    exact = db.scalar(
        select(BoardCard).where(
            BoardCard.id == wanted, BoardCard.workspace_id == workspace_id
        )
    )
    if exact is not None:
        return exact
    if list_name:
        searched = [resolve_list(db, workspace_id=workspace_id, name=list_name)]
    else:
        searched = list_todo_lists(db, workspace_id=workspace_id)
    lowered = wanted.lower()
    cards = [card for board in searched for card in boards.cards_for(db, board.id)]
    matches = [card for card in cards if card.title.strip().lower() == lowered]
    if not matches:
        matches = [card for card in cards if lowered in card.title.strip().lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise BoardError(f"More than one item matches “{item}”; use its id")
    raise BoardError(f"No item matching “{item}”")


def set_done(db: Session, *, card: BoardCard, done: bool) -> BoardCard:
    """Tick or untick an item.

    Idempotent on the *timestamp*, not just on the flag: re-ticking something
    already done leaves the original `done_at` alone, so the record keeps saying
    when the work finished rather than when somebody last clicked.
    """
    if done and card.done_at is None:
        card.done_at = utcnow()
    elif not done:
        card.done_at = None
    db.commit()
    return card


def update_item(
    db: Session,
    *,
    card: BoardCard,
    title: Optional[str] = None,
    body: Optional[str] = None,
) -> BoardCard:
    if title is not None and title.strip():
        card.title = title.strip()[:300]
    if body is not None:
        card.body = body
    db.commit()
    return card


def delete_item(db: Session, *, workspace_id: str, card: BoardCard) -> str:
    board = boards.get_board(db, workspace_id=workspace_id, board_id=card.board_id)
    # Through the board's own delete so the column is renumbered dense, which is
    # the whole reason this is a view over boards rather than a table beside one.
    return boards.delete_card(db, board=board, card=card.id)


def item_snapshot(card: BoardCard) -> Dict[str, Any]:
    return {
        "id": card.id,
        "list_id": card.board_id,
        "title": card.title,
        "body": card.body,
        "done": card.done_at is not None,
        "done_at": card.done_at,
    }


def snapshot(db: Session, board: Board) -> Dict[str, Any]:
    """One list as nested data, for both the model and the API."""
    return {
        "id": board.id,
        "name": board.name,
        "items": [item_snapshot(card) for card in boards.cards_for(db, board.id)],
    }
