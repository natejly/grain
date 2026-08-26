"""Kanban boards the agent can read and rearrange."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import case, select, update
from sqlalchemy.orm import Session

from ...models import Board, BoardCard, BoardColumn
from .. import coworking

DEFAULT_COLUMNS = ("Todo", "In progress", "Done")
MAX_CARDS_PER_BOARD = 500


class BoardError(ValueError):
    """A user- and model-facing problem with a board operation."""


def get_board(db: Session, *, workspace_id: str, board_id: str) -> Board:
    board = db.scalar(
        select(Board).where(Board.id == board_id, Board.workspace_id == workspace_id)
    )
    if board is None:
        raise BoardError("No board with that id")
    return board


def find_by_name(db: Session, *, workspace_id: str, name: str) -> Optional[Board]:
    wanted = name.strip().lower()
    for board in db.scalars(select(Board).where(Board.workspace_id == workspace_id)):
        if board.name.strip().lower() == wanted:
            return board
    return None


def resolve(
    db: Session, *, workspace_id: str, board_id: str = "", name: str = ""
) -> Board:
    if board_id:
        return get_board(db, workspace_id=workspace_id, board_id=board_id)
    if name:
        found = find_by_name(db, workspace_id=workspace_id, name=name)
        if found is None:
            raise BoardError(f"No board named “{name}”")
        return found
    # A single board is the common case; don't make the model name it.
    boards = list_boards(db, workspace_id=workspace_id)
    if len(boards) == 1:
        return boards[0]
    raise BoardError("Provide board_id or name")


def list_boards(db: Session, *, workspace_id: str) -> List[Board]:
    return list(
        db.scalars(
            select(Board)
            .where(Board.workspace_id == workspace_id)
            .order_by(Board.created_at.asc())
        )
    )


def columns_for(db: Session, board_id: str) -> List[BoardColumn]:
    # id breaks ties so a board with legacy duplicate positions still renders in
    # a stable order rather than shuffling between reads.
    return list(
        db.scalars(
            select(BoardColumn)
            .where(BoardColumn.board_id == board_id)
            .order_by(BoardColumn.position.asc(), BoardColumn.id.asc())
        )
    )


def cards_for(db: Session, board_id: str) -> List[BoardCard]:
    return list(
        db.scalars(
            select(BoardCard)
            .where(BoardCard.board_id == board_id)
            .order_by(BoardCard.position.asc(), BoardCard.created_at.asc())
        )
    )


def cards_in(db: Session, column_id: str) -> List[BoardCard]:
    return list(
        db.scalars(
            select(BoardCard)
            .where(BoardCard.column_id == column_id)
            .order_by(BoardCard.position.asc(), BoardCard.created_at.asc())
        )
    )


def resolve_column(
    db: Session, board: Board, column: str
) -> BoardColumn:
    """Accept a column id or a (case-insensitive) column name."""
    wanted = column.strip().lower()
    if not wanted:
        raise BoardError("Provide a column")
    found = None
    for candidate in columns_for(db, board.id):
        if candidate.id == column or candidate.name.strip().lower() == wanted:
            found = candidate
            break
    if found is None:
        names = [item.name for item in columns_for(db, board.id)]
        raise BoardError(f"No column “{column}”. Columns are: {', '.join(names)}")
    return found


def create_board(
    db: Session,
    *,
    workspace_id: str,
    name: str,
    columns: Optional[List[str]] = None,
    created_by: str = "",
) -> Board:
    name = name.strip()
    if not name:
        raise BoardError("A board needs a name")
    if find_by_name(db, workspace_id=workspace_id, name=name) is not None:
        raise BoardError(f"A board named “{name}” already exists")
    board = Board(workspace_id=workspace_id, name=name[:120], created_by=created_by)
    db.add(board)
    db.flush()
    wanted = [item.strip() for item in (columns or list(DEFAULT_COLUMNS)) if item.strip()]
    if not wanted:
        wanted = list(DEFAULT_COLUMNS)
    for position, column_name in enumerate(wanted):
        db.add(
            BoardColumn(
                workspace_id=workspace_id,
                board_id=board.id,
                name=column_name[:80],
                position=position,
            )
        )
    db.commit()
    return board


def _next_position(db: Session, column_id: str) -> int:
    """Append slot. Positions are dense 0..n-1, so the next one is the count."""
    return len(cards_in(db, column_id))


def renumber_cards(db: Session, column_id: str, ordered_ids: Sequence[str]) -> None:
    """Rewrite a column's card positions to a dense 0..n-1 in a single statement.

    One `UPDATE … CASE` instead of one UPDATE per card is what keeps a reorder
    from half-applying: there is no window in which some cards carry the new
    numbering and some the old, so two racing reorders each leave a dense
    permutation behind (one of them wins) rather than a column with duplicate or
    gapped positions that later reads would order arbitrarily.
    """
    ids = list(ordered_ids)
    if not ids:
        return
    db.execute(
        update(BoardCard)
        .where(BoardCard.column_id == column_id, BoardCard.id.in_(ids))
        .values(
            position=case(
                {card_id: index for index, card_id in enumerate(ids)},
                value=BoardCard.id,
                else_=BoardCard.position,
            )
        )
        .execution_options(synchronize_session=False)
    )


def renumber_columns(db: Session, board_id: str, ordered_ids: Sequence[str]) -> None:
    """Dense 0..n-1 column positions, single statement — see renumber_cards."""
    ids = list(ordered_ids)
    if not ids:
        return
    db.execute(
        update(BoardColumn)
        .where(BoardColumn.board_id == board_id, BoardColumn.id.in_(ids))
        .values(
            position=case(
                {column_id: index for index, column_id in enumerate(ids)},
                value=BoardColumn.id,
                else_=BoardColumn.position,
            )
        )
        .execution_options(synchronize_session=False)
    )


def place_card(db: Session, *, card: BoardCard, column_id: str, index: int) -> BoardCard:
    """Put `card` at `index` of `column_id`, leaving both columns dense.

    The column reassignment, the destination renumbering and the source
    renumbering all commit together, so a card is never briefly in two orders or
    none.
    """
    source_id = card.column_id
    if source_id != column_id:
        card.column_id = column_id
        db.flush()
    remaining = [item.id for item in cards_in(db, column_id) if item.id != card.id]
    index = max(0, min(index, len(remaining)))
    remaining.insert(index, card.id)
    renumber_cards(db, column_id, remaining)
    if source_id != column_id:
        renumber_cards(db, source_id, [item.id for item in cards_in(db, source_id)])
    db.commit()
    # Bulk UPDATEs bypass the identity map and the session does not expire on
    # commit, so without this the caller would keep reading stale positions.
    db.expire_all()
    return card


def add_card(
    db: Session,
    *,
    workspace_id: str,
    board: Board,
    column: str,
    title: str,
    body: str = "",
    labels: Optional[List[str]] = None,
) -> BoardCard:
    title = title.strip()
    if not title:
        raise BoardError("A card needs a title")
    if len(cards_for(db, board.id)) >= MAX_CARDS_PER_BOARD:
        raise BoardError(f"Board is at its {MAX_CARDS_PER_BOARD}-card limit")
    target = resolve_column(db, board, column)
    card = BoardCard(
        workspace_id=workspace_id,
        board_id=board.id,
        column_id=target.id,
        title=title[:300],
        body=body,
        labels_json=json.dumps([str(item)[:40] for item in (labels or [])][:8]),
        position=_next_position(db, target.id),
    )
    db.add(card)
    db.commit()
    return card


def find_card(db: Session, *, board: Board, card: str) -> BoardCard:
    """Accept a card id or an exact-ish title, since models quote titles."""
    wanted = card.strip().lower()
    if not wanted:
        raise BoardError("Provide a card")
    candidates = cards_for(db, board.id)
    for candidate in candidates:
        if candidate.id == card:
            return candidate
    matches = [item for item in candidates if item.title.strip().lower() == wanted]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise BoardError(f"More than one card is titled “{card}”; use its id")
    partial = [item for item in candidates if wanted in item.title.strip().lower()]
    if len(partial) == 1:
        return partial[0]
    raise BoardError(f"No card matching “{card}”")


def move_card(
    db: Session, *, board: Board, card: str, column: str
) -> BoardCard:
    target_card = find_card(db, board=board, card=card)
    target_column = resolve_column(db, board, column)
    if target_card.column_id == target_column.id:
        return target_card
    # Landing at the end is what "move to this column" has always meant; an
    # explicit index goes through board_columns.reorder_card instead.
    return place_card(
        db,
        card=target_card,
        column_id=target_column.id,
        index=_next_position(db, target_column.id),
    )


def update_card(
    db: Session,
    *,
    board: Board,
    card: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    labels: Optional[List[str]] = None,
) -> BoardCard:
    target = find_card(db, board=board, card=card)
    if title is not None and title.strip():
        target.title = title.strip()[:300]
    if body is not None:
        target.body = body
    if labels is not None:
        target.labels_json = json.dumps([str(item)[:40] for item in labels][:8])
    db.commit()
    return target


def delete_card(db: Session, *, board: Board, card: str) -> str:
    target = find_card(db, board=board, card=card)
    title = target.title
    column_id = target.column_id
    db.delete(target)
    db.flush()
    renumber_cards(db, column_id, [item.id for item in cards_in(db, column_id)])
    db.commit()
    db.expire_all()
    return title


def delete_board(db: Session, *, workspace_id: str, board_id: str) -> None:
    board = get_board(db, workspace_id=workspace_id, board_id=board_id)
    for card in cards_for(db, board.id):
        db.delete(card)
    for column in columns_for(db, board.id):
        db.delete(column)
    db.delete(board)
    db.commit()


def snapshot(db: Session, board: Board) -> Dict[str, Any]:
    """The board as nested data, for both the model and the API."""
    columns = columns_for(db, board.id)
    cards = cards_for(db, board.id)
    by_column: Dict[str, List[Dict[str, Any]]] = {column.id: [] for column in columns}
    for card in cards:
        by_column.setdefault(card.column_id, []).append(
            {
                "id": card.id,
                "title": card.title,
                "body": card.body,
                "labels": _labels(card),
                # Every card carries it, not only the ones a todo list draws as
                # checkboxes — see services/artifacts/todos.py. Reported here so
                # that an item ticked off as a todo is still visibly done after
                # its board grows a second column and becomes a kanban.
                "done": card.done_at is not None,
                # The claim rides the same snapshot (expired leases read as
                # none), so "who is on this" needs no second request either.
                **coworking.claim_snapshot(card),
            }
        )
    return {
        "id": board.id,
        "name": board.name,
        "columns": [
            {
                "id": column.id,
                "name": column.name,
                "cards": by_column.get(column.id, []),
            }
            for column in columns
        ],
    }


def _labels(card: BoardCard) -> List[str]:
    try:
        loaded = json.loads(card.labels_json)
    except (ValueError, TypeError):
        return []
    return [str(item) for item in loaded] if isinstance(loaded, list) else []
