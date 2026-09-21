"""Memory import: batch remember, personal scope, dedupe accounting.

Every imported row is the IMPORTER'S OWN (`owner_id` = the caller) on the
global shelf (`space_id=""`): the export's "shared" flag never survives the
trip, because widening a sentence to the workspace is a human decision made
row-by-row on the Memory view. Re-importing the same file converges — rows
dedupe through `remember_memory`, only importance moves — which is why the
route carries no Idempotency-Key.
"""
from __future__ import annotations

import json

from conftest import TEST_BASE_URL, Identity, authenticate, create_identity, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Membership, MemoryItem, User, WorkspaceEvent


def client_for(identity: Identity) -> TestClient:
    return authenticate(TestClient(app, base_url=TEST_BASE_URL), identity)


def rows_for(workspace_id: str) -> list[MemoryItem]:
    db = SessionLocal()
    try:
        return list(
            db.scalars(
                select(MemoryItem).where(MemoryItem.workspace_id == workspace_id)
            )
        )
    finally:
        db.close()


def test_items_import_lands_personal_on_the_global_shelf():
    identity = create_identity()
    client = client_for(identity)
    response = client.post(
        "/api/memory/import",
        json={
            "items": [
                {"content": "The staging cluster lives in eu-west-1."},
                {"content": "Prefers answers with citations.", "kind": "preference"},
                {"content": "A rolling recap of everything.", "kind": "summary"},
                {"content": "Deploys happen on Tuesdays.", "kind": "whatever-else"},
            ]
        },
    )
    assert response.status_code == 200
    assert response.json() == {"added": 3, "reinforced": 0, "skipped": 1}
    rows = rows_for(identity.workspace_id)
    assert len(rows) == 3
    for row in rows:
        assert row.owner_id == identity.user_id  # personal, never SHARED_OWNER
        assert row.space_id == ""  # the global shelf sentinel
    kinds = {row.content: row.kind for row in rows}
    assert kinds["Prefers answers with citations."] == "preference"
    # An unknown kind coerces to fact rather than importing a stray vocabulary.
    assert kinds["Deploys happen on Tuesdays."] == "fact"


def test_a_replayed_import_converges_instead_of_duplicating():
    identity = create_identity()
    client = client_for(identity)
    payload = {
        "items": [
            {"content": "The staging cluster lives in eu-west-1."},
            {"content": "Deploys happen on Tuesdays."},
        ]
    }
    first = client.post("/api/memory/import", json=payload)
    assert first.json() == {"added": 2, "reinforced": 0, "skipped": 0}
    second = client.post("/api/memory/import", json=payload)
    assert second.json() == {"added": 0, "reinforced": 2, "skipped": 0}
    # Row state converged; only the importance counters moved.
    assert len(rows_for(identity.workspace_id)) == 2


def test_the_text_door_creates_a_fact_per_nonblank_line():
    identity = create_identity()
    client = client_for(identity)
    response = client.post(
        "/api/memory/import",
        json={"text": "First fact.\n\n   \nSecond fact.\nThird fact.\n"},
    )
    assert response.status_code == 200
    assert response.json() == {"added": 3, "reinforced": 0, "skipped": 0}
    assert all(row.kind == "fact" for row in rows_for(identity.workspace_id))


def test_the_text_door_truncates_past_the_cap_and_counts_the_rest():
    identity = create_identity()
    client = client_for(identity)
    text = "\n".join(f"Line number {index} is a fact." for index in range(205))
    response = client.post("/api/memory/import", json={"text": text})
    assert response.status_code == 200
    assert response.json() == {"added": 200, "reinforced": 0, "skipped": 5}


def test_more_than_200_items_is_refused_at_the_door():
    # The items door is bounded by the schema (Field max_length=200), so the
    # overload answer is a 422 rather than a silent truncation.
    identity = create_identity()
    client = client_for(identity)
    response = client.post(
        "/api/memory/import",
        json={"items": [{"content": f"item {index}"} for index in range(201)]},
    )
    assert response.status_code == 422


def test_neither_items_nor_text_is_a_422():
    client = client_for(create_identity())
    assert client.post("/api/memory/import", json={}).status_code == 422


def test_imported_rows_are_invisible_to_a_colleague():
    owner = create_identity()
    db = SessionLocal()
    try:
        colleague = User(
            email=f"import-roommate-{owner.workspace_id[:8]}@example.com",
            name="Import Roommate",
        )
        db.add(colleague)
        db.flush()
        db.add(
            Membership(
                workspace_id=owner.workspace_id, user_id=colleague.id, role="member"
            )
        )
        db.commit()
        colleague_id = colleague.id
    finally:
        db.close()
    token, csrf_token = issue_session(colleague_id)
    roommate = Identity(
        user_id=colleague_id,
        workspace_id=owner.workspace_id,
        token=token,
        csrf_token=csrf_token,
    )
    assert (
        client_for(owner)
        .post(
            "/api/memory/import",
            json={"items": [{"content": "The importer's private fact."}]},
        )
        .status_code
        == 200
    )
    listed = client_for(roommate).get("/api/memory")
    assert listed.status_code == 200
    assert "private fact" not in listed.text


def test_one_aggregated_event_carries_the_importers_ownership():
    identity = create_identity()
    client = client_for(identity)
    response = client.post(
        "/api/memory/import",
        json={"items": [{"content": "One."}, {"content": "Two."}]},
    )
    assert response.status_code == 200
    db = SessionLocal()
    try:
        events = list(
            db.scalars(
                select(WorkspaceEvent).where(
                    WorkspaceEvent.workspace_id == identity.workspace_id,
                    WorkspaceEvent.event_type == "memory.updated",
                )
            )
        )
    finally:
        db.close()
    assert len(events) == 1
    payload = json.loads(events[0].payload_json)
    assert payload["count"] == 2
    assert len(payload["ids"]) == 2
    assert payload["owner_id"] == identity.user_id
