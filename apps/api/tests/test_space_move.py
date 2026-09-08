"""Moving a thread between spaces, and what a thread's deletion takes along.

Two halves of the same container story. `PUT /api/conversations/{id}/space`
is what makes a space a container rather than a birthmark — a thread can be
filed into the project it grew into, and the scope it works in follows the
column, not a cache. And a container is only trustworthy if leaving it is
clean: deleting a thread (alone, or via its space) must take its attachment
rows, its scoped sources and their bytes, because an orphaned scoped source
is a leak waiting for a scheme change.

The graph test at the bottom is the space twin of the attachment one in
`test_chat_attachments.py`: the workspace graph is built from the library
alone, so a space's knowledge files stay out of it.
"""
from __future__ import annotations

import os
from pathlib import Path

from conftest import TEST_BASE_URL, Identity, authenticate, create_identity, issue_session
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import ChatAttachment, Membership, Source, User
from app.services import spaces as spaces_service
from app.services.graph import rebuild_graph
from app.services.memory import memory_space
from app.services.retrieval import search_evidence


def _client_for(identity: Identity) -> TestClient:
    client = TestClient(app, base_url=TEST_BASE_URL)
    client.identity = identity  # type: ignore[attr-defined]
    return authenticate(client, identity)


def _fresh_client(label: str = "Mover") -> TestClient:
    return _client_for(create_identity(name=label, workspace_name=f"{label} ws"))


def _key() -> dict[str, str]:
    return {"Idempotency-Key": "move-test-" + os.urandom(8).hex()}


def _member(workspace_id: str, *, name: str) -> TestClient:
    """A second user placed in `workspace_id`, authenticated."""
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
    return client


def _space(client: TestClient, name: str) -> dict:
    response = client.post("/api/spaces", json={"name": name}, headers=_key())
    assert response.status_code == 201, response.text
    return response.json()


