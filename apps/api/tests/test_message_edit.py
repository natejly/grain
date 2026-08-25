"""Message edit: rewrite a prompt and re-run the conversation from there.

The edit IS a truncation — the pivot's run and everything after it is deleted,
a fresh turn is queued — so these tests are mostly about what must go with the
turn (tool calls, run events, the derived chunk index, extracted memory) and
what must survive (the usage ledger, earlier turns), plus the three gates:
authorship, only-a-prompt, and never-over-a-live-run.
"""
from __future__ import annotations

import os
from datetime import datetime

import pytest
from conftest import Identity, create_identity, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AgentToolCall,
    Conversation,
    ConversationChunk,
    Membership,
    MemoryItem,
    Message,
    Run,
    RunEvent,
    User,
    new_id,
)

TEST_BASE_URL = "http://testserver.local"


def _client_for(identity: Identity) -> TestClient:
    client = TestClient(app, base_url=TEST_BASE_URL)
    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, identity.token)
    client.headers[settings.csrf_header_name] = identity.csrf_token
    return client


def _key() -> dict[str, str]:
    return {"Idempotency-Key": "edit-test-" + os.urandom(8).hex()}


@pytest.fixture
def tenant() -> tuple[TestClient, Identity]:
    identity = create_identity(name="Editor")
    return _client_for(identity), identity


def _thread(client: TestClient, title: str = "Edit thread") -> str:
    created = client.post("/api/conversations", headers=_key(), json={"title": title})
    assert created.status_code == 201
    return created.json()["id"]


def _send(client: TestClient, conversation_id: str, content: str) -> dict:
    sent = client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers=_key(),
        json={"content": content},
    )
    assert sent.status_code == 202
    return sent.json()


def _messages(client: TestClient, conversation_id: str) -> list[dict]:
    listed = client.get(f"/api/conversations/{conversation_id}/messages")
    assert listed.status_code == 200
    return listed.json()


def test_editing_a_prompt_truncates_and_reruns(tenant):
    client, _ = tenant
    conversation_id = _thread(client)
    first = _send(client, conversation_id, "first question")
    _send(client, conversation_id, "second question")
    before = _messages(client, conversation_id)
    assert [m["content"] for m in before if m["role"] == "user"] == [
        "first question",
        "second question",
    ]
    removed_run_ids = {m["run_id"] for m in before}

    edited = client.post(
        f"/api/conversations/{conversation_id}/messages/{first['message']['id']}/edit",
        headers=_key(),
        json={"content": "first question, asked better"},
    )
    assert edited.status_code == 202
    new_run_id = edited.json()["run"]["id"]
    assert new_run_id not in removed_run_ids

    after = _messages(client, conversation_id)
    users = [m for m in after if m["role"] == "user"]
    assert [m["content"] for m in users] == ["first question, asked better"]
    # Nothing from the removed turns survives, in any table that hangs off them.
    db = SessionLocal()
    try:
        assert (
            db.scalars(select(Run.id).where(Run.id.in_(removed_run_ids))).first()
            is None
        )
        assert (
            db.scalars(
                select(RunEvent.id).where(RunEvent.run_id.in_(removed_run_ids))
            ).first()
            is None
        )
        assert (
            db.scalars(
                select(AgentToolCall.id).where(
                    AgentToolCall.run_id.in_(removed_run_ids)
                )
            ).first()
            is None
        )
        # The derived chunk index was dropped whole and rebuilt by the new
        # run's post-run hook — so chunks may exist again, but none may quote
        # the removed turns. This is the privacy property: an edited-away
        # question must not stay searchable.
        chunks = db.scalars(
            select(ConversationChunk.content).where(
                ConversationChunk.conversation_id == conversation_id
            )
        ).all()
        assert not any("second question" in content for content in chunks)
    finally:
        db.close()


def test_memory_from_a_removed_turn_is_tombstoned(tenant):
    client, identity = tenant
    conversation_id = _thread(client)
    first = _send(client, conversation_id, "remember my deadline is friday")
    run_id = first["run"]["id"]
    db = SessionLocal()
    try:
        db.add(
            MemoryItem(
                id=new_id(),
                workspace_id=identity.workspace_id,
                owner_id=identity.user_id,
                conversation_id=conversation_id,
                run_id=run_id,
                kind="fact",
                content="The deadline is friday",
                normalized_key="deadline-friday-" + os.urandom(3).hex(),
            )
        )
        db.commit()
    finally:
        db.close()

    assert (
        client.post(
            f"/api/conversations/{conversation_id}/messages/{first['message']['id']}/edit",
            headers=_key(),
            json={"content": "actually, forget the deadline"},
        ).status_code
        == 202
    )
    db = SessionLocal()
    try:
        memory = db.scalar(
            select(MemoryItem).where(MemoryItem.run_id == run_id)
        )
        assert memory is not None
        assert memory.status == "deleted"
    finally:
        db.close()


