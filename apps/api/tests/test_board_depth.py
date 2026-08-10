"""Column shape and card ordering: dense positions after every operation."""
from __future__ import annotations

import pytest

from app.api import board_ops
from app.database import SessionLocal
from app.main import app
from app.models import Board, BoardCard, BoardColumn
from app.services.artifacts import board_columns, boards
from app.services.artifacts.tools import registry_tools
from app.services.llm_tools import ToolContext

# The orchestrator wires this router into main.py; including it here (once) keeps
# the REST tests honest before and after that lands.
if not any(getattr(route, "path", "").startswith("/api/board-ops") for route in app.routes):
    app.include_router(board_ops.router)


@pytest.fixture
def workspace(client):
    identity = client.get("/api/bootstrap").json()["identity"]
    yield identity
    db = SessionLocal()
    try:
        for model in (BoardCard, BoardColumn, Board):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _context(identity) -> ToolContext:
    return ToolContext(
        workspace_id=identity["workspace_id"],
        user_id=identity["user_id"],
        conversation_id="none",
    )


def _board(db, workspace, name="Depth", columns=None):
    return boards.create_board(
        db, workspace_id=workspace["workspace_id"], name=name, columns=columns
    )


def _fill(db, workspace, board, column, titles):
    return [
        boards.add_card(
            db,
            workspace_id=workspace["workspace_id"],
            board=board,
            column=column,
            title=title,
        )
        for title in titles
    ]


def _titles(db, board, column_name):
    column = boards.resolve_column(db, board, column_name)
    return [card.title for card in boards.cards_in(db, column.id)]


def _card_positions(db, board, column_name):
    column = boards.resolve_column(db, board, column_name)
    return [card.position for card in boards.cards_in(db, column.id)]


def _column_positions(db, board):
    return [column.position for column in boards.columns_for(db, board.id)]


# --------------------------------------------------------------------------
# Columns


def test_add_column_appends_or_inserts_and_stays_dense(db, workspace):
    board = _board(db, workspace)
    board_columns.add_column(
        db, workspace_id=workspace["workspace_id"], board=board, name="Review"
    )
    assert board_columns.column_names(db, board) == [
        "Todo",
        "In progress",
        "Done",
        "Review",
    ]
    board_columns.add_column(
        db, workspace_id=workspace["workspace_id"], board=board, name="Backlog", index=0
    )
    assert board_columns.column_names(db, board) == [
        "Backlog",
        "Todo",
        "In progress",
        "Done",
        "Review",
    ]
    assert _column_positions(db, board) == [0, 1, 2, 3, 4]


def test_add_column_rejects_duplicates_and_bad_index(db, workspace):
    board = _board(db, workspace)
    with pytest.raises(boards.BoardError, match="already has a column"):
        board_columns.add_column(
            db, workspace_id=workspace["workspace_id"], board=board, name="todo"
        )
    with pytest.raises(boards.BoardError, match="out of range"):
        board_columns.add_column(
            db,
            workspace_id=workspace["workspace_id"],
            board=board,
            name="Later",
            index=9,
        )
    with pytest.raises(boards.BoardError, match="needs a name"):
        board_columns.add_column(
            db, workspace_id=workspace["workspace_id"], board=board, name="   "
        )


def test_rename_column_keeps_cards_and_refuses_duplicate_names(db, workspace):
    board = _board(db, workspace)
    _fill(db, workspace, board, "Todo", ["a", "b"])
    board_columns.rename_column(db, board=board, column="Todo", name="Backlog")
    assert board_columns.column_names(db, board) == ["Backlog", "In progress", "Done"]
    assert _titles(db, board, "Backlog") == ["a", "b"]
    with pytest.raises(boards.BoardError, match="already has a column"):
        board_columns.rename_column(db, board=board, column="Backlog", name="done")
    # Re-casing a column's own name is not a duplicate.
    board_columns.rename_column(db, board=board, column="Backlog", name="BACKLOG")
    assert board_columns.column_names(db, board)[0] == "BACKLOG"


