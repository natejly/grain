"""Todo lists, which are one-column boards read as checkboxes.

The design claim under test is reuse: a list is not a new entity, it is a view
over the kanban that already exists. That buys three things, and each is asserted
below rather than asserted in a comment —

- the agent's existing `board_*` tools work on a list the moment it exists,
- an item graduates into a kanban card by growing a second column, with no
  migration and no id changing hands, and the tick survives the promotion,
- ordering has one implementation, so deleting an item renumbers the rest.

Plus the two things a kanban genuinely cannot do, which are the reason the
feature exists at all: check an item off without opening its board, and let the
agent tick items as it finishes them.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

import pytest
from conftest import Identity
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Board, BoardCard
from app.services.artifacts import boards, todos
from app.services.artifacts.tools import registry_tools
from app.services.llm_tools import ToolContext


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def owner(identity_client: Callable[..., TestClient]) -> TestClient:
    """A workspace of its own, since board names are unique per workspace."""
    return identity_client(name="List owner", workspace_name="List workspace")


def identity_of(client: TestClient) -> Identity:
    return client.identity  # type: ignore[attr-defined,no-any-return]


def make_list(client: TestClient, name: str = "Today") -> Dict[str, Any]:
    response = client.post("/api/todos", json={"name": name})
    assert response.status_code == 201, response.text
    payload: Dict[str, Any] = response.json()
    return payload


def add(client: TestClient, list_id: str, title: str) -> Dict[str, Any]:
    response = client.post(f"/api/todos/{list_id}/items", json={"title": title})
    assert response.status_code == 201, response.text
    payload: Dict[str, Any] = response.json()
    return payload


def titles(payload: Dict[str, Any]) -> List[str]:
    return [item["title"] for item in payload["items"]]


def context_for(client: TestClient) -> ToolContext:
    identity = identity_of(client)
    return ToolContext(
        workspace_id=identity.workspace_id,
        user_id=identity.user_id,
        conversation_id="",
    )


def call_tool(
    db: Any, client: TestClient, name: str, args: Dict[str, Any]
) -> str:
    context = context_for(client)
    spec = registry_tools(db, context)[name]
    return spec.executor(db, context, args).content


# --------------------------------------------------------------------------
# A list is a board
# --------------------------------------------------------------------------


def test_a_list_is_a_one_column_board(owner: TestClient, db: Any) -> None:
    """Not a claim about the API surface — a claim about the row in the table.

    Everything else here depends on it: if a list were its own entity, the board
    tools, the board ordering and the board approval previews would all need a
    second copy that agreed with the first.
    """
    created = make_list(owner, "Groceries")
    board = db.get(Board, created["id"])
    assert board is not None and board.name == "Groceries"
    assert [column.name for column in boards.columns_for(db, board.id)] == [
        todos.LIST_COLUMN
    ]
    # And it is an ordinary board everywhere boards are shown.
    listed = owner.get("/api/boards").json()
    assert [row["name"] for row in listed if row["id"] == created["id"]] == ["Groceries"]


def test_a_kanban_is_not_offered_as_a_todo_list(owner: TestClient) -> None:
    """The shape is the definition: three columns is a board you drag things
    across, and there is nothing to tick off."""
    kanban = owner.post("/api/boards", json={"name": "Pipeline", "columns": []}).json()
    make_list(owner, "Today")

    ids = [row["id"] for row in owner.get("/api/todos").json()]
    assert kanban["id"] not in ids
    assert len(ids) == 1


# --------------------------------------------------------------------------
# Ticking off — the interaction a kanban has no word for
# --------------------------------------------------------------------------


def test_an_item_is_checked_off_by_its_id_alone(owner: TestClient) -> None:
    """The headline. No board id anywhere in this request."""
    created = make_list(owner)
    item = add(owner, created["id"], "Ship the thing")["items"][0]
    assert item["done"] is False and item["done_at"] is None

    checked = owner.patch(f"/api/todos/items/{item['id']}", json={"done": True})
    assert checked.status_code == 200
    assert checked.json()["done"] is True
    assert checked.json()["done_at"]
    assert checked.json()["list_id"] == created["id"]

    reopened = owner.patch(f"/api/todos/items/{item['id']}", json={"done": False})
    assert reopened.json()["done"] is False
    assert reopened.json()["done_at"] is None


def test_re_checking_keeps_the_original_time(owner: TestClient, db: Any) -> None:
    """`done_at` records when the work finished, not when somebody last clicked.

    A checklist that rewrites its own timestamps cannot answer the only question
    a finished checklist is ever asked afterwards.
    """
    created = make_list(owner)
    item = add(owner, created["id"], "Land the migration")["items"][0]
    first = owner.patch(f"/api/todos/items/{item['id']}", json={"done": True}).json()
    again = owner.patch(f"/api/todos/items/{item['id']}", json={"done": True}).json()
    assert again["done_at"] == first["done_at"]


def test_renaming_an_item_does_not_untick_it(owner: TestClient) -> None:
    created = make_list(owner)
    item = add(owner, created["id"], "Draft the note")["items"][0]
    owner.patch(f"/api/todos/items/{item['id']}", json={"done": True})

    renamed = owner.patch(
        f"/api/todos/items/{item['id']}", json={"title": "Draft the memo"}
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Draft the memo"
    assert renamed.json()["done"] is True


def test_checking_an_item_is_audited(owner: TestClient) -> None:
    created = make_list(owner)
    item = add(owner, created["id"], "Call the bank")["items"][0]
    owner.patch(f"/api/todos/items/{item['id']}", json={"done": True})
    owner.patch(f"/api/todos/items/{item['id']}", json={"done": False})

    actions = [
        row["action"]
        for row in owner.get("/api/audit-events").json()
        if row["resource_id"] == item["id"]
    ]
    assert actions == ["todo_item.unchecked", "todo_item.checked"]


def test_an_item_from_another_workspace_is_not_found(
    owner: TestClient, identity_client: Callable[..., TestClient]
) -> None:
    """The item id is the whole address, so the workspace filter is the only
    thing standing between it and another tenant's list."""
    stranger = identity_client(name="Stranger", workspace_name="Stranger lists")
    created = make_list(owner)
    item = add(owner, created["id"], "Private")["items"][0]

    assert stranger.patch(f"/api/todos/items/{item['id']}", json={"done": True}).status_code == 404
    assert stranger.delete(f"/api/todos/items/{item['id']}").status_code == 404
    assert owner.patch("/api/todos/items/nope", json={"done": True}).status_code == 404


