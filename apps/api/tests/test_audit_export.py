"""`GET /api/admin/audit-events/export`: the keyset walk over the trail.

Rows are planted directly, the way the observability tests plant runs: what is
under test is the pagination arithmetic and the filters, and driving them
through write endpoints would entangle every assertion with unrelated audit
rows those endpoints also write. Per-test workspaces, so the counts are exact.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from conftest import TEST_BASE_URL, Identity, authenticate, create_identity, issue_session
from fastapi.testclient import TestClient

from app.clock import utcnow
from app.database import SessionLocal
from app.main import app
from app.models import AuditEvent, Membership, User

EXPORT = "/api/admin/audit-events/export"


class _Workspace:
    def __init__(self) -> None:
        self.identity = create_identity(name="Audit exporter")
        self.client = authenticate(
            TestClient(app, base_url=TEST_BASE_URL), self.identity
        )

    def get(self, path: str):
        return self.client.get(path)


def _workspace() -> _Workspace:
    return _Workspace()


def _plant(ws: _Workspace, *, count: int, action: str = "thing.did") -> list[str]:
    identity = ws.identity
    base = utcnow() - timedelta(minutes=count)
    db = SessionLocal()
    try:
        ids = []
        for index in range(count):
            event = AuditEvent(
                workspace_id=identity.workspace_id,
                actor_id=identity.user_id,
                action=action,
                resource_type="thing",
                resource_id=f"thing-{index}",
                detail_json="{}",
                created_at=base + timedelta(minutes=index),
            )
            db.add(event)
            ids.append(event.id)
        db.commit()
        return ids
    finally:
        db.close()


def _drain(ws: _Workspace, *, limit: int, params: str = "") -> list[dict]:
    events: list[dict] = []
    cursor = ""
    pages = 0
    while True:
        query = f"?limit={limit}{params}" + (f"&cursor={cursor}" if cursor else "")
        page = ws.get(EXPORT + query).json()
        events.extend(page["events"])
        pages += 1
        assert pages < 50, "cursor walk did not terminate"
        if not page["next_cursor"]:
            return events
        cursor = page["next_cursor"]


def test_the_cursor_walk_drains_the_trail_exactly_once_in_order(client=None):
    ws = _workspace()
    _plant(ws, count=7)
    events = _drain(ws, limit=3)
    # Signup/seed writes its own audit rows; the planted seven are the tail.
    planted = [event for event in events if event["action"] == "thing.did"]
    assert [event["resource_id"] for event in planted] == [
        f"thing-{index}" for index in range(7)
    ]
    # Exactly once: no page boundary duplicated or skipped a row.
    assert len({event["id"] for event in events}) == len(events)
    stamps = [event["created_at"] for event in events]
    assert stamps == sorted(stamps)


def test_since_and_action_prefix_narrow_the_walk(client=None):
    ws = _workspace()
    _plant(ws, count=4, action="alpha.one")
    _plant(ws, count=3, action="beta.two")
    all_beta = _drain(ws, limit=2, params="&action=beta.")
    assert [event["action"] for event in all_beta] == ["beta.two"] * 3

    cut = utcnow() - timedelta(seconds=90)
    recent = _drain(ws, limit=100, params=f"&since={cut.isoformat()}")
    assert all(event["created_at"] >= cut.isoformat() for event in recent)


def test_a_bad_cursor_is_a_422_not_a_500(client=None):
    ws = _workspace()
    response = ws.get(EXPORT + "?cursor=not-a-cursor")
    assert response.status_code == 422
    assert "cursor" in response.json()["detail"]


def test_a_plain_member_is_refused(client=None):
    ws = _workspace()
    db = SessionLocal()
    try:
        user = User(email=f"{uuid.uuid4().hex}@example.com", name="Member")
        db.add(user)
        db.flush()
        db.add(
            Membership(
                workspace_id=ws.identity.workspace_id, user_id=user.id, role="member"
            )
        )
        db.commit()
        user_id = user.id
    finally:
        db.close()
    token, csrf = issue_session(user_id)
    member = authenticate(
        TestClient(app, base_url=TEST_BASE_URL),
        Identity(
            user_id=user_id,
            workspace_id=ws.identity.workspace_id,
            token=token,
            csrf_token=csrf,
        ),
    )
    assert member.get(EXPORT).status_code == 403


def test_another_workspaces_rows_never_appear(client=None):
    ours = _workspace()
    theirs = _workspace()
    _plant(theirs, count=3, action="foreign.row")
    events = _drain(ours, limit=100)
    assert all(event["action"] != "foreign.row" for event in events)


def test_the_cursor_tie_breaks_on_id_when_rows_share_a_timestamp(client=None):
    """The real keyset risk: a bulk write commits many rows at ONE timestamp.

    The predicate is (created_at > x) OR (created_at == x AND id > y), so a
    page boundary that falls inside a group of same-timestamp rows must resume
    at the next id, skipping and repeating nothing. Planted with an identical
    created_at to force every comparison through the id tie-breaker.
    """
    ws = _workspace()
    stamp = utcnow() - timedelta(minutes=5)
    db = SessionLocal()
    try:
        for index in range(10):
            db.add(
                AuditEvent(
                    workspace_id=ws.identity.workspace_id,
                    actor_id=ws.identity.user_id,
                    action="tie.row",
                    resource_type="thing",
                    resource_id=f"tie-{index}",
                    detail_json="{}",
                    created_at=stamp,  # identical for all ten
                )
            )
        db.commit()
    finally:
        db.close()
    # Page size 3 forces boundaries inside the ten-row same-timestamp block.
    events = _drain(ws, limit=3, params="&action=tie.")
    ids = [event["id"] for event in events]
    assert len(ids) == 10, ids
    assert len(set(ids)) == 10, "a same-timestamp row was duplicated across a page"
    # id is a uuid hex string, so the total order the keyset relies on is
    # lexicographic; the walk must come out in that order.
    assert ids == sorted(ids)
