"""Comments on things, and the mentions they hand the Inbox.

A comment hangs on the polymorphic pair (`subject_kind` + `subject_id`) and a
mention writes a `Notification(kind='mention')` row — the first personal item
in the notifications store. What these tests pin down: only actual workspace
members can be mentioned (a foreign user id is dropped without comment, never
confirmed), a mention on a private thread never reaches a member who cannot
see the thread, resolving a mention empties it from the target's Inbox and
nobody else can resolve it for them, the mention set has no scan bound, and a
member's mentions are invisible to their roommate. The cross-workspace DENY
cases live in the isolation sweep (tests/isolation.py); here we prove the
within-workspace halves plus the ORM/migration parity for both new tables.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import TEST_BASE_URL, Identity, create_identity, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from app.config import get_settings
from app.database import SessionLocal, engine
from app.main import app
from app.models import Comment, Membership, Notification, User

COMMENT_TABLES = ("comments", "notifications")
API_ROOT = Path(__file__).resolve().parents[1]


def _client_for(identity: Identity) -> TestClient:
    client = TestClient(app, base_url=TEST_BASE_URL)
    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, identity.token)
    client.headers[settings.csrf_header_name] = identity.csrf_token
    return client


def _member(workspace_id: str, *, name: str) -> tuple[TestClient, str]:
    """A fresh plain member of `workspace_id`, and a client signed in as them."""
    db = SessionLocal()
    try:
        user = User(email=f"{os.urandom(6).hex()}@example.com", name=name)
        db.add(user)
        db.flush()
        db.add(Membership(workspace_id=workspace_id, user_id=user.id, role="member"))
        db.commit()
        user_id = user.id
    finally:
        db.close()
    token, csrf_token = issue_session(user_id)
    settings = get_settings()
    client = TestClient(app, base_url=TEST_BASE_URL)
    client.cookies.set(settings.session_cookie_name, token)
    client.headers[settings.csrf_header_name] = csrf_token
    return client, user_id


def _key() -> dict[str, str]:
    return {"Idempotency-Key": "comment-test-" + os.urandom(8).hex()}


@pytest.fixture
def workspace():
    """A workspace with its owner A and a plain member B, plus an outsider C
    alone in a wholly separate workspace."""
    owner = create_identity(name="Commenter A", workspace_name="Comment workspace")
    client_a = _client_for(owner)
    client_b, user_b = _member(owner.workspace_id, name="Member B")
    outsider = create_identity(name="Outsider C", workspace_name="Elsewhere")
    client_c = _client_for(outsider)
    return {
        "a": (client_a, owner),
        "b": (client_b, user_b),
        "c": (client_c, outsider),
    }


def _make_document(client: TestClient, title: str) -> str:
    created = client.post(
        "/api/documents", headers=_key(), json={"title": title, "content": "words"}
    )
    assert created.status_code == 201
    return created.json()["id"]


def _make_thread(client: TestClient, title: str) -> str:
    created = client.post("/api/conversations", headers=_key(), json={"title": title})
    assert created.status_code == 201
    return created.json()["id"]


def _comment(
    client: TestClient,
    *,
    subject_kind: str,
    subject_id: str,
    body: str,
    mentions: list[str] | None = None,
):
    return client.post(
        "/api/comments",
        headers=_key(),
        json={
            "subject_kind": subject_kind,
            "subject_id": subject_id,
            "body": body,
            "mentions": mentions or [],
        },
    )


def _mentions_of(client: TestClient) -> list[dict]:
    response = client.get("/api/inbox")
    assert response.status_code == 200
    return response.json()["mentions"]


# ---------------------------------------------------------------------------
# Schema promises
# ---------------------------------------------------------------------------


def test_every_comments_table_is_workspace_scoped():
    """No comment and no notification exists outside a workspace.

    The isolation suite proves queries filter; this proves there is a column to
    filter on in the first place, which is the part a new table gets wrong.
    """
    for model in (Comment, Notification):
        columns = model.__table__.columns
        assert "workspace_id" in columns, model.__tablename__
        assert not columns["workspace_id"].nullable, model.__tablename__


def test_the_migration_chain_builds_the_tables_the_orm_declares():
    """`alembic upgrade head` from an empty database must match `create_all`.

    Production gets the alembic schema and development gets the metadata
    schema; a comments or notifications column that exists in only one of them
    is a bug nobody sees until deploy.
    """
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'chain.db'}"
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=API_ROOT,
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "DATABASE_URL": url,
                "APP_ENV": "test",
                "MODEL_PROVIDER": "scripted",
                "SCRIPTED_MODEL_SCRIPT": "tests/scripts/agent.json",
                "PYTHONPATH": str(API_ROOT),
            },
        )
        assert result.returncode == 0, result.stderr

        migrated = inspect(create_engine(url))
        assert set(COMMENT_TABLES) <= set(migrated.get_table_names())
        declared = inspect(engine)
        for table in COMMENT_TABLES:
            assert {column["name"] for column in migrated.get_columns(table)} == {
                column["name"] for column in declared.get_columns(table)
            }, table
            assert {index["name"] for index in migrated.get_indexes(table)} >= {
                index["name"] for index in declared.get_indexes(table)
            }, table


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------


def test_comments_on_a_document_round_trip_oldest_first(workspace):
    client_a, owner = workspace["a"]
    client_b, user_b = workspace["b"]
    document_id = _make_document(client_a, "Quarterly plan")
    first = _comment(
        client_a, subject_kind="document", subject_id=document_id, body="First draft?"
    )
    assert first.status_code == 201
    second = _comment(
        client_b, subject_kind="document", subject_id=document_id, body="Ship it"
    )
    assert second.status_code == 201

    listed = client_b.get(
        "/api/comments",
        params={"subject_kind": "document", "subject_id": document_id},
    )
    assert listed.status_code == 200
    rows = listed.json()
    assert [row["body"] for row in rows] == ["First draft?", "Ship it"]
    assert rows[0]["created_by"] == owner.user_id
    assert rows[1]["created_by"] == user_b


def test_only_the_author_or_an_owner_deletes_a_comment(workspace):
    """A third member is neither, and the refusal is a 403 — the comment is
    real and visibly theirs to read, just not theirs to remove."""
    client_a, owner = workspace["a"]
    client_b, _user_b = workspace["b"]
    client_d, _user_d = _member(owner.workspace_id, name="Member D")
    document_id = _make_document(client_a, "Deletable")
    created = _comment(
        client_b, subject_kind="document", subject_id=document_id, body="mine"
    ).json()

    refused = client_d.delete(f"/api/comments/{created['id']}")
    assert refused.status_code == 403

    # The workspace owner may moderate what they did not write.
    removed = client_a.delete(f"/api/comments/{created['id']}")
    assert removed.status_code == 204
    listed = client_b.get(
        "/api/comments",
        params={"subject_kind": "document", "subject_id": document_id},
    ).json()
    assert created["id"] not in [row["id"] for row in listed]


def test_delete_answers_404_when_the_subject_is_not_visible(workspace):
    """Delete goes through the same visibility gate as create/list: a comment
    on a private thread is a 404 to a non-viewer — never a 403, which would
    confirm the comment exists — and even the workspace owner cannot moderate
    a comment on a thread they cannot see. Sharing the thread restores the
    ordinary author/owner rules."""
    client_a, _owner = workspace["a"]
    client_b, _user_b = workspace["b"]

    # B's private thread: the owner A is a non-viewer with a real comment id.
    thread_id = _make_thread(client_b, "B's private notes")
    created = _comment(
        client_b, subject_kind="conversation", subject_id=thread_id, body="mine"
    )
    assert created.status_code == 201
    comment_id = created.json()["id"]

    refused = client_a.delete(f"/api/comments/{comment_id}")
    assert refused.status_code == 404

    # Sharing makes the subject visible; now the owner gate answers normally.
    shared = client_b.put(
        f"/api/conversations/{thread_id}/share",
        headers=_key(),
        json={"shared": True},
    )
    assert shared.status_code == 200
    removed = client_a.delete(f"/api/comments/{comment_id}")
    assert removed.status_code == 204


def test_deleting_a_comment_takes_its_mention_snapshots_with_it(workspace):
    """The mention notification snapshots the comment body; deleting the
    comment must resolve the open mention AND blank the snapshot, or the
    deleted text stays readable in the mentioned inbox forever."""
    client_a, owner = workspace["a"]
    client_b, user_b = workspace["b"]
    document_id = _make_document(client_a, "Retracted")
    created = _comment(
        client_a,
        subject_kind="document",
        subject_id=document_id,
        body="regrettable words",
        mentions=[user_b],
    ).json()
    assert created["id"] in [row["comment_id"] for row in _mentions_of(client_b)]

    removed = client_a.delete(f"/api/comments/{created['id']}")
    assert removed.status_code == 204

    # Gone from the mentioned inbox, and the snapshot itself is redacted.
    assert created["id"] not in [row["comment_id"] for row in _mentions_of(client_b)]
    db = SessionLocal()
    try:
        rows = (
            db.query(Notification)
            .filter(Notification.comment_id == created["id"])
            .all()
        )
        assert rows, "the mention row must survive as a resolved record"
        for row in rows:
            assert row.status == "resolved"
            assert row.body == ""
            assert row.resolved_by == owner.user_id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Mentions
# ---------------------------------------------------------------------------


def test_a_mention_notifies_the_member_and_only_the_member(workspace):
    client_a, owner = workspace["a"]
    client_b, user_b = workspace["b"]
    document_id = _make_document(client_a, "Mention target")
    created = _comment(
        client_a,
        subject_kind="document",
        subject_id=document_id,
        body="have a look",
        mentions=[user_b],
    )
    assert created.status_code == 201
    assert created.json()["mentions"] == [user_b]

    rows = [
        row for row in _mentions_of(client_b) if row["comment_id"] == created.json()["id"]
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Commenter A mentioned you"
    assert row["body"] == "have a look"
    assert row["document_id"] == document_id
    assert row["created_by"] == owner.user_id

    # Mentions are personal: the author's own feed — the roommate axis — must
    # not list what was addressed to a colleague.
    assert created.json()["id"] not in [
        row["comment_id"] for row in _mentions_of(client_a)
    ]


def test_a_foreign_user_id_mention_is_dropped_without_comment(workspace):
    """Mentioning an id from another workspace must neither notify nor err —
    an error would confirm the id exists, which is the oracle to avoid."""
    client_a, _owner = workspace["a"]
    client_c, outsider = workspace["c"]
    document_id = _make_document(client_a, "No strangers")
    created = _comment(
        client_a,
        subject_kind="document",
        subject_id=document_id,
        body="cc someone who is not here",
        mentions=[outsider.user_id, "not-even-id-shaped"],
    )
    assert created.status_code == 201
    assert created.json()["mentions"] == []
    assert _mentions_of(client_c) == []
    db = SessionLocal()
    try:
        planted = (
            db.query(Notification)
            .filter(Notification.comment_id == created.json()["id"])
            .count()
        )
    finally:
        db.close()
    assert planted == 0


def test_a_private_thread_mention_never_reaches_a_non_viewer(workspace):
    """The notification is a deep link into the subject, so it must obey the
    subject's visibility: mentioning a member on a thread they cannot see is
    dropped exactly like a foreign id. Sharing the thread makes the same
    mention land."""
    client_a, _owner = workspace["a"]
    client_b, user_b = workspace["b"]
    thread_id = _make_thread(client_a, "Private planning")

    dropped = _comment(
        client_a,
        subject_kind="conversation",
        subject_id=thread_id,
        body="thoughts?",
        mentions=[user_b],
    )
    assert dropped.status_code == 201
    assert dropped.json()["mentions"] == []
    assert _mentions_of(client_b) == []

    shared = client_a.put(
        f"/api/conversations/{thread_id}/share",
        headers=_key(),
        json={"shared": True},
    )
    assert shared.status_code == 200
    landed = _comment(
        client_a,
        subject_kind="conversation",
        subject_id=thread_id,
        body="now you can see it",
        mentions=[user_b],
    )
    assert landed.status_code == 201
    assert landed.json()["mentions"] == [user_b]
    rows = [
        row
        for row in _mentions_of(client_b)
        if row["comment_id"] == landed.json()["id"]
    ]
    assert len(rows) == 1
    assert rows[0]["conversation_id"] == thread_id


def test_resolving_a_mention_empties_it_from_the_inbox(workspace):
    client_a, _owner = workspace["a"]
    client_b, user_b = workspace["b"]
    document_id = _make_document(client_a, "Resolve me")
    created = _comment(
        client_a,
        subject_kind="document",
        subject_id=document_id,
        body="ping",
        mentions=[user_b],
    ).json()
    mention = [
        row for row in _mentions_of(client_b) if row["comment_id"] == created["id"]
    ][0]

    resolved = client_b.post(f"/api/notifications/{mention['id']}/resolve")
    assert resolved.status_code == 200
    body = resolved.json()
    assert body["status"] == "resolved"
    assert body["resolved_by"] == user_b
    assert body["resolved_at"] is not None
    assert mention["id"] not in [row["id"] for row in _mentions_of(client_b)]

    # Resolving again is a no-op, not an error — and it does not rewrite who
    # resolved it or when.
    again = client_b.post(f"/api/notifications/{mention['id']}/resolve")
    assert again.status_code == 200
    assert again.json()["resolved_at"] == body["resolved_at"]


def test_nobody_resolves_a_mention_for_its_target(workspace):
    """Even the workspace owner: a mention addressed to a colleague is not
    theirs to clear, and the refusal is the same 404 a missing row answers."""
    client_a, _owner = workspace["a"]
    client_b, user_b = workspace["b"]
    document_id = _make_document(client_a, "Hands off")
    created = _comment(
        client_a,
        subject_kind="document",
        subject_id=document_id,
        body="for B only",
        mentions=[user_b],
    ).json()
    mention = [
        row for row in _mentions_of(client_b) if row["comment_id"] == created["id"]
    ][0]
    refused = client_a.post(f"/api/notifications/{mention['id']}/resolve")
    assert refused.status_code == 404
    assert mention["id"] in [row["id"] for row in _mentions_of(client_b)]


def test_the_mention_set_has_no_scan_bound(workspace):
    """An open mention must survive any number of newer, already-resolved
    notifications — the same contract the approvals set pins, because a window
    plus a busy notification stream is how work silently disappears."""
    client_a, owner = workspace["a"]
    client_b, user_b = workspace["b"]
    document_id = _make_document(client_a, "Buried")
    created = _comment(
        client_a,
        subject_kind="document",
        subject_id=document_id,
        body="do not lose this",
        mentions=[user_b],
    ).json()
    db = SessionLocal()
    try:
        parked = (
            db.query(Notification)
            .filter(Notification.comment_id == created["id"])
            .one()
        )
        # Milliseconds, not minutes: newer than the open mention without
        # future-dating rows past what neighbouring tests create.
        for index in range(60):
            db.add(
                Notification(
                    workspace_id=owner.workspace_id,
                    target_user_id=user_b,
                    kind="mention",
                    status="resolved",
                    title="already handled",
                    created_at=parked.created_at + timedelta(milliseconds=index + 1),
                )
            )
        db.commit()
    finally:
        db.close()
    rows = _mentions_of(client_b)
    assert created["id"] in [row["comment_id"] for row in rows]
    assert "already handled" not in [row["title"] for row in rows]


# ---------------------------------------------------------------------------
# The @-picker's member list
# ---------------------------------------------------------------------------


def test_the_member_list_offers_exactly_this_workspaces_colleagues(workspace):
    """Every member may see who they can mention — including a plain member,
    which is why this is not the admin surface — and nobody outside the
    workspace appears on it."""
    client_a, owner = workspace["a"]
    client_b, user_b = workspace["b"]
    _client_c, outsider = workspace["c"]
    for caller in (client_a, client_b):
        listed = caller.get("/api/members")
        assert listed.status_code == 200
        ids = {row["user_id"] for row in listed.json()}
        assert ids == {owner.user_id, user_b}
        assert outsider.user_id not in ids