def test_only_the_author_edits_and_only_real_prompts(tenant):
    client, identity = tenant
    conversation_id = _thread(client)
    first = _send(client, conversation_id, "my words")
    message_id = first["message"]["id"]

    # An aside never ran, so there is nothing to re-run.
    aside = client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers=_key(),
        json={"content": "a side note", "aside": True},
    )
    assert (
        client.post(
            f"/api/conversations/{conversation_id}/messages/{aside.json()['message']['id']}/edit",
            headers=_key(),
            json={"content": "rewrite the note"},
        ).status_code
        == 422
    )

    # The assistant's answer is not the caller's to put words into.
    assistant = next(
        (m for m in _messages(client, conversation_id) if m["role"] == "assistant"),
        None,
    )
    if assistant is not None:
        assert (
            client.post(
                f"/api/conversations/{conversation_id}/messages/{assistant['id']}/edit",
                headers=_key(),
                json={"content": "assistant says whatever I want"},
            ).status_code
            == 422
        )

    # Another workspace sees nothing at all — 404 on the workspace filter.
    outsider = _client_for(create_identity(name="Outsider", workspace_name="Elsewhere"))
    assert (
        outsider.post(
            f"/api/conversations/{conversation_id}/messages/{message_id}/edit",
            headers=_key(),
            json={"content": "hijack"},
        ).status_code
        == 404
    )


def test_a_live_run_blocks_the_edit(tenant):
    client, identity = tenant
    conversation_id = _thread(client)
    # A turn parked on an approval, staged directly: the route must refuse to
    # delete a transcript out from under a leased loop.
    db = SessionLocal()
    try:
        agent_id = db.scalar(
            select(Agent.id).where(Agent.workspace_id == identity.workspace_id)
        )
        run = Run(
            id=new_id(),
            workspace_id=identity.workspace_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            created_by=identity.user_id,
            status="waiting_for_approval",
            prompt="parked turn",
        )
        message = Message(
            id=new_id(),
            workspace_id=identity.workspace_id,
            conversation_id=conversation_id,
            run_id=run.id,
            role="user",
            created_by=identity.user_id,
            content="parked turn",
        )
        db.add_all([run, message])
        db.commit()
        message_id = message.id
    finally:
        db.close()
    refused = client.post(
        f"/api/conversations/{conversation_id}/messages/{message_id}/edit",
        headers=_key(),
        json={"content": "rewrite over a live run"},
    )
    assert refused.status_code == 409


def test_edit_replays_on_the_same_key(tenant):
    client, _ = tenant
    conversation_id = _thread(client)
    first = _send(client, conversation_id, "original")
    key = _key()
    once = client.post(
        f"/api/conversations/{conversation_id}/messages/{first['message']['id']}/edit",
        headers=key,
        json={"content": "revised"},
    )
    assert once.status_code == 202
    again = client.post(
        f"/api/conversations/{conversation_id}/messages/{first['message']['id']}/edit",
        headers=key,
        json={"content": "revised"},
    )
    assert again.status_code == 202
    assert again.json()["replayed"] is True
    assert again.json()["run"]["id"] == once.json()["run"]["id"]


def _member_client(workspace_id: str, *, name: str) -> tuple[TestClient, str]:
    """A fresh member of `workspace_id`, and a client authenticated as them."""
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


def _staged_turn(
    identity: Identity,
    conversation_id: str,
    *,
    prompt: str,
    run_at: datetime,
    user_at: datetime,
    answer_at: datetime | None,
    created_by: str | None = None,
) -> tuple[str, str]:
    """A finished turn written directly, with explicit clocks.

    Returns (run_id, user_message_id). The API cannot express overlapping
    turns on demand, and overlap is exactly what the regression below is
    about, so the rows are staged rather than driven.
    """
    db = SessionLocal()
    try:
        agent_id = db.scalar(
            select(Agent.id).where(Agent.workspace_id == identity.workspace_id)
        )
        run = Run(
            id=new_id(),
            workspace_id=identity.workspace_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            created_by=created_by or identity.user_id,
            status="completed",
            prompt=prompt,
            created_at=run_at,
        )
        user_message = Message(
            id=new_id(),
            workspace_id=identity.workspace_id,
            conversation_id=conversation_id,
            run_id=run.id,
            role="user",
            created_by=created_by or identity.user_id,
            content=prompt,
            created_at=user_at,
        )
        db.add_all([run, user_message])
        if answer_at is not None:
            db.add(
                Message(
                    id=new_id(),
                    workspace_id=identity.workspace_id,
                    conversation_id=conversation_id,
                    run_id=run.id,
                    role="assistant",
                    created_by=created_by or identity.user_id,
                    content=f"answer to: {prompt}",
                    created_at=answer_at,
                )
            )
        db.commit()
        return run.id, user_message.id
    finally:
        db.close()


