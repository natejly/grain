"""Message feedback: one row per (message, member), upserted, visibility-gated.

The route sits behind `conversations.resolve_visible` — the transcript's own
chokepoint — so a colleague's personal thread answers 404 before the role gate
could reveal anything. Only the viewer's own verdict ever serializes into the
transcript, and nobody's note rides it at all.
"""
from __future__ import annotations

from conftest import TEST_BASE_URL, Identity, authenticate, create_identity, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Conversation, Membership, Message, MessageFeedback, User

IDEM = {"Idempotency-Key": "feedback-suite-0001"}


def client_for(identity: Identity) -> TestClient:
    return authenticate(TestClient(app, base_url=TEST_BASE_URL), identity)


def plant_thread(
    workspace_id: str, created_by: str, *, shared: bool = False
) -> tuple[str, str, str]:
    """A conversation with one user and one assistant message; returns
    (conversation_id, user_message_id, assistant_message_id)."""
    db = SessionLocal()
    try:
        conversation = Conversation(
            workspace_id=workspace_id,
            created_by=created_by,
            shared=shared,
            title="Rated thread",
        )
        db.add(conversation)
        db.flush()
        prompt = Message(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            run_id="run-x",
            role="user",
            content="what is it?",
            created_by=created_by,
        )
        answer = Message(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            run_id="run-x",
            role="assistant",
            content="it is this.",
            created_by=created_by,
        )
        db.add_all([prompt, answer])
        db.commit()
        return conversation.id, prompt.id, answer.id
    finally:
        db.close()


def add_roommate(workspace_id: str, name: str = "Roommate") -> Identity:
    db = SessionLocal()
    try:
        user = User(email=f"roommate-{name.lower()}-{workspace_id[:8]}@example.com", name=name)
        db.add(user)
        db.flush()
        db.add(Membership(workspace_id=workspace_id, user_id=user.id, role="member"))
        db.commit()
        user_id = user.id
    finally:
        db.close()
    token, csrf_token = issue_session(user_id)
    return Identity(
        user_id=user_id, workspace_id=workspace_id, token=token, csrf_token=csrf_token
    )


def feedback_rows(message_id: str) -> list[MessageFeedback]:
    db = SessionLocal()
    try:
        return list(
            db.scalars(
                select(MessageFeedback).where(MessageFeedback.message_id == message_id)
            )
        )
    finally:
        db.close()


def test_up_then_down_leaves_one_row_with_the_last_verdict():
    identity = create_identity()
    client = client_for(identity)
    _, _, answer_id = plant_thread(identity.workspace_id, identity.user_id)
    up = client.post(f"/api/messages/{answer_id}/feedback", json={"verdict": "up"})
    assert up.status_code == 200
    down = client.post(
        f"/api/messages/{answer_id}/feedback",
        json={"verdict": "down", "note": "misses the point"},
    )
    assert down.status_code == 200
    rows = feedback_rows(answer_id)
    assert len(rows) == 1
    assert rows[0].verdict == "down"
    assert rows[0].note == "misses the point"
    # And a later up clears nothing it shouldn't: still one row, note replaced.
    again = client.post(
        f"/api/messages/{answer_id}/feedback", json={"verdict": "up", "note": ""}
    )
    assert again.status_code == 200
    rows = feedback_rows(answer_id)
    assert len(rows) == 1
    assert rows[0].verdict == "up"
    assert rows[0].note == ""


def test_feedback_on_a_user_message_is_refused():
    identity = create_identity()
    client = client_for(identity)
    _, prompt_id, _ = plant_thread(identity.workspace_id, identity.user_id)
    response = client.post(
        f"/api/messages/{prompt_id}/feedback", json={"verdict": "up"}
    )
    assert response.status_code == 422


def test_a_colleagues_personal_thread_is_a_404_and_a_shared_one_is_not():
    owner = create_identity()
    roommate = add_roommate(owner.workspace_id)
    _, _, personal_answer = plant_thread(owner.workspace_id, owner.user_id)
    _, _, shared_answer = plant_thread(
        owner.workspace_id, owner.user_id, shared=True
    )
    roommate_client = client_for(roommate)
    hidden = roommate_client.post(
        f"/api/messages/{personal_answer}/feedback", json={"verdict": "up"}
    )
    # Invisible, not forbidden: indistinguishable from a message that is not.
    assert hidden.status_code == 404
    visible = roommate_client.post(
        f"/api/messages/{shared_answer}/feedback", json={"verdict": "up"}
    )
    assert visible.status_code == 200


def test_a_foreign_workspaces_message_is_a_404():
    ours = create_identity()
    theirs = create_identity()
    _, _, foreign_answer = plant_thread(theirs.workspace_id, theirs.user_id)
    response = client_for(ours).post(
        f"/api/messages/{foreign_answer}/feedback", json={"verdict": "up"}
    )
    assert response.status_code == 404


def test_the_transcript_serializes_only_the_viewers_own_verdict():
    owner = create_identity()
    roommate = add_roommate(owner.workspace_id)
    conversation_id, _, answer_id = plant_thread(
        owner.workspace_id, owner.user_id, shared=True
    )
    owner_client = client_for(owner)
    assert (
        owner_client.post(
            f"/api/messages/{answer_id}/feedback",
            json={"verdict": "down", "note": "a private grumble"},
        ).status_code
        == 200
    )
    rows = owner_client.get(f"/api/conversations/{conversation_id}/messages").json()
    by_id = {row["id"]: row for row in rows}
    assert by_id[answer_id]["my_feedback"] == "down"
    # The note is feedback about the answer, never part of the transcript.
    assert "a private grumble" not in str(rows)

    colleague_rows = client_for(roommate).get(
        f"/api/conversations/{conversation_id}/messages"
    ).json()
    colleague_by_id = {row["id"]: row for row in colleague_rows}
    # The colleague's own (absent) verdict, not the owner's.
    assert colleague_by_id[answer_id]["my_feedback"] == ""
    assert "a private grumble" not in str(colleague_rows)


def test_purging_the_conversation_takes_the_feedback_rows_with_it():
    identity = create_identity()
    client = client_for(identity)
    conversation_id, _, answer_id = plant_thread(
        identity.workspace_id, identity.user_id
    )
    assert (
        client.post(
            f"/api/messages/{answer_id}/feedback", json={"verdict": "up"}
        ).status_code
        == 200
    )
    assert feedback_rows(answer_id)
    deleted = client.delete(
        f"/api/conversations/{conversation_id}", headers=IDEM
    )
    assert deleted.status_code in {200, 204}
    # No orphan FK: the purge cascade removed the verdict with its message.
    assert feedback_rows(answer_id) == []