def test_delete_column_refuses_cards_until_told_where_they_go(db, workspace):
    board = _board(db, workspace)
    _fill(db, workspace, board, "Todo", ["a", "b"])
    _fill(db, workspace, board, "Done", ["z"])
    with pytest.raises(boards.BoardError, match="still holds 2 cards"):
        board_columns.delete_column(db, board=board, column="Todo")
    assert board_columns.column_names(db, board) == ["Todo", "In progress", "Done"]

    name, moved = board_columns.delete_column(
        db, board=board, column="Todo", move_cards_to="Done"
    )
    assert (name, moved) == ("Todo", 2)
    assert board_columns.column_names(db, board) == ["In progress", "Done"]
    assert _titles(db, board, "Done") == ["z", "a", "b"]
    assert _card_positions(db, board, "Done") == [0, 1, 2]
    assert _column_positions(db, board) == [0, 1]


def test_delete_empty_column_needs_no_destination(db, workspace):
    board = _board(db, workspace)
    _name, moved = board_columns.delete_column(db, board=board, column="In progress")
    assert moved == 0
    assert board_columns.column_names(db, board) == ["Todo", "Done"]


def test_last_column_cannot_be_deleted(db, workspace):
    board = _board(db, workspace, columns=["Only"])
    with pytest.raises(boards.BoardError, match="at least one column"):
        board_columns.delete_column(db, board=board, column="Only")


def test_reorder_columns_requires_a_full_permutation(db, workspace):
    board = _board(db, workspace)
    board_columns.reorder_columns(db, board=board, order=["Done", "Todo", "In progress"])
    assert board_columns.column_names(db, board) == ["Done", "Todo", "In progress"]
    assert _column_positions(db, board) == [0, 1, 2]

    with pytest.raises(boards.BoardError, match="all 3 columns"):
        board_columns.reorder_columns(db, board=board, order=["Done"])
    with pytest.raises(boards.BoardError, match="listed twice"):
        board_columns.reorder_columns(db, board=board, order=["Done", "Done", "Todo"])
    with pytest.raises(boards.BoardError, match="No column"):
        board_columns.reorder_columns(db, board=board, order=["Done", "Todo", "Nope"])
    # A failed reorder changes nothing.
    assert board_columns.column_names(db, board) == ["Done", "Todo", "In progress"]


# --------------------------------------------------------------------------
# Card ordering


def test_reorder_card_within_a_column(db, workspace):
    board = _board(db, workspace)
    _fill(db, workspace, board, "Todo", ["a", "b", "c", "d"])
    board_columns.reorder_card(db, board=board, card="d", index=0)
    assert _titles(db, board, "Todo") == ["d", "a", "b", "c"]
    board_columns.reorder_card(db, board=board, card="d", index=2)
    assert _titles(db, board, "Todo") == ["a", "b", "d", "c"]
    board_columns.reorder_card(db, board=board, card="a", index=3)
    assert _titles(db, board, "Todo") == ["b", "d", "c", "a"]
    assert _card_positions(db, board, "Todo") == [0, 1, 2, 3]


def test_reorder_card_across_columns_renumbers_both(db, workspace):
    board = _board(db, workspace)
    _fill(db, workspace, board, "Todo", ["a", "b", "c"])
    _fill(db, workspace, board, "Done", ["x", "y"])
    board_columns.reorder_card(db, board=board, card="b", index=1, column="Done")
    assert _titles(db, board, "Todo") == ["a", "c"]
    assert _titles(db, board, "Done") == ["x", "b", "y"]
    assert _card_positions(db, board, "Todo") == [0, 1]
    assert _card_positions(db, board, "Done") == [0, 1, 2]