# --------------------------------------------------------------------------
# It is still a board underneath
# --------------------------------------------------------------------------


def test_an_item_graduates_into_a_kanban_card_with_its_tick_intact(
    owner: TestClient, db: Any
) -> None:
    """The reuse argument, cashed.

    Adding a second column turns the list into a kanban. The card keeps its id,
    its position and its `done_at`, and no migration ran — which is precisely
    what a separate todo table would have cost.
    """
    created = make_list(owner, "Sprint")
    item = add(owner, created["id"], "Write the ADR")["items"][0]
    owner.patch(f"/api/todos/items/{item['id']}", json={"done": True})

    grown = owner.post(
        f"/api/board-ops/{created['id']}/columns", json={"name": "In progress"}
    )
    assert grown.status_code == 201
    board = grown.json()
    assert [column["name"] for column in board["columns"]] == [
        todos.LIST_COLUMN,
        "In progress",
    ]
    card = board["columns"][0]["cards"][0]
    assert card["id"] == item["id"]
    assert card["done"] is True

    # And it has left the todo surface, because it is no longer list-shaped.
    assert created["id"] not in [row["id"] for row in owner.get("/api/todos").json()]


def test_deleting_an_item_renumbers_the_rest(owner: TestClient, db: Any) -> None:
    """One ordering implementation, and it is the board's."""
    created = make_list(owner)
    for title in ("One", "Two", "Three"):
        add(owner, created["id"], title)
    listed = owner.get("/api/todos").json()[0]
    middle = listed["items"][1]

    assert owner.delete(f"/api/todos/items/{middle['id']}").status_code == 204

    remaining = owner.get("/api/todos").json()[0]
    assert titles(remaining) == ["One", "Three"]
    positions = [
        card.position
        for card in db.scalars(
            select(BoardCard)
            .where(BoardCard.board_id == created["id"])
            .order_by(BoardCard.position)
        )
    ]
    assert positions == [0, 1]


def test_items_come_back_in_the_order_they_were_added(owner: TestClient) -> None:
    created = make_list(owner)
    for title in ("First", "Second", "Third"):
        add(owner, created["id"], title)
    assert titles(owner.get("/api/todos").json()[0]) == ["First", "Second", "Third"]


def test_a_list_route_refuses_a_board_that_is_not_a_list(owner: TestClient) -> None:
    kanban = owner.post("/api/boards", json={"name": "Pipeline", "columns": []}).json()
    refused = owner.post(f"/api/todos/{kanban['id']}/items", json={"title": "nope"})
    assert refused.status_code == 404
    assert "not a todo list" in refused.json()["detail"]