def test_an_overlapping_earlier_turn_survives_the_edit(tenant):
    """The sweep is by run start, not message time.

    Run 1 was still streaming when the pivot was sent, so its answer lands
    AFTER the pivot. A message-time sweep would delete run 1 whole — its
    pre-pivot prompt included. Only the pivot's run may go.
    """
    client, identity = tenant
    conversation_id = _thread(client, title="Overlap thread")
    t = datetime(2026, 8, 22, 10, 0, 0)

    def at(seconds: int) -> datetime:
        return t.replace(second=seconds)

    slow_run, slow_prompt = _staged_turn(
        identity,
        conversation_id,
        prompt="slow question",
        run_at=at(0),
        user_at=at(0),
        answer_at=at(30),  # finished streaming AFTER the pivot was sent
    )
    _, pivot_id = _staged_turn(
        identity,
        conversation_id,
        prompt="impatient question",
        run_at=at(10),
        user_at=at(10),
        answer_at=at(20),
    )

    assert (
        client.post(
            f"/api/conversations/{conversation_id}/messages/{pivot_id}/edit",
            headers=_key(),
            json={"content": "impatient question, reworded"},
        ).status_code
        == 202
    )
    remaining = _messages(client, conversation_id)
    contents = [m["content"] for m in remaining]
    assert "slow question" in contents
    assert "answer to: slow question" in contents
    assert "impatient question" not in contents
    db = SessionLocal()
    try:
        assert db.get(Run, slow_run) is not None
        assert db.scalar(select(Message).where(Message.id == slow_prompt)) is not None
    finally:
        db.close()


def test_a_shared_thread_edit_never_takes_a_teammates_turn(tenant):
    client, identity = tenant
    conversation_id = _thread(client, title="Shared edit thread")
    client.put(
        f"/api/conversations/{conversation_id}/share", json={"shared": True}
    )
    member_client, member_id = _member_client(identity.workspace_id, name="Member B")
    t = datetime(2026, 8, 22, 11, 0, 0)
    _, mine = _staged_turn(
        identity,
        conversation_id,
        prompt="my turn",
        run_at=t.replace(second=0),
        user_at=t.replace(second=0),
        answer_at=t.replace(second=5),
    )
    _staged_turn(
        identity,
        conversation_id,
        prompt="their turn",
        run_at=t.replace(second=10),
        user_at=t.replace(second=10),
        answer_at=t.replace(second=15),
        created_by=member_id,
    )

    # Editing my earlier turn would sweep B's later one — refused, 409.
    refused = client.post(
        f"/api/conversations/{conversation_id}/messages/{mine}/edit",
        headers=_key(),
        json={"content": "my turn, rewritten over yours"},
    )
    assert refused.status_code == 409
    assert "teammate" in refused.json()["detail"]

    # B's own trailing turn is theirs to rewrite.
    their_pivot = next(
        m
        for m in _messages(member_client, conversation_id)
        if m["content"] == "their turn"
    )
    assert (
        member_client.post(
            f"/api/conversations/{conversation_id}/messages/{their_pivot['id']}/edit",
            headers=_key(),
            json={"content": "their turn, reworded"},
        ).status_code
        == 202
    )


def test_deleting_a_thread_deletes_its_chunk_index(tenant):
    client, identity = tenant
    conversation_id = _thread(client, title="Purge thread")
    _send(client, conversation_id, "index me")
    db = SessionLocal()
    try:
        db.add(
            ConversationChunk(
                id=new_id(),
                workspace_id=identity.workspace_id,
                conversation_id=conversation_id,
                kind="chunk",
                ordinal=1,
                content="index me — verbatim quote",
                message_count=1,
                last_message_at=datetime(2026, 8, 22, 12, 0, 0),
            )
        )
        db.commit()
    finally:
        db.close()
    assert (
        client.delete(
            f"/api/conversations/{conversation_id}", headers=_key()
        ).status_code
        in (200, 204)
    )
    db = SessionLocal()
    try:
        assert (
            db.scalars(
                select(ConversationChunk.id).where(
                    ConversationChunk.conversation_id == conversation_id
                )
            ).first()
            is None
        )
        assert db.get(Conversation, conversation_id) is None
    finally:
        db.close()