def _thread(client: TestClient, space_id: str = "", title: str = "T") -> dict:
    response = client.post(
        "/api/conversations",
        json={"title": title, "space_id": space_id},
        headers=_key(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _move(client: TestClient, conversation_id: str, space_id: str):
    return client.put(
        f"/api/conversations/{conversation_id}/space", json={"space_id": space_id}
    )


def _workspace_of(client: TestClient) -> str:
    return client.identity.workspace_id  # type: ignore[attr-defined]


# --------------------------------------------------------------------------
# The move itself


def test_a_thread_moves_into_a_space_and_back_out() -> None:
    client = _fresh_client()
    space = _space(client, "Research")
    thread = _thread(client)
    assert thread["space_id"] == ""

    moved = _move(client, thread["id"], space["id"])
    assert moved.status_code == 200, moved.text
    assert moved.json()["space_id"] == space["id"]
    in_space = client.get("/api/conversations", params={"space_id": space["id"]}).json()
    assert [row["id"] for row in in_space] == [thread["id"]]

    back = _move(client, thread["id"], "")
    assert back.status_code == 200
    assert back.json()["space_id"] == ""
    assert client.get("/api/conversations", params={"space_id": space["id"]}).json() == []


def test_scope_follows_the_conversations_current_space() -> None:
    """Retrieval and the memory shelf read the column, so a move acts from the
    next turn on — no cache to go stale."""
    client = _fresh_client("Scoper")
    workspace_id = _workspace_of(client)
    space = _space(client, "Kestrels")
    upload = client.post(
        "/api/sources",
        files={"file": ("kestrel.md", b"the kestrel hovers into the wind", "text/markdown")},
        data={"space_id": space["id"]},
        headers=_key(),
    )
    assert upload.status_code == 202, upload.text
    thread = _thread(client)

    db = SessionLocal()
    try:
        def seen_from_thread() -> set[str]:
            space_id = spaces_service.space_id_for_conversation(
                db, workspace_id=workspace_id, conversation_id=thread["id"]
            )
            return {
                item.filename
                for item in search_evidence(
                    db, workspace_id=workspace_id, query="kestrel", space_id=space_id
                )
            }

        assert "kestrel.md" not in seen_from_thread()
        assert _move(client, thread["id"], space["id"]).status_code == 200
        db.expire_all()
        assert "kestrel.md" in seen_from_thread()
        assert memory_space(db, thread["id"]) == space["id"]
        assert _move(client, thread["id"], "").status_code == 200
        db.expire_all()
        assert "kestrel.md" not in seen_from_thread()
        assert memory_space(db, thread["id"]) == ""
    finally:
        db.close()


def test_a_foreign_deleted_or_unknown_space_is_a_404() -> None:
    client = _fresh_client("Refused")
    thread = _thread(client)
    stranger = _fresh_client("Stranger")
    foreign = _space(stranger, "Not yours")
    assert _move(client, thread["id"], foreign["id"]).status_code == 404
    assert _move(client, thread["id"], "no-such-space").status_code == 404

    doomed = _space(client, "Doomed")
    assert client.delete(f"/api/spaces/{doomed['id']}", headers=_key()).status_code == 204
    assert _move(client, thread["id"], doomed["id"]).status_code == 404
    # And nothing above changed the thread.
    assert (
        client.get("/api/conversations").json()[0]["space_id"] == ""
    )


def test_a_subject_thread_cannot_be_filed_into_a_space() -> None:
    client = _fresh_client("Subject")
    space = _space(client, "Somewhere")
    document = client.post(
        "/api/documents", json={"title": "Notes", "content": "hello"}
    ).json()
    subject_thread = client.post(f"/api/documents/{document['id']}/conversation").json()
    response = _move(client, subject_thread["id"], space["id"])
    assert response.status_code == 409


def test_another_members_personal_thread_cannot_be_moved() -> None:
    client = _fresh_client("Owner")
    space = _space(client, "Team space")
    other = _member(_workspace_of(client), name="Teammate")
    theirs = other.post(
        "/api/conversations", json={"title": "Private"}, headers=_key()
    ).json()
    # Invisible to `client`, so the move must 404 — but a member the thread IS
    # visible to (shared) may move it, same as rename.
    assert _move(client, theirs["id"], space["id"]).status_code == 404
    other.put(f"/api/conversations/{theirs['id']}/share", json={"shared": True})
    shared_move = _move(client, theirs["id"], space["id"])
    assert shared_move.status_code == 200
    assert shared_move.json()["space_id"] == space["id"]


# --------------------------------------------------------------------------
# What deletion takes along


def _attach_pdf(client: TestClient, conversation_id: str, filename: str):
    response = client.post(
        f"/api/conversations/{conversation_id}/attachments",
        files={"file": (filename, b"%PDF-1.4 kestrel facts %%EOF", "application/pdf")},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def _attachment_rows(conversation_id: str) -> list[ChatAttachment]:
    db = SessionLocal()
    try:
        return list(
            db.query(ChatAttachment)
            .filter(ChatAttachment.conversation_id == conversation_id)
            .all()
        )
    finally:
        db.close()


def _live_source_files(workspace_id: str, conversation_id: str) -> list[tuple[str, str]]:
    """(filename, object_key) of the thread's live scoped sources."""
    db = SessionLocal()
    try:
        return [
            (row.filename, row.object_key)
            for row in db.query(Source)
            .filter(
                Source.workspace_id == workspace_id,
                Source.conversation_id == conversation_id,
                Source.deleted_at.is_(None),
            )
            .all()
        ]
    finally:
        db.close()


def test_deleting_a_thread_takes_its_attachments_rows_sources_and_bytes() -> None:
    client = _fresh_client("Purger")
    workspace_id = _workspace_of(client)
    thread = _thread(client, title="Doomed thread")
    _attach_pdf(client, thread["id"], "doomed.pdf")
    files = _live_source_files(workspace_id, thread["id"])
    assert files, "the attachment should have produced a scoped source"
    object_key = files[0][1]
    assert Path(object_key).exists()

    delete = client.delete(f"/api/conversations/{thread['id']}", headers=_key())
    assert delete.status_code == 204, delete.text

    assert _attachment_rows(thread["id"]) == []
    assert _live_source_files(workspace_id, thread["id"]) == []
    assert not Path(object_key).exists()


def test_deleting_a_space_takes_its_threads_attachments_too() -> None:
    client = _fresh_client("Cascade")
    workspace_id = _workspace_of(client)
    space = _space(client, "Whole container")
    thread = _thread(client, space_id=space["id"], title="Inside")
    _attach_pdf(client, thread["id"], "inside.pdf")
    files = _live_source_files(workspace_id, thread["id"])
    assert files
    object_key = files[0][1]
    assert Path(object_key).exists()

    assert client.delete(f"/api/spaces/{space['id']}", headers=_key()).status_code == 204

    assert _attachment_rows(thread["id"]) == []
    assert _live_source_files(workspace_id, thread["id"]) == []
    assert not Path(object_key).exists()


# --------------------------------------------------------------------------
# The graph stays library-only


def test_a_space_file_is_not_projected_into_the_workspace_graph() -> None:
    client = _fresh_client("Grapher")
    identity: Identity = client.identity  # type: ignore[attr-defined]
    space = _space(client, "Birds")
    upload = client.post(
        "/api/sources",
        files={
            "file": (
                "merlin.csv",
                b"bird,note\nmerlin,a small dark falcon of open country\n",
                "text/csv",
            )
        },
        data={"space_id": space["id"]},
        headers=_key(),
    )
    assert upload.status_code == 202, upload.text
    rebuild_graph(identity.workspace_id, identity.user_id)
    blob = str(client.get("/api/graph").json()).lower()
    assert "merlin.csv" not in blob
