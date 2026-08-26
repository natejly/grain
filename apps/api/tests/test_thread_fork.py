"""Forking a thread from one of its messages.

The fork route copies a transcript prefix into a brand-new personal thread.
What these tests pin down, beyond "it copies": the anchor bounds the copy
(nothing said after it comes along), the pair of ids is scoped together (my
thread + any other thread's message id is a 404, never a splice), a shared
thread is forkable by any member who can read it while the fork itself is
personal, and a foreign or invisible thread is the same 404 as any other
unreadable one. The cross-workspace DENY itself lives in the isolation sweep
(tests/isolation.py); here we prove the within-workspace halves.
"""
from __future__ import annotations

import os
from datetime import timedelta

import pytest
from conftest import TEST_BASE_URL, Identity, create_identity, issue_session
from fastapi.testclient import TestClient

from app.clock import utcnow
from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import Membership, Message, User


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
    return {"Idempotency-Key": "fork-test-" + os.urandom(8).hex()}


def _make_thread(client: TestClient, title: str) -> str:
    created = client.post("/api/conversations", headers=_key(), json={"title": title})
    assert created.status_code == 201
    return created.json()["id"]


def _plant_messages(
    conversation_id: str, workspace_id: str, created_by: str, contents: list[str]
) -> list[str]:
    """Insert a transcript directly, one second apart so the order is fixed.

    Direct rows rather than real turns: the fork copies whatever the messages
    table holds, and a scripted agent run would only add noise between the
    contents and the assertion.
    """
    db = SessionLocal()
    try:
        base = utcnow() - timedelta(seconds=len(contents))
        ids = []
        for index, content in enumerate(contents):
            row = Message(
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                run_id="run-" + os.urandom(4).hex(),
                role="user" if index % 2 == 0 else "assistant",
                content=content,
                created_by=created_by,
                created_at=base + timedelta(seconds=index),
            )
            db.add(row)
            db.flush()
            ids.append(row.id)
        db.commit()
        return ids
    finally:
        db.close()


@pytest.fixture
def workspace():
    """A workspace with its owner A and a plain member B, plus an outsider C
    alone in a wholly separate workspace."""
    owner = create_identity(name="Forker A", workspace_name="Fork workspace")
    client_a = _client_for(owner)
    client_b, user_b = _member(owner.workspace_id, name="Member B")
    outsider = create_identity(name="Outsider C", workspace_name="Elsewhere")
    client_c = _client_for(outsider)
    return {
        "a": (client_a, owner),
        "b": (client_b, user_b),
        "c": client_c,
    }


def test_a_fork_copies_the_transcript_up_to_the_anchor_and_no_further(workspace):
    client_a, owner = workspace["a"]
    source_id = _make_thread(client_a, "Planning the launch")
    message_ids = _plant_messages(
        source_id,
        owner.workspace_id,
        owner.user_id,
        ["first question", "first answer", "second question", "second answer"],
    )

    response = client_a.post(
        f"/api/conversations/{source_id}/fork",
        headers=_key(),
        json={"message_id": message_ids[1]},
    )
    assert response.status_code == 201
    fork = response.json()
    # A fork is a plain personal thread named after its source by default.
    assert fork["id"] != source_id
    assert fork["title"] == "Fork of Planning the launch"
    assert fork["shared"] is False
    assert fork["owned"] is True
    assert fork["subject_kind"] == ""

    copied = client_a.get(f"/api/conversations/{fork['id']}/messages").json()
    assert [m["content"] for m in copied] == ["first question", "first answer"]
    # Fresh rows, not references: new ids, no run linkage, sender kept.
    assert all(m["id"] not in message_ids for m in copied)
    assert all(m["run_id"] == "" for m in copied)
    assert all(m["sender_id"] == owner.user_id for m in copied)
    # And the source is untouched — still four messages, none re-parented.
    source_messages = client_a.get(f"/api/conversations/{source_id}/messages").json()
    assert [m["id"] for m in source_messages] == message_ids


def test_a_custom_title_names_the_fork_instead_of_the_default(workspace):
    client_a, owner = workspace["a"]
    source_id = _make_thread(client_a, "Original")
    (anchor_id,) = _plant_messages(
        source_id, owner.workspace_id, owner.user_id, ["only line"]
    )
    response = client_a.post(
        f"/api/conversations/{source_id}/fork",
        headers=_key(),
        json={"message_id": anchor_id, "title": "  A branch of my own  "},
    )
    assert response.status_code == 201
    assert response.json()["title"] == "A branch of my own"


