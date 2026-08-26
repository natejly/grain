"""Live coworking: claims, presence, the workspace event log, and awareness.

The claims under test are the feature's load-bearing promises:

- a claim is a LEASE decided by one conditional UPDATE — the holder renews,
  anyone else conflicts, and an expired lease reads as free with no sweep;
- humans outrank agents (force-release exists for users, not for tools), and
  finishing an item releases it wherever the tick came from;
- presence is ephemeral by TTL read, not by deletion, and a personal thread's
  typing chip never leaks to another member;
- the digest an agent starts with names other runs and claimed cards, so
  "don't do it twice" reaches the model as instructions, not etiquette.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable, Dict

import pytest
from conftest import Identity
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from app.clock import utcnow
from app.database import SessionLocal
from app.models import BoardCard, Conversation, Membership, Presence, Run, User
from app.services import coworking
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
    return identity_client(name="Coworker", workspace_name="Coworking workspace")


def identity_of(client: TestClient) -> Identity:
    return client.identity  # type: ignore[attr-defined,no-any-return]


def make_item(client: TestClient, title: str = "Ship the report") -> Dict[str, Any]:
    listed = client.post("/api/todos", json={"name": "Today"})
    assert listed.status_code == 201, listed.text
    added = client.post(
        f"/api/todos/{listed.json()['id']}/items", json={"title": title}
    )
    assert added.status_code == 201, added.text
    item: Dict[str, Any] = added.json()["items"][0]
    return item


def agent_context(client: TestClient) -> ToolContext:
    who = identity_of(client)
    return ToolContext(
        workspace_id=who.workspace_id, user_id=who.user_id, conversation_id=""
    )


def run_tool(db: Any, client: TestClient, name: str, args: Dict[str, Any]) -> str:
    registry = registry_tools(db, agent_context(client))
    result = registry[name].executor(db, agent_context(client), args)
    return result.content


# ---------------------------------------------------------------------------
# Claims


def test_claim_badges_the_item_and_the_snapshot(owner: TestClient) -> None:
    item = make_item(owner)
    claimed = owner.post(f"/api/coworking/items/{item['id']}/claim")
    assert claimed.status_code == 200, claimed.text
    body = claimed.json()
    assert body["claimed"] is True
    assert body["claimed_kind"] == "user"
    assert body["claimed_label"] == "Coworker"
    assert body["claim_expires_at"] is not None
    # The claim rides the ordinary list read — no second request.
    lists = owner.get("/api/todos").json()
    assert lists[0]["items"][0]["claimed"] is True


def test_agent_cannot_take_or_tick_a_claimed_item(
    owner: TestClient, db: Any
) -> None:
    item = make_item(owner)
    assert owner.post(f"/api/coworking/items/{item['id']}/claim").status_code == 200
    conflict = run_tool(db, owner, "todo_claim", {"item": item["id"]})
    assert "already claimed by Coworker" in conflict
    refused = run_tool(db, owner, "todo_check", {"item": item["id"]})
    assert "claimed by Coworker" in refused
    row = db.get(BoardCard, item["id"])
    assert row.done_at is None


def test_expired_claim_reads_as_free(owner: TestClient, db: Any) -> None:
    item = make_item(owner)
    assert owner.post(f"/api/coworking/items/{item['id']}/claim").status_code == 200
    row = db.get(BoardCard, item["id"])
    row.claim_expires_at = utcnow() - timedelta(minutes=1)
    db.commit()
    # The stale lease renders as no claim at all...
    lists = owner.get("/api/todos").json()
    assert lists[0]["items"][0]["claimed"] is False
    # ...and the card is genuinely takeable, no sweep in between.
    taken = run_tool(db, owner, "todo_claim", {"item": item["id"]})
    assert taken.startswith("Claimed")


def test_release_needs_the_holder_or_force(
    owner: TestClient, db: Any
) -> None:
    item = make_item(owner)
    assert run_tool(db, owner, "todo_claim", {"item": item["id"]}).startswith(
        "Claimed"
    )
    # A user who is not the holder is refused without force...
    refused = owner.post(f"/api/coworking/items/{item['id']}/release")
    assert refused.status_code == 409
    # ...and outranks the agent with it.
    forced = owner.post(f"/api/coworking/items/{item['id']}/release?force=true")
    assert forced.status_code == 200
    assert forced.json()["claimed"] is False


def test_ticking_releases_the_claim(owner: TestClient) -> None:
    item = make_item(owner)
    assert owner.post(f"/api/coworking/items/{item['id']}/claim").status_code == 200
    ticked = owner.patch(f"/api/todos/items/{item['id']}", json={"done": True})
    assert ticked.status_code == 200, ticked.text
    body = ticked.json()
    assert body["done"] is True
    assert body["claimed"] is False


def test_agent_claim_then_check_flows_through(owner: TestClient, db: Any) -> None:
    item = make_item(owner)
    assert run_tool(db, owner, "todo_claim", {"item": item["id"]}).startswith(
        "Claimed"
    )
    # The holder renews rather than conflicts, and may tick its own card.
    again = run_tool(db, owner, "todo_claim", {"item": item["id"]})
    assert again.startswith("Claimed")
    checked = run_tool(db, owner, "todo_check", {"item": item["id"]})
    assert checked.startswith("Checked off")
    row = db.get(BoardCard, item["id"])
    assert row.done_at is not None
    assert row.claimed_by == ""


# ---------------------------------------------------------------------------
# The workspace event log


def test_claims_and_ticks_land_in_the_workspace_log(
    owner: TestClient, db: Any
) -> None:
    who = identity_of(owner)
    item = make_item(owner)
    before = owner.get("/api/coworking/activity").json()["last_event_sequence"]
    assert owner.post(f"/api/coworking/items/{item['id']}/claim").status_code == 200
    assert (
        owner.patch(f"/api/todos/items/{item['id']}", json={"done": True}).status_code
        == 200
    )
    events = coworking.events_after(db, workspace_id=who.workspace_id, after=before)
    kinds = [event.event_type for event in events]
    assert kinds == ["card.claimed", "todo.checked"]
    sequences = [event.sequence for event in events]
    assert sequences == sorted(sequences)


# ---------------------------------------------------------------------------
# Presence


def test_presence_heartbeat_shows_in_activity_and_expires(
    owner: TestClient, db: Any
) -> None:
    who = identity_of(owner)
    beat = owner.post(
        "/api/coworking/presence",
        json={
            "surface": "document:doc-1",
            "state": {"cursor": 12, "typing": True, "draft": "Live text"},
        },
    )
    assert beat.status_code == 200, beat.text
    presences = owner.get("/api/coworking/activity").json()["presences"]
    assert len(presences) == 1
    assert presences[0]["state"]["draft"] == "Live text"
    # Ephemeral by TTL read: an old heartbeat is simply not there.
    row = db.scalar(select(Presence).where(Presence.workspace_id == who.workspace_id))
    row.updated_at = utcnow() - timedelta(seconds=60)
    db.commit()
    assert owner.get("/api/coworking/activity").json()["presences"] == []


def test_presence_goodbye_clears_immediately(owner: TestClient) -> None:
    assert (
        owner.post(
            "/api/coworking/presence", json={"surface": "board:b-1", "state": {}}
        ).status_code
        == 200
    )
    assert owner.delete("/api/coworking/presence?surface=board:b-1").status_code == 204
    assert owner.get("/api/coworking/activity").json()["presences"] == []


def test_personal_thread_presence_does_not_leak_to_another_member(
    owner: TestClient, db: Any
) -> None:
    who = identity_of(owner)
    thread = Conversation(
        workspace_id=who.workspace_id, created_by=who.user_id, title="Mine"
    )
    other = User(email=f"other-{who.user_id[:8]}@example.com", name="Other member")
    db.add_all([thread, other])
    db.flush()
    db.add(
        Membership(workspace_id=who.workspace_id, user_id=other.id, role="member")
    )
    db.commit()
    assert (
        owner.post(
            "/api/coworking/presence",
            json={"surface": f"conversation:{thread.id}", "state": {"typing": True}},
        ).status_code
        == 200
    )
    from app.api.coworking import _visible_presences

    mine = _visible_presences(db, workspace_id=who.workspace_id, user_id=who.user_id)
    theirs = _visible_presences(db, workspace_id=who.workspace_id, user_id=other.id)
    assert len(mine) == 1
    assert theirs == []


# ---------------------------------------------------------------------------
# Awareness


def test_digest_names_other_runs_and_claimed_cards(
    owner: TestClient, db: Any
) -> None:
    who = identity_of(owner)
    item = make_item(owner, title="Reconcile invoices")
    assert owner.post(f"/api/coworking/items/{item['id']}/claim").status_code == 200
    thread = Conversation(
        workspace_id=who.workspace_id, created_by=who.user_id, title="Work"
    )
    db.add(thread)
    db.flush()
    other = Run(
        workspace_id=who.workspace_id,
        conversation_id=thread.id,
        agent_id="",
        created_by=who.user_id,
        status="running",
        prompt="Draft the quarterly summary",
    )
    mine = Run(
        workspace_id=who.workspace_id,
        conversation_id=thread.id,
        agent_id="",
        created_by=who.user_id,
        status="running",
        prompt="Do the next open item",
    )
    db.add_all([other, mine])
    db.commit()
    digest = coworking.digest_block(db, run=mine)
    assert "Draft the quarterly summary" in digest
    assert "Reconcile invoices" in digest
    assert "Coworker" in digest
    # Its own run is not "someone else".
    assert "Do the next open item" not in digest


def test_quiet_workspace_has_no_digest(owner: TestClient, db: Any) -> None:
    who = identity_of(owner)
    thread = Conversation(
        workspace_id=who.workspace_id, created_by=who.user_id, title="Quiet"
    )
    db.add(thread)
    db.flush()
    run = Run(
        workspace_id=who.workspace_id,
        conversation_id=thread.id,
        agent_id="",
        created_by=who.user_id,
        status="running",
        prompt="Only run in town",
    )
    db.add(run)
    db.commit()
    assert coworking.digest_block(db, run=run) == ""


# ---------------------------------------------------------------------------
# Live pointer cursors. The pointer is the one part of a heartbeat that another
# member's browser turns into geometry rather than into escaped text, so the
# shape is enforced on the way in and these pin that it is.


def test_pointer_rides_the_heartbeat_to_the_other_members(
    owner: TestClient, identity_client: Callable[..., TestClient], db: Any
) -> None:
    """The whole feature in one assertion: A moves, B sees where."""
    who = identity_of(owner)
    mate = identity_client(name="Second")
    db.add(
        Membership(
            workspace_id=who.workspace_id,
            user_id=identity_of(mate).user_id,
            role="member",
        )
    )
    db.commit()
    mate.headers["X-Workspace-Id"] = who.workspace_id

    beat = owner.post(
        "/api/coworking/presence",
        json={
            "surface": "document:doc-9",
            "state": {"pointer": {"x": 0.25, "y": 0.5}},
        },
    )
    assert beat.status_code == 200, beat.text
    seen = mate.get("/api/coworking/activity").json()["presences"]
    mine = [row for row in seen if row["actor_id"] == who.user_id]
    assert mine and mine[0]["state"]["pointer"] == {"x": 0.25, "y": 0.5}


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        # Out of the box in both directions clamps rather than 422s: a drag
        # that runs off the edge is a normal gesture, not a broken client.
        ({"x": -3.0, "y": 9.0}, {"x": 0.0, "y": 1.0}),
        # Rounded, so a pointer costs a bounded number of bytes per beat.
        ({"x": 0.123456789, "y": 0.987654321}, {"x": 0.1235, "y": 0.9877}),
    ],
)
def test_pointer_is_clamped_and_rounded(
    owner: TestClient, sent: Dict[str, Any], expected: Dict[str, Any]
) -> None:
    owner.post(
        "/api/coworking/presence",
        json={"surface": "document:doc-9", "state": {"pointer": sent}},
    )
    presences = owner.get("/api/coworking/activity").json()["presences"]
    assert presences[0]["state"]["pointer"] == expected


@pytest.mark.parametrize(
    "junk",
    [
        "over there",
        {"x": "left", "y": 0.5},
        {"x": None, "y": None},
        {"y": 0.5},
        [0.5, 0.5],
    ],
)
def test_a_malformed_pointer_is_dropped_not_rejected(
    owner: TestClient, junk: Any
) -> None:
    """The rest of the heartbeat still lands.

    A member whose cursor stops moving is a smaller failure than a member who
    stops appearing, so a bad pointer costs the pointer and nothing else.
    """
    beat = owner.post(
        "/api/coworking/presence",
        json={
            "surface": "document:doc-9",
            "state": {"pointer": junk, "typing": True},
        },
    )
    assert beat.status_code == 200, beat.text
    state = owner.get("/api/coworking/activity").json()["presences"][0]["state"]
    assert "pointer" not in state
    assert state["typing"] is True


def test_a_non_finite_pointer_never_reaches_the_payload() -> None:
    """NaN and the infinities are rejected, not clamped.

    Worth its own test at the unit level because JSON cannot carry them but
    Python's float() parses the words, and `min`/`max` would wave a NaN
    through wearing the shape of a legitimate clamp — a cursor drawn nowhere,
    on everyone else's screen.
    """
    for bad in ("nan", "inf", "-inf"):
        state = coworking.sanitize_pointer({"pointer": {"x": bad, "y": "0.5"}})
        assert "pointer" not in state, bad
    # A string that IS a number still works: the check is on the value, not on
    # the type the transport happened to use.
    assert coworking.sanitize_pointer({"pointer": {"x": "0.5", "y": "0.25"}})[
        "pointer"
    ] == {"x": 0.5, "y": 0.25}


def test_sanitize_pointer_leaves_a_pointerless_state_alone() -> None:
    """Identity, not a copy: every heartbeat that is not a mouse move — every
    keystroke, every idle re-beat — goes through here."""
    state = {"typing": True, "draft": "x"}
    assert coworking.sanitize_pointer(state) is state


# ---------------------------------------------------------------------------
# The presence upsert under two writers
#
# Two heartbeats for one (actor, surface) is the ordinary case, not a corner:
# `use-coworking.ts` sends a throttled beat while the pointer clear bypasses
# the throttle to go out immediately, and a surface being left sends `leave`
# on the heels of a beat already in flight. Each lands on its own session, so
# they interleave two ways -- both INSERT (the loser breaks the unique
# constraint) or one UPDATEs a row `leave` has already deleted.
#
# Unhandled, either raised straight out of the endpoint and past CORSMiddleware,
# which only stamps Access-Control-Allow-Origin on responses it sees pass
# through. The browser then reported "blocked by CORS policy" on this one
# endpoint and features.spec.ts's `expect(errors).toEqual([])` failed on a
# phantom CORS error that was really a 500. These pin the retry, not the
# message, because the retry is what keeps the 500 from happening at all.


class _SessionSpy:
    """Only what the retry touches: a session is otherwise irrelevant here."""

    def __init__(self) -> None:
        self.rollbacks = 0

    def rollback(self) -> None:
        self.rollbacks += 1


def _raise_then_return(exc: Exception, attempts_before_success: int):
    calls: list[int] = []

    def fake_write(db: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) <= attempts_before_success:
            raise exc
        return "presence-row"

    return fake_write, calls


@pytest.mark.parametrize(
    "exc",
    [
        IntegrityError("INSERT INTO presences", {}, Exception("UNIQUE constraint")),
        StaleDataError("UPDATE statement on table 'presences' expected to update 1 row"),
    ],
    ids=["lost-the-insert", "row-deleted-under-the-update"],
)
def test_a_lost_presence_write_race_is_retried(monkeypatch: Any, exc: Exception) -> None:
    """The loser re-reads and succeeds instead of 500ing."""
    fake_write, calls = _raise_then_return(exc, attempts_before_success=1)
    monkeypatch.setattr(coworking, "_write_presence", fake_write)
    db = _SessionSpy()

    row = coworking.heartbeat_presence(
        db,
        workspace_id="w1",
        actor_id="u1",
        actor_kind="user",
        actor_label="Ann",
        surface="document:d1",
    )

    assert row == "presence-row"
    # Rolled back once -- the losing transaction is poisoned, and only a
    # rollback makes the session usable for the re-read that follows.
    assert calls == [1, 1]
    assert db.rollbacks == 1


def test_a_presence_race_that_never_settles_still_raises(monkeypatch: Any) -> None:
    """The retry is a bounded concession, not a promise to succeed.

    A permanent IntegrityError is a real defect somewhere else; swallowing it
    would turn a loud 500 into presence that silently stops updating.
    """
    exc = IntegrityError("INSERT INTO presences", {}, Exception("UNIQUE constraint"))
    fake_write, calls = _raise_then_return(exc, attempts_before_success=99)
    monkeypatch.setattr(coworking, "_write_presence", fake_write)
    db = _SessionSpy()

    with pytest.raises(IntegrityError):
        coworking.heartbeat_presence(
            db,
            workspace_id="w1",
            actor_id="u1",
            actor_kind="user",
            actor_label="Ann",
            surface="document:d1",
        )

    assert len(calls) == coworking._PRESENCE_UPSERT_ATTEMPTS
    assert db.rollbacks == coworking._PRESENCE_UPSERT_ATTEMPTS
