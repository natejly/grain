"""Asides ("/btw"): a message that joins the transcript without starting a turn.

The contract is small and worth pinning exactly: an aside creates a `Message`
and nothing else — no `Run`, no run events, no agent turn, no billing — and the
next real turn reads it as context because `_transcript` collects by
conversation, not by run.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict

import pytest
from conftest import Identity
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Agent, Message, Run
from app.services.runs import _transcript


def _key(stem: str = "aside") -> Dict[str, str]:
    return {"Idempotency-Key": f"{stem}-" + os.urandom(6).hex()}


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def owner(identity_client: Callable[..., TestClient]) -> TestClient:
    return identity_client(name="Aside owner", workspace_name="Aside workspace")


def identity_of(client: TestClient) -> Identity:
    return client.identity  # type: ignore[attr-defined,no-any-return]


def new_conversation(client: TestClient) -> str:
    response = client.post(
        "/api/conversations", headers=_key("conv"), json={"title": "Asides"}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def send_aside(client: TestClient, conversation_id: str, content: str, **headers: Any) -> Any:
    return client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers=headers.get("headers") or _key(),
        json={"content": content, "aside": True},
    )


def test_an_aside_is_a_message_and_nothing_else(owner: TestClient, db: Any) -> None:
    identity = identity_of(owner)
    conversation_id = new_conversation(owner)

    response = send_aside(owner, conversation_id, "btw, prefer the second option")
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["run"] is None
    assert payload["message"]["content"] == "btw, prefer the second option"
    assert payload["message"]["run_id"] == ""

    listed = owner.get(f"/api/conversations/{conversation_id}/messages").json()
    assert [row["content"] for row in listed] == ["btw, prefer the second option"]
    assert (
        db.scalar(select(Run).where(Run.conversation_id == conversation_id)) is None
    ), "an aside must not queue a run"
    assert identity.workspace_id  # the fixture's workspace held it, nobody else's


def test_an_aside_rides_the_next_turns_transcript(owner: TestClient, db: Any) -> None:
    identity = identity_of(owner)
    conversation_id = new_conversation(owner)
    send_aside(owner, conversation_id, "btw, the deadline moved to Friday")

    agent = db.scalar(select(Agent).where(Agent.workspace_id == identity.workspace_id))
    assert agent is not None
    run = Run(
        workspace_id=identity.workspace_id,
        conversation_id=conversation_id,
        agent_id=agent.id,
        created_by=identity.user_id,
        status="running",
        prompt="So when is it due?",
    )
    db.add(run)
    db.commit()

    assert ("user", "btw, the deadline moved to Friday") in _transcript(db, run)


def test_an_aside_replays_idempotently(owner: TestClient, db: Any) -> None:
    conversation_id = new_conversation(owner)
    key = _key()
    first = send_aside(owner, conversation_id, "btw once", headers=key)
    second = send_aside(owner, conversation_id, "btw once", headers=key)
    assert first.status_code == 202 and second.status_code == 202
    assert second.json()["replayed"] is True
    assert second.json()["message"]["id"] == first.json()["message"]["id"]
    rows = list(
        db.scalars(select(Message).where(Message.conversation_id == conversation_id))
    )
    assert len(rows) == 1


def test_an_aside_is_audited(owner: TestClient) -> None:
    conversation_id = new_conversation(owner)
    message_id = send_aside(owner, conversation_id, "btw").json()["message"]["id"]
    rows = [
        row
        for row in owner.get("/api/audit-events").json()
        if row["action"] == "message.aside" and row["resource_id"] == message_id
    ]
    assert [row["detail"]["conversation_id"] for row in rows] == [conversation_id]


def test_an_aside_respects_thread_visibility(
    owner: TestClient, identity_client: Callable[..., TestClient]
) -> None:
    """Same gate as a real message: a stranger's aside lands nowhere."""
    stranger = identity_client(name="Stranger", workspace_name="Elsewhere")
    conversation_id = new_conversation(owner)
    assert send_aside(stranger, conversation_id, "btw, let me in").status_code == 404
