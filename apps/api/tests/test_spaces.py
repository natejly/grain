"""Spaces: the container, its threads and files, and what deletion takes.

The decision this feature turns on is D6: deleting a space is destructive,
where deleting a folder refuses. A folder's documents live happily at the top
level; a space's sources have no outside to return to — re-labelling them
`space_id = ""` would drop them into the workspace library, retrievable from
every general chat, which is the one direction scoping must never fail
toward. So the cascade tests here are the ones that matter.
"""
from __future__ import annotations

import os

from conftest import TEST_BASE_URL, Identity, authenticate, create_identity
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import AuditEvent, Chunk, Conversation, MemoryItem, Message, Run, Source


def _client_for(identity: Identity) -> TestClient:
    client = TestClient(app, base_url=TEST_BASE_URL)
    client.identity = identity  # type: ignore[attr-defined]
    return authenticate(client, identity)


def _fresh_client(label: str = "Space owner") -> TestClient:
    return _client_for(create_identity(name=label, workspace_name=f"{label} ws"))


def _key() -> dict[str, str]:
    return {"Idempotency-Key": "space-test-" + os.urandom(8).hex()}


def make_space(client: TestClient, name: str, instructions: str = "") -> dict:
    response = client.post(
        "/api/spaces",
        json={"name": name, "instructions": instructions},
        headers=_key(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_thread(client: TestClient, space_id: str = "", title: str = "T") -> dict:
    response = client.post(
        "/api/conversations",
        json={"title": title, "space_id": space_id},
        headers=_key(),
    )
    return response.json() if response.status_code == 201 else response


def upload_source(client: TestClient, space_id: str = "", name: str = "notes.md"):
    data = {"space_id": space_id} if space_id else {}
    return client.post(
        "/api/sources",
        files={"file": (name, b"kestrel deploys on tuesdays", "text/markdown")},
        data=data,
        headers=_key(),
    )


# --------------------------------------------------------------------------
# CRUD


def test_a_space_is_created_listed_and_read_back() -> None:
    client = _fresh_client()
    space = make_space(client, "  Research  ", "Cite primary sources.")
    assert space["name"] == "Research"
    assert space["instructions"] == "Cite primary sources."
    rows = client.get("/api/spaces").json()
    assert [(row["id"], row["thread_count"], row["source_count"]) for row in rows] == [
        (space["id"], 0, 0)
    ]
    assert client.get(f"/api/spaces/{space['id']}").json()["name"] == "Research"


def test_a_blank_or_duplicate_name_is_refused() -> None:
    client = _fresh_client()
    make_space(client, "Research")
    for body in ({"name": "   "}, {"name": "research"}):
        response = client.post("/api/spaces", json=body, headers=_key())
        assert response.status_code == 422, response.text


def test_patch_edits_only_what_it_names_and_empty_clears() -> None:
    client = _fresh_client()
    space = make_space(client, "Research", "Old instructions.")
    renamed = client.patch(
        f"/api/spaces/{space['id']}", json={"name": "Field work"}
    ).json()
    assert (renamed["name"], renamed["instructions"]) == (
        "Field work",
        "Old instructions.",
    )
    cleared = client.patch(
        f"/api/spaces/{space['id']}", json={"instructions": ""}
    ).json()
    assert (cleared["name"], cleared["instructions"]) == ("Field work", "")


def test_create_replays_on_the_same_idempotency_key() -> None:
    client = _fresh_client()
    key = _key()
    first = client.post("/api/spaces", json={"name": "Once"}, headers=key)
    again = client.post("/api/spaces", json={"name": "Twice?"}, headers=key)
    assert first.json()["id"] == again.json()["id"]
    assert [row["name"] for row in client.get("/api/spaces").json()] == ["Once"]


# --------------------------------------------------------------------------
# Threads in a space


def test_a_space_thread_stays_in_the_rail_and_filters_by_space() -> None:
    client = _fresh_client()
    space = make_space(client, "Research")
    inside = make_thread(client, space["id"], title="In the space")
    outside = make_thread(client, title="Ordinary")
    assert inside["space_id"] == space["id"]
    assert outside["space_id"] == ""

    rail = {row["id"]: row["space_id"] for row in client.get("/api/conversations").json()}
    assert rail[inside["id"]] == space["id"]  # in the rail, not hidden like a subject
    assert rail[outside["id"]] == ""

    scoped = client.get("/api/conversations", params={"space_id": space["id"]}).json()
    assert [row["id"] for row in scoped] == [inside["id"]]
    unscoped = client.get("/api/conversations", params={"space_id": ""}).json()
    assert [row["id"] for row in unscoped] == [outside["id"]]

    counts = client.get("/api/spaces").json()[0]
    assert counts["thread_count"] == 1


def test_a_foreign_or_deleted_space_id_cannot_start_a_thread() -> None:
    client = _fresh_client()
    other = _fresh_client("Other tenant")
    foreign = make_space(other, "Theirs")
    for bad in (foreign["id"], "no-such-space"):
        response = client.post(
            "/api/conversations",
            json={"title": "probe", "space_id": bad},
            headers=_key(),
        )
        assert response.status_code == 404, response.text


# --------------------------------------------------------------------------
# Knowledge files in a space


def test_an_upload_lands_on_the_space_and_the_list_filters() -> None:
    client = _fresh_client()
    space = make_space(client, "Research")
    spaced = upload_source(client, space["id"])
    assert spaced.status_code == 202, spaced.text
    assert spaced.json()["space_id"] == space["id"]
    general = upload_source(client, name="library.md")
    assert general.json()["space_id"] == ""

    everything = client.get("/api/sources").json()
    assert {row["filename"] for row in everything} == {"notes.md", "library.md"}
    scoped = client.get("/api/sources", params={"space_id": space["id"]}).json()
    assert [row["filename"] for row in scoped] == ["notes.md"]

    counts = client.get("/api/spaces").json()[0]
    assert counts["source_count"] == 1


def test_a_foreign_space_id_cannot_receive_an_upload() -> None:
    client = _fresh_client()
    other = _fresh_client("Other tenant")
    foreign = make_space(other, "Theirs")
    response = upload_source(client, foreign["id"])
    assert response.status_code == 404, response.text
    # And nothing landed in either tenant's library on the way out.
    assert client.get("/api/sources").json() == []
    assert [row["space_id"] for row in other.get("/api/sources").json()] == []


# --------------------------------------------------------------------------
# Deletion is destructive — and only of the space's own contents


def test_deleting_a_space_takes_its_threads_files_and_memories() -> None:
    client = _fresh_client()
    identity = client.identity  # type: ignore[attr-defined]
    space = make_space(client, "Doomed")
    thread = make_thread(client, space["id"])
    keep_thread = make_thread(client, title="Keeper")
    doomed_source = upload_source(client, space["id"]).json()
    kept_source = upload_source(client, name="keep.md").json()

    db = SessionLocal()
    try:
        db.add(
            MemoryItem(
                workspace_id=identity.workspace_id,
                space_id=space["id"],
                content="space secret",
                normalized_key="space|secret",
            )
        )
        db.add(
            MemoryItem(
                workspace_id=identity.workspace_id,
                content="global fact",
                normalized_key="global|fact",
            )
        )
        db.commit()
    finally:
        db.close()

    response = client.delete(f"/api/spaces/{space['id']}", headers=_key())
    assert response.status_code == 204, response.text

    assert client.get("/api/spaces").json() == []
    rail = [row["id"] for row in client.get("/api/conversations").json()]
    assert thread["id"] not in rail and keep_thread["id"] in rail
    filenames = {row["id"] for row in client.get("/api/sources").json()}
    assert filenames == {kept_source["id"]}

    db = SessionLocal()
    try:
        # The thread's whole record went with it, not just the listing row.
        assert db.query(Conversation).filter_by(id=thread["id"]).count() == 0
        assert (
            db.query(Message).filter_by(conversation_id=thread["id"]).count() == 0
        )
        assert db.query(Run).filter_by(conversation_id=thread["id"]).count() == 0
        # The source is tombstoned and unindexed, like a route delete.
        source_row = db.query(Source).filter_by(id=doomed_source["id"]).one()
        assert source_row.deleted_at is not None
        assert db.query(Chunk).filter_by(source_id=doomed_source["id"]).count() == 0
        # The space's shelf is gone; the global shelf is untouched.
        keys = {
            row.normalized_key
            for row in db.query(MemoryItem).filter_by(
                workspace_id=identity.workspace_id
            )
        }
        assert "space|secret" not in keys and "global|fact" in keys
        audit = (
            db.query(AuditEvent)
            .filter_by(workspace_id=identity.workspace_id, action="space.deleted")
            .one()
        )
        assert audit.resource_id == space["id"]
    finally:
        db.close()


def test_delete_replays_on_its_key_and_404s_on_a_fresh_one() -> None:
    client = _fresh_client()
    space = make_space(client, "Twice deleted")
    key = _key()
    assert client.delete(f"/api/spaces/{space['id']}", headers=key).status_code == 204
    assert client.delete(f"/api/spaces/{space['id']}", headers=key).status_code == 204
    assert (
        client.delete(f"/api/spaces/{space['id']}", headers=_key()).status_code == 404
    )