def test_reorder_card_index_must_be_in_range(db, workspace):
    board = _board(db, workspace)
    _fill(db, workspace, board, "Todo", ["a", "b"])
    with pytest.raises(boards.BoardError, match="out of range"):
        board_columns.reorder_card(db, board=board, card="a", index=5)
    with pytest.raises(boards.BoardError, match="out of range"):
        board_columns.reorder_card(db, board=board, card="a", index=-1)
    assert _titles(db, board, "Todo") == ["a", "b"]
    # The far end of a column is a legal slot: len(others) means "last".
    board_columns.reorder_card(db, board=board, card="a", index=1)
    assert _titles(db, board, "Todo") == ["b", "a"]


def test_move_card_still_appends_to_the_target_column(db, workspace):
    board = _board(db, workspace)
    _fill(db, workspace, board, "Todo", ["a", "b"])
    _fill(db, workspace, board, "Done", ["x"])
    boards.move_card(db, board=board, card="a", column="Done")
    assert _titles(db, board, "Done") == ["x", "a"]
    assert _card_positions(db, board, "Done") == [0, 1]
    assert _card_positions(db, board, "Todo") == [0]
    # Moving to the column it already sits in is a no-op, not a reshuffle.
    boards.move_card(db, board=board, card="a", column="Done")
    assert _titles(db, board, "Done") == ["x", "a"]


def test_deleting_a_card_leaves_the_column_dense(db, workspace):
    board = _board(db, workspace)
    _fill(db, workspace, board, "Todo", ["a", "b", "c"])
    boards.delete_card(db, board=board, card="b")
    assert _titles(db, board, "Todo") == ["a", "c"]
    assert _card_positions(db, board, "Todo") == [0, 1]


def test_racing_reorders_still_leave_a_dense_permutation(db, workspace):
    """Two writers that both decided on an order from the same starting state.

    The loser's numbering is overwritten wholesale rather than interleaved, so
    the column ends up as one of the two orders — never with duplicate or gapped
    positions, which is what a card-at-a-time renumbering would risk.
    """
    board = _board(db, workspace)
    cards = _fill(db, workspace, board, "Todo", ["a", "b", "c"])
    column_id = cards[0].column_id
    by_title = {card.title: card.id for card in cards}

    other = SessionLocal()
    try:
        # Both sessions planned against [a, b, c]; the second one to write wins.
        boards.renumber_cards(
            other, column_id, [by_title["c"], by_title["a"], by_title["b"]]
        )
        other.commit()
        boards.renumber_cards(
            db, column_id, [by_title["b"], by_title["c"], by_title["a"]]
        )
        db.commit()
        db.expire_all()
    finally:
        other.close()

    assert _titles(db, board, "Todo") == ["b", "c", "a"]
    assert _card_positions(db, board, "Todo") == [0, 1, 2]


def test_snapshot_reflects_column_and_card_order(db, workspace):
    board = _board(db, workspace)
    _fill(db, workspace, board, "Todo", ["a", "b"])
    board_columns.reorder_card(db, board=board, card="b", index=0)
    board_columns.reorder_columns(db, board=board, order=["Done", "In progress", "Todo"])
    snapshot = boards.snapshot(db, board)
    assert [column["name"] for column in snapshot["columns"]] == [
        "Done",
        "In progress",
        "Todo",
    ]
    todo = next(column for column in snapshot["columns"] if column["name"] == "Todo")
    assert [card["title"] for card in todo["cards"]] == ["b", "a"]


# --------------------------------------------------------------------------
# Agent tools


def _tools(db, workspace):
    return registry_tools(db, _context(workspace))


def test_new_tools_are_writes_with_previews(db, workspace):
    specs = _tools(db, workspace)
    for name in (
        "board_add_column",
        "board_rename_column",
        "board_delete_column",
        "board_reorder_columns",
        "board_reorder_card",
    ):
        spec = specs[name]
        assert spec.read_only is False
        assert spec.preview is not None