# --------------------------------------------------------------------------
# The agent's side
# --------------------------------------------------------------------------


def test_the_agent_ticks_items_off_as_it_finishes_them(
    owner: TestClient, db: Any
) -> None:
    """The pairing with workflows: a run checking off its own steps."""
    created = make_list(owner, "Release")
    add(owner, created["id"], "Cut the branch")
    add(owner, created["id"], "Tag the release")

    said = call_tool(db, owner, "todo_check", {"item": "Cut the branch"})
    assert "Checked off" in said

    open_items = json.loads(call_tool(db, owner, "list_todos", {}))
    assert [item["title"] for item in open_items[0]["items"]] == ["Tag the release"]

    everything = json.loads(call_tool(db, owner, "list_todos", {"open_only": False}))
    assert [(item["title"], item["done"]) for item in everything[0]["items"]] == [
        ("Cut the branch", True),
        ("Tag the release", False),
    ]


def test_the_agent_adds_an_item_without_naming_a_column(
    owner: TestClient, db: Any
) -> None:
    """A list has exactly one column, so making the model pick one would be
    asking it to restate the definition."""
    created = make_list(owner, "Errands")
    said = call_tool(db, owner, "add_todo", {"title": "Buy milk"})
    assert "Buy milk" in said
    assert titles(owner.get("/api/todos").json()[0]) == ["Buy milk"]
    assert created["id"] == owner.get("/api/todos").json()[0]["id"]


def test_the_agent_is_refused_an_ambiguous_item_rather_than_guessing(
    owner: TestClient, db: Any
) -> None:
    """Ticking off the wrong "Email the client" is a mistake nobody notices
    until it matters, so the tool says so instead of picking one."""
    first = make_list(owner, "Work")
    second = make_list(owner, "Home")
    add(owner, first["id"], "Email the client")
    add(owner, second["id"], "Email the client")

    said = call_tool(db, owner, "todo_check", {"item": "Email the client"})
    assert "More than one item" in said
    # Naming the list resolves it.
    narrowed = call_tool(
        db, owner, "todo_check", {"item": "Email the client", "list": "Work"}
    )
    assert "Checked off" in narrowed
    lists = {row["name"]: row for row in owner.get("/api/todos").json()}
    assert [item["done"] for item in lists["Work"]["items"]] == [True]
    assert [item["done"] for item in lists["Home"]["items"]] == [False]


def test_the_write_tools_carry_a_preview_for_the_approval_card(
    owner: TestClient, db: Any
) -> None:
    """Every write tool in this file parks under the default approval mode, and
    an approval card showing raw JSON is a card nobody reads."""
    created = make_list(owner, "Preview")
    add(owner, created["id"], "Review the diff")
    registry = registry_tools(db, context_for(owner))
    context = context_for(owner)

    for name in ("add_todo", "todo_check"):
        spec = registry[name]
        assert spec.read_only is False
        assert spec.preview is not None
    assert (
        registry["todo_check"].preview(db, context, {"item": "Review the diff"})  # type: ignore[misc]
        == "Check off “Review the diff”"
    )
    assert registry["list_todos"].read_only is True


def test_the_board_tools_still_work_on_a_list(owner: TestClient, db: Any) -> None:
    """The reuse dividend, stated as a test: nothing had to be taught about
    lists for the agent to be able to rename, read and delete their items."""
    created = make_list(owner, "Shared")
    add(owner, created["id"], "Old name")
    context = context_for(owner)
    registry = registry_tools(db, context)

    registry["board_update_card"].executor(
        db, context, {"board": "Shared", "card": "Old name", "title": "New name"}
    )
    snapshot = json.loads(
        registry["read_board"].executor(db, context, {"name": "Shared"}).content
    )
    assert [card["title"] for card in snapshot["columns"][0]["cards"]] == ["New name"]
    assert snapshot["columns"][0]["cards"][0]["done"] is False


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _delete_what_this_file_creates(owner: TestClient) -> Any:
    """Each test owns a fresh workspace, and takes its boards with it.

    Boards are named uniquely per workspace, so a leftover "Today" would make the
    next spec's `make_list` fail — and these specs share a database with every
    other file in the suite.
    """
    yield
    db = SessionLocal()
    try:
        workspace_id = identity_of(owner).workspace_id
        for board in boards.list_boards(db, workspace_id=workspace_id):
            boards.delete_board(db, workspace_id=workspace_id, board_id=board.id)
    finally:
        db.close()
