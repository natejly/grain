"""Mid-turn steering over HTTP: `POST /api/runs/{run_id}/steer`.

The loop-side half — how `_absorb_steering` folds the event into the transcript
— belongs to the agent-loop tests. These prove the route's contract: only an
in-flight run accepts a steer, and an accepted one leaves exactly two traces —
a `run.steer` run event for the loop to poll, and a user Message carrying
the run's own id — the same shape as the turn's prompt message, so a queued
start or a lease-recovery re-run (which excludes the current run's messages
from its transcript) cannot meet the steer twice.

Runs are planted directly with status "running", the way
`test_agent_loop._make_run` does — a genuinely running turn would race the
assertions. Each test purges its conversation afterwards (the DELETE cascades
runs, events and messages), because the seeded workspace is shared.
"""
from __future__ import annotations

import json
import uuid

from app.database import SessionLocal
from app.models import Message, Run, RunEvent


def _headers() -> dict:
    return {"Idempotency-Key": "steer-" + uuid.uuid4().hex}


def _make_run(client, *, status: str) -> tuple[str, str]:
    """A conversation of the seeded owner's, holding one directly planted run."""
    identity = client.get("/api/bootstrap").json()["identity"]
    conversation = client.post(
        "/api/conversations",
        headers=_headers(),
        json={"title": "Steer target"},
    ).json()
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=identity["workspace_id"],
            conversation_id=conversation["id"],
            agent_id=client.get("/api/bootstrap").json()["default_agent_id"],
            created_by=identity["user_id"],
            status=status,
            prompt="What is in the sources about Atlas?",
        )
        db.add(run)
        db.commit()
        return run.id, conversation["id"]
    finally:
        db.close()


def _purge(client, conversation_id: str) -> None:
    """Cascade the conversation and everything planted under it."""
    client.delete(f"/api/conversations/{conversation_id}", headers=_headers())


def _steer_events(db, run_id: str) -> list[RunEvent]:
    return (
        db.query(RunEvent)
        .filter(RunEvent.run_id == run_id, RunEvent.event_type == "run.steer")
        .all()
    )


def test_steering_a_running_run_records_the_event_and_a_user_message(client):
    run_id, conversation_id = _make_run(client, status="running")
    try:
        response = client.post(
            f"/api/runs/{run_id}/steer",
            headers=_headers(),
            json={"content": "Focus on the Q3 numbers instead."},
        )
        assert response.status_code == 202
        assert response.json()["message"]["run_id"] == run_id

        db = SessionLocal()
        try:
            events = _steer_events(db, run_id)
            assert len(events) == 1
            payload = json.loads(events[0].payload_json)
            assert payload["content"] == "Focus on the Q3 numbers instead."

            messages = (
                db.query(Message)
                .filter(Message.conversation_id == conversation_id)
                .all()
            )
            assert len(messages) == 1
            message = messages[0]
            assert message.role == "user"
            assert message.content == "Focus on the Q3 numbers instead."
            # Stamped with the run's own id, like the turn's prompt message:
            # `_transcript` excludes the current run's messages, so a queued
            # start or a recovery re-run absorbs the steer from its events
            # without ALSO meeting it in the transcript. Later turns see it as
            # ordinary history.
            assert message.run_id == run_id
        finally:
            db.close()
    finally:
        _purge(client, conversation_id)


def test_steering_a_completed_run_is_refused_with_a_409(client):
    run_id, conversation_id = _make_run(client, status="completed")
    try:
        response = client.post(
            f"/api/runs/{run_id}/steer",
            headers=_headers(),
            json={"content": "Too late for this."},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "This run has finished — send the note as a new message"
        db = SessionLocal()
        try:
            assert _steer_events(db, run_id) == []
            assert (
                db.query(Message)
                .filter(Message.conversation_id == conversation_id)
                .count()
                == 0
            )
        finally:
            db.close()
    finally:
        _purge(client, conversation_id)


def test_steering_a_missing_run_is_a_404(client):
    response = client.post(
        f"/api/runs/{uuid.uuid4()}/steer",
        headers=_headers(),
        json={"content": "Anyone there?"},
    )
    assert response.status_code == 404


def test_steering_with_empty_content_is_a_422(client):
    run_id, conversation_id = _make_run(client, status="running")
    try:
        response = client.post(
            f"/api/runs/{run_id}/steer", headers=_headers(), json={"content": ""}
        )
        assert response.status_code == 422
    finally:
        _purge(client, conversation_id)


def test_a_replayed_steer_writes_one_event_and_one_message(client):
    run_id, conversation_id = _make_run(client, status="running")
    headers = _headers()
    try:
        first = client.post(
            f"/api/runs/{run_id}/steer",
            headers=headers,
            json={"content": "Check the appendix."},
        )
        assert first.status_code == 202
        replay = client.post(
            f"/api/runs/{run_id}/steer",
            headers=headers,
            json={"content": "Check the appendix."},
        )
        assert replay.status_code == 202
        assert replay.json()["message"]["run_id"] == run_id

        db = SessionLocal()
        try:
            assert len(_steer_events(db, run_id)) == 1
            assert (
                db.query(Message)
                .filter(
                    Message.conversation_id == conversation_id,
                    Message.content == "Check the appendix.",
                )
                .count()
                == 1
            )
        finally:
            db.close()
    finally:
        _purge(client, conversation_id)


def test_a_plain_member_cannot_steer_a_run_on_someone_elses_personal_thread(client):
    """Steer demands the authority to post in the run's conversation.

    Cancel lets any member stop an automation; steer INJECTS instructions and
    writes a user message into the thread, so a member who is neither the
    run's creator nor able to open its (personal, unshared) conversation is
    answered 404 — the same shape as a run that does not exist.
    """
    import uuid as _uuid

    from conftest import TEST_BASE_URL, Identity, authenticate, issue_session
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import Membership, User

    run_id, conversation_id = _make_run(client, status="running")
    db = SessionLocal()
    try:
        user = User(email=f"{_uuid.uuid4().hex}@example.com", name="Second member")
        db.add(user)
        db.flush()
        identity = db.get(Run, run_id)
        db.add(
            Membership(
                workspace_id=identity.workspace_id, user_id=user.id, role="member"
            )
        )
        db.commit()
        user_id = user.id
        workspace_id = identity.workspace_id
    finally:
        db.close()
    token, csrf = issue_session(user_id)
    other = authenticate(
        TestClient(app, base_url=TEST_BASE_URL),
        Identity(
            user_id=user_id, workspace_id=workspace_id, token=token, csrf_token=csrf
        ),
    )
    try:
        response = other.post(
            f"/api/runs/{run_id}/steer",
            headers={"Idempotency-Key": "steer-foreign-member"},
            json={"content": "redirect this"},
        )
        assert response.status_code == 404
        db = SessionLocal()
        try:
            assert _steer_events(db, run_id) == []
        finally:
            db.close()
    finally:
        _purge(client, conversation_id)