def test_previews_name_before_and_after_without_mutating(db, workspace):
    board = _board(db, workspace)
    _fill(db, workspace, board, "Todo", ["a", "b"])
    specs = _tools(db, workspace)
    context = _context(workspace)
    before = boards.snapshot(db, board)

    previews = {
        "board_add_column": {"board": board.name, "name": "Review", "index": 1},
        "board_rename_column": {"board": board.name, "column": "Todo", "name": "Later"},
        "board_delete_column": {
            "board": board.name,
            "column": "Todo",
            "move_cards_to": "Done",
        },
        "board_reorder_columns": {
            "board": board.name,
            "order": ["Done", "Todo", "In progress"],
        },
        "board_reorder_card": {"board": board.name, "card": "b", "index": 0},
    }
    rendered = {}
    for name, args in previews.items():
        spec = specs[name]
        assert spec.preview is not None
        rendered[name] = spec.preview(db, context, args)

    assert "Todo, In progress, Done" in rendered["board_add_column"]
    assert "Todo, Review, In progress, Done" in rendered["board_add_column"]
    assert "“Todo” → “Later”" in rendered["board_rename_column"]
    assert "2 card(s) move to “Done”" in rendered["board_delete_column"]
    assert "Todo, In progress, Done → Done, Todo, In progress" in (
        rendered["board_reorder_columns"]
    )
    assert "“Todo” #2" in rendered["board_reorder_card"]
    assert "“Todo” #1" in rendered["board_reorder_card"]

    assert boards.snapshot(db, board) == before


def test_preview_of_a_doomed_delete_explains_itself(db, workspace):
    board = _board(db, workspace)
    _fill(db, workspace, board, "Todo", ["a"])
    spec = _tools(db, workspace)["board_delete_column"]
    assert spec.preview is not None
    text = spec.preview(db, _context(workspace), {"board": board.name, "column": "Todo"})
    assert "will fail" in text and "name a column to move them to" in text


def test_tools_apply_and_report_the_new_state(db, workspace):
    board = _board(db, workspace)
    _fill(db, workspace, board, "Todo", ["a", "b", "c"])
    specs = _tools(db, workspace)
    context = _context(workspace)

    added = specs["board_add_column"].executor(
        db, context, {"board": board.name, "name": "Review", "index": "1"}
    )
    assert "Todo, Review, In progress, Done" in added.content

    renamed = specs["board_rename_column"].executor(
        db, context, {"board": board.name, "column": "Review", "name": "QA"}
    )
    assert "“Review” to “QA”" in renamed.content

    moved = specs["board_reorder_card"].executor(
        db, context, {"board": board.name, "card": "c", "index": 0}
    )
    assert "#1 in “Todo”" in moved.content
    assert _titles(db, board, "Todo") == ["c", "a", "b"]

    dropped = specs["board_delete_column"].executor(
        db, context, {"board": board.name, "column": "Todo", "move_cards_to": "QA"}
    )
    assert "3 cards moved" in dropped.content
    assert _titles(db, board, "QA") == ["c", "a", "b"]

    reordered = specs["board_reorder_columns"].executor(
        db, context, {"board": board.name, "order": ["Done", "In progress", "QA"]}
    )
    assert "Done, In progress, QA" in reordered.content


def test_every_new_tool_returns_an_error_string_on_bad_input(db, workspace):
    board = _board(db, workspace)
    _fill(db, workspace, board, "Todo", ["a"])
    specs = _tools(db, workspace)
    context = _context(workspace)

    cases = [
        ("board_add_column", {"board": board.name, "name": ""}),
        ("board_add_column", {"board": board.name, "name": "X", "index": "soon"}),
        ("board_add_column", {"board": "nope", "name": "X"}),
        ("board_rename_column", {"board": board.name, "column": "Ghost", "name": "X"}),
        ("board_rename_column", {"board": board.name, "column": "Todo", "name": "Done"}),
        ("board_delete_column", {"board": board.name, "column": "Todo"}),
        ("board_delete_column", {"board": board.name, "column": "Ghost"}),
        ("board_reorder_columns", {"board": board.name, "order": "Todo"}),
        ("board_reorder_columns", {"board": board.name, "order": ["Todo"]}),
        ("board_reorder_card", {"board": board.name, "card": "a"}),
        ("board_reorder_card", {"board": board.name, "card": "ghost", "index": 0}),
        ("board_reorder_card", {"board": board.name, "card": "a", "index": 12}),
    ]
    for name, args in cases:
        result = specs[name].executor(db, context, args)
        assert result.content.startswith("Error: "), (name, args, result.content)

    # And nothing leaked through: the board is untouched.
    assert board_columns.column_names(db, board) == ["Todo", "In progress", "Done"]
    assert _titles(db, board, "Todo") == ["a"]