def test_the_anchor_must_belong_to_the_thread_being_forked(workspace):
    """My thread + another thread's message id is a 404, never a splice.

    The pattern of test_document_version_ids_are_scoped_to_the_document: both
    ids are real and both are mine, but they name different threads, so the
    route must refuse exactly as if the message did not exist.
    """
    client_a, owner = workspace["a"]
    thread_one = _make_thread(client_a, "Thread one")
    thread_two = _make_thread(client_a, "Thread two")
    _plant_messages(thread_one, owner.workspace_id, owner.user_id, ["one's line"])
    (foreign_anchor,) = _plant_messages(
        thread_two, owner.workspace_id, owner.user_id, ["two's line"]
    )
    response = client_a.post(
        f"/api/conversations/{thread_one}/fork",
        headers=_key(),
        json={"message_id": foreign_anchor},
    )
    assert response.status_code == 404
    # Nothing was created for the refused pair.
    titles = [c["title"] for c in client_a.get("/api/conversations").json()]
    assert not any(title.startswith("Fork of") for title in titles)


def test_a_shared_thread_forks_into_the_forkers_own_personal_thread(workspace):
    client_a, owner = workspace["a"]
    client_b, user_b = workspace["b"]
    source_id = _make_thread(client_a, "Team thread")
    (anchor_id,) = _plant_messages(
        source_id, owner.workspace_id, owner.user_id, ["shared knowledge"]
    )
    shared = client_a.put(
        f"/api/conversations/{source_id}/share", json={"shared": True}
    )
    assert shared.status_code == 200

    # B can read the shared thread, so B can fork it.
    response = client_b.post(
        f"/api/conversations/{source_id}/fork",
        headers=_key(),
        json={"message_id": anchor_id},
    )
    assert response.status_code == 201
    fork = response.json()
    assert fork["shared"] is False
    assert fork["owned"] is True
    copied = client_b.get(f"/api/conversations/{fork['id']}/messages").json()
    assert [m["content"] for m in copied] == ["shared knowledge"]
    # The words keep their original speaker's attribution.
    assert copied[0]["sender_id"] == owner.user_id

    # The fork is B's personal thread: invisible to A, unreadable by id.
    assert fork["id"] not in [
        c["id"] for c in client_a.get("/api/conversations").json()
    ]
    assert (
        client_a.get(f"/api/conversations/{fork['id']}/messages").status_code == 404
    )


def test_an_invisible_thread_cannot_be_forked(workspace):
    """Another member's personal thread and another workspace's thread are the
    same 404 — visibility gates the source before the anchor is looked at."""
    client_a, owner = workspace["a"]
    client_b, _ = workspace["b"]
    client_c = workspace["c"]
    source_id = _make_thread(client_a, "A's private thread")
    (anchor_id,) = _plant_messages(
        source_id, owner.workspace_id, owner.user_id, ["private line"]
    )
    for intruder in (client_b, client_c):
        response = intruder.post(
            f"/api/conversations/{source_id}/fork",
            headers=_key(),
            json={"message_id": anchor_id},
        )
        assert response.status_code == 404


def test_replaying_the_same_idempotency_key_returns_the_same_fork(workspace):
    client_a, owner = workspace["a"]
    source_id = _make_thread(client_a, "Replayed")
    (anchor_id,) = _plant_messages(
        source_id, owner.workspace_id, owner.user_id, ["once"]
    )
    key = _key()
    first = client_a.post(
        f"/api/conversations/{source_id}/fork",
        headers=key,
        json={"message_id": anchor_id},
    )
    assert first.status_code == 201
    second = client_a.post(
        f"/api/conversations/{source_id}/fork",
        headers=key,
        json={"message_id": anchor_id},
    )
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]
    # One fork, one copy of the message — the replay created nothing.
    copied = client_a.get(f"/api/conversations/{first.json()['id']}/messages").json()
    assert [m["content"] for m in copied] == ["once"]


def test_forking_keeps_the_sources_space(workspace):
    """A fork stays findable where the original lives: same space, still a
    plain rail thread."""
    client_a, owner = workspace["a"]
    space = client_a.post(
        "/api/spaces", headers=_key(), json={"name": "Fork space " + os.urandom(3).hex()}
    )
    assert space.status_code == 201
    space_id = space.json()["id"]
    created = client_a.post(
        "/api/conversations",
        headers=_key(),
        json={"title": "Spaced thread", "space_id": space_id},
    )
    assert created.status_code == 201
    source_id = created.json()["id"]
    (anchor_id,) = _plant_messages(
        source_id, owner.workspace_id, owner.user_id, ["in a space"]
    )
    response = client_a.post(
        f"/api/conversations/{source_id}/fork",
        headers=_key(),
        json={"message_id": anchor_id},
    )
    assert response.status_code == 201
    assert response.json()["space_id"] == space_id