def test_a_preview_never_promises_what_the_executor_refuses(db, workspace):
    """The approval card is the only thing the user reads before saying yes.

    Each of these arguments is rejected by the executor, so a preview that
    rendered a confident before → after would be asking for a blind approval.
    """
    board = _board(db, workspace)
    _fill(db, workspace, board, "Todo", ["a"])
    specs = _tools(db, workspace)
    context = _context(workspace)

    doomed = [
        ("board_add_column", {"board": board.name, "name": "todo"}),
        ("board_add_column", {"board": board.name, "name": "New", "index": 9}),
        ("board_add_column", {"board": board.name, "name": "New", "index": -3}),
        ("board_rename_column", {"board": board.name, "column": "Todo", "name": "Done"}),
        ("board_rename_column", {"board": board.name, "column": "Todo", "name": "  "}),
        ("board_delete_column", {"board": board.name, "column": "Todo", "move_cards_to": "Ghost"}),
        ("board_delete_column", {"board": board.name, "column": "Todo", "move_cards_to": "Todo"}),
        ("board_reorder_columns", {"board": board.name, "order": ["Done"]}),
        ("board_reorder_columns", {"board": board.name, "order": ["Done", "Done", "Todo"]}),
        ("board_reorder_card", {"board": board.name, "card": "a", "index": 7}),
        ("board_reorder_card", {"board": board.name, "card": "a", "index": -1}),
    ]
    for name, args in doomed:
        spec = specs[name]
        assert spec.preview is not None
        text = spec.preview(db, context, args)
        assert "will fail" in text, (name, args, text)
        assert spec.executor(db, context, args).content.startswith("Error: ")

    assert board_columns.column_names(db, board) == ["Todo", "In progress", "Done"]
    assert _titles(db, board, "Todo") == ["a"]


def test_an_index_that_is_a_digit_but_not_a_number_is_an_error_not_a_crash(db, workspace):
    """"²".isdigit() is True but int("²") raises — the tool must not.

    Executors owe the model a ToolResult and previews owe the approval card a
    string; letting a ValueError out costs the user their preview entirely.
    """
    board = _board(db, workspace)
    _fill(db, workspace, board, "Todo", ["a"])
    specs = _tools(db, workspace)
    context = _context(workspace)
    for name, args in [
        ("board_add_column", {"board": board.name, "name": "X", "index": "²"}),
        ("board_reorder_card", {"board": board.name, "card": "a", "index": "④"}),
    ]:
        spec = specs[name]
        assert spec.preview is not None
        assert "will fail" in spec.preview(db, context, args)
        result = spec.executor(db, context, args)
        assert result.content == "Error: index must be a whole number"


def test_previews_never_raise_on_bad_input(db, workspace):
    board = _board(db, workspace)
    specs = _tools(db, workspace)
    context = _context(workspace)
    for name, args in [
        ("board_add_column", {"board": "nope", "name": "X"}),
        ("board_rename_column", {"board": board.name, "column": "Ghost", "name": "X"}),
        ("board_delete_column", {"board": board.name, "column": "Ghost"}),
        ("board_reorder_columns", {"board": board.name, "order": []}),
        ("board_reorder_card", {"board": board.name, "card": "ghost", "index": 0}),
    ]:
        spec = specs[name]
        assert spec.preview is not None
        assert "will fail" in spec.preview(db, context, args)


# --------------------------------------------------------------------------
# REST


def test_rest_column_and_card_ordering_round_trip(client, workspace):
    board = client.post("/api/boards", json={"name": "REST", "columns": []}).json()
    board_id = board["id"]
    for title in ("a", "b", "c"):
        board = client.post(
            f"/api/boards/{board_id}/cards",
            json={"column": "Todo", "title": title, "body": "", "labels": []},
        ).json()

    added = client.post(
        f"/api/board-ops/{board_id}/columns", json={"name": "Review", "index": 1}
    )
    assert added.status_code == 201
    assert [column["name"] for column in added.json()["columns"]] == [
        "Todo",
        "Review",
        "In progress",
        "Done",
    ]

    todo_id = added.json()["columns"][0]["id"]
    card_id = added.json()["columns"][0]["cards"][2]["id"]
    reordered = client.post(
        f"/api/board-ops/{board_id}/cards/{card_id}/reorder", json={"index": 0}
    )
    assert [card["title"] for card in reordered.json()["columns"][0]["cards"]] == [
        "c",
        "a",
        "b",
    ]

    review_id = reordered.json()["columns"][1]["id"]
    across = client.post(
        f"/api/board-ops/{board_id}/cards/{card_id}/reorder",
        json={"index": 0, "column_id": review_id},
    )
    columns = {column["id"]: column for column in across.json()["columns"]}
    assert [card["title"] for card in columns[review_id]["cards"]] == ["c"]
    assert [card["title"] for card in columns[todo_id]["cards"]] == ["a", "b"]

    renamed = client.patch(
        f"/api/board-ops/{board_id}/columns/{review_id}", json={"name": "QA"}
    )
    assert renamed.json()["columns"][1]["name"] == "QA"

    order = [column["id"] for column in renamed.json()["columns"]][::-1]
    flipped = client.post(
        f"/api/board-ops/{board_id}/columns/reorder", json={"order": order}
    )
    assert [column["id"] for column in flipped.json()["columns"]] == order

    client.delete(f"/api/boards/{board_id}")


def test_rest_rejects_deleting_a_column_with_cards(client, workspace):
    board = client.post("/api/boards", json={"name": "Guard", "columns": []}).json()
    board_id = board["id"]
    board = client.post(
        f"/api/boards/{board_id}/cards",
        json={"column": "Todo", "title": "keep me", "body": "", "labels": []},
    ).json()
    todo_id = board["columns"][0]["id"]
    done_id = board["columns"][2]["id"]

    refused = client.delete(f"/api/board-ops/{board_id}/columns/{todo_id}")
    assert refused.status_code == 422
    assert "still holds" in refused.json()["detail"]

    ok = client.delete(
        f"/api/board-ops/{board_id}/columns/{todo_id}?move_cards_to={done_id}"
    )
    assert ok.status_code == 200
    columns = {column["id"]: column for column in ok.json()["columns"]}
    assert todo_id not in columns
    assert [card["title"] for card in columns[done_id]["cards"]] == ["keep me"]

    missing = client.post(
        "/api/board-ops/does-not-exist/columns", json={"name": "X"}
    )
    assert missing.status_code == 404
    client.delete(f"/api/boards/{board_id}")


def test_rest_reorders_a_board_wider_than_the_add_column_cap(client, workspace):
    """A board can already hold more columns than `add_column` would create.

    The reorder body must not cap the permutation at MAX_COLUMNS_PER_BOARD, or
    such a board could never be sorted again: the service demands every column.
    """
    width = board_columns.MAX_COLUMNS_PER_BOARD + 8
    board = client.post(
        "/api/boards",
        json={"name": "Wide", "columns": [f"c{index}" for index in range(width)]},
    ).json()
    assert len(board["columns"]) == width

    order = [column["id"] for column in board["columns"]][::-1]
    flipped = client.post(
        f"/api/board-ops/{board['id']}/columns/reorder", json={"order": order}
    )
    assert flipped.status_code == 200
    assert [column["id"] for column in flipped.json()["columns"]] == order
    client.delete(f"/api/boards/{board['id']}")
