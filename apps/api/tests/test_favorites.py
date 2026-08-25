"""Universal Favorites: one sidebar block, eight kinds of named thing.

What these tests hold is the store's two load-bearing lines. Visibility is
proved *before* a favorite is stored — a member must not be able to use the PUT
as an oracle for ids in a colleague's personal threads, and another tenant must
not be able to use it at all. And a favorite outlives its target without ever
resurrecting it: a gone or invisible target drops out of the listing, while the
row stays for the day the target is visible again.
"""
from __future__ import annotations

import json
import uuid

from conftest import TEST_BASE_URL, Identity, authenticate, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import DEV_SEED_USER_ID, DEV_SEED_WORKSPACE_ID
from app.database import SessionLocal
from app.main import app
from app.models import Dashboard, Favorite, Membership, User


def key() -> dict[str, str]:
    return {"Idempotency-Key": "fav-" + uuid.uuid4().hex}


def unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


def make_conversation(client, title: str) -> dict:
    response = client.post("/api/conversations", headers=key(), json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


def make_document(client, title: str) -> dict:
    response = client.post("/api/documents", json={"title": title, "content": "x"})
    assert response.status_code == 201, response.text
    return response.json()


def make_cron(client, name: str) -> dict:
    response = client.post(
        "/api/crons", json={"name": name, "schedule_cron": "0 9 * * *"}
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_dashboard(client) -> tuple[str, str]:
    """A dashboard over a real uploaded dataset, the row itself planted.

    The dataset goes through the upload API — a version-less Dataset planted
    directly poisons every later test that resolves the workspace's data. Only
    the Dashboard row is direct: the API authors dashboards through the agent's
    tool, which is `test_dashboards.py`'s subject; favorites need a row with a
    name.
    """
    upload = client.post(
        "/api/sources",
        headers=key(),
        files={"file": ("deals.csv", b"territory,revenue\nNorth,10\n", "text/csv")},
    )
    assert upload.status_code == 202, upload.text
    dataset = client.post(
        "/api/datasets",
        headers=key(),
        json={
            "name": unique("Deals"),
            "description": "Favorites fixture",
            "source_id": upload.json()["id"],
        },
    )
    assert dataset.status_code == 201, dataset.text
    name = unique("Revenue")
    db = SessionLocal()
    try:
        dashboard = Dashboard(
            workspace_id=DEV_SEED_WORKSPACE_ID,
            created_by=DEV_SEED_USER_ID,
            dataset_id=dataset.json()["id"],
            name=name,
            spec_json=json.dumps(
                {
                    "visualization": "table",
                    "query": {"limit": 10},
                    "x_field": None,
                    "y_fields": [],
                }
            ),
        )
        db.add(dashboard)
        db.commit()
        return dashboard.id, name
    finally:
        db.close()


def second_member() -> tuple[TestClient, str]:
    """Another person in the *same* workspace, holding a real session.

    Not `identity_client`, which builds a whole new tenant — the isolation
    sweep owns cross-tenant. The interesting favorites questions are between
    colleagues who legitimately share every document and dashboard: whether
    they share a sidebar (they must not) and whether one can pin the other's
    personal thread (they must not).
    """
    db = SessionLocal()
    try:
        user = User(email=f"{uuid.uuid4().hex}@example.com", name="Second member")
        db.add(user)
        db.flush()
        db.add(
            Membership(
                workspace_id=DEV_SEED_WORKSPACE_ID, user_id=user.id, role="member"
            )
        )
        db.commit()
        user_id = user.id
    finally:
        db.close()
    token, csrf_token = issue_session(user_id)
    client = TestClient(app, base_url=TEST_BASE_URL)
    return (
        authenticate(
            client,
            Identity(
                user_id=user_id,
                workspace_id=DEV_SEED_WORKSPACE_ID,
                token=token,
                csrf_token=csrf_token,
            ),
        ),
        user_id,
    )


def favorite_rows(user_id: str) -> list[Favorite]:
    db = SessionLocal()
    try:
        return list(db.scalars(select(Favorite).where(Favorite.user_id == user_id)))
    finally:
        db.close()


# --------------------------------------------------------------------------
# Round trips


def test_each_kind_pins_and_lists_with_its_own_label(client):
    conversation = make_conversation(client, unique("Standup notes"))
    document = make_document(client, unique("Q3 plan"))
    cron = make_cron(client, unique("Digest"))
    dashboard_id, dashboard_name = make_dashboard(client)

    targets = [
        ("conversation", conversation["id"], conversation["title"]),
        ("document", document["id"], document["title"]),
        ("dashboard", dashboard_id, dashboard_name),
        ("cron", cron["id"], cron["name"]),
    ]
    for kind, target_id, label in targets:
        response = client.put(f"/api/favorites/{kind}/{target_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["kind"], body["target_id"], body["label"]) == (
            kind,
            target_id,
            label,
        )
    listed = {
        (entry["kind"], entry["target_id"]): entry["label"]
        for entry in client.get("/api/favorites").json()
    }
    for kind, target_id, label in targets:
        assert listed[(kind, target_id)] == label


def test_favoriting_twice_is_one_row_and_unpinning_twice_is_a_404(client):
    document = make_document(client, unique("Once"))
    for _ in range(2):
        assert client.put(f"/api/favorites/document/{document['id']}").status_code == 200
    entries = [
        entry
        for entry in client.get("/api/favorites").json()
        if entry["target_id"] == document["id"]
    ]
    assert len(entries) == 1

    assert client.delete(f"/api/favorites/document/{document['id']}").status_code == 204
    again = client.delete(f"/api/favorites/document/{document['id']}")
    assert again.status_code == 404
    assert again.json()["detail"] == "Not a favorite"


def test_an_unknown_kind_is_refused_as_malformed(client):
    target = uuid.uuid4().hex
    assert client.put(f"/api/favorites/widget/{target}").status_code == 422
    assert client.delete(f"/api/favorites/widget/{target}").status_code == 422


# --------------------------------------------------------------------------
# Visibility is proved before anything is stored


def test_a_colleague_cannot_favorite_a_personal_thread_until_it_is_shared(client):
    colleague, colleague_id = second_member()
    thread = make_conversation(client, unique("Private notes"))

    denied = colleague.put(f"/api/favorites/conversation/{thread['id']}")
    assert denied.status_code == 404
    # Refused BEFORE storing: a 404 that still wrote the row would leave the
    # favorite as a standing probe for when the thread becomes visible.
    assert favorite_rows(colleague_id) == []

    shared = client.put(
        f"/api/conversations/{thread['id']}/share", json={"shared": True}
    )
    assert shared.status_code == 200, shared.text
    allowed = colleague.put(f"/api/favorites/conversation/{thread['id']}")
    assert allowed.status_code == 200
    assert allowed.json()["label"] == thread["title"]


def test_another_tenant_sees_only_404s(client, identity_client):
    document = make_document(client, unique("Ours"))
    outsider = identity_client()
    assert outsider.put(f"/api/favorites/document/{document['id']}").status_code == 404
    assert (
        outsider.delete(f"/api/favorites/document/{document['id']}").status_code == 404
    )


# --------------------------------------------------------------------------
# A favorite outlives its target without resurrecting it


def test_a_deleted_target_drops_out_of_the_listing_but_the_row_stays(client):
    document = make_document(client, unique("Ephemeral"))
    assert client.put(f"/api/favorites/document/{document['id']}").status_code == 200
    assert client.delete(f"/api/documents/{document['id']}").status_code == 204

    listed = [entry["target_id"] for entry in client.get("/api/favorites").json()]
    assert document["id"] not in listed
    # Left, not lazily deleted: invisible is not always gone — an unshared
    # thread resolves again when it is re-shared — and a GET stays a read.
    db = SessionLocal()
    try:
        assert (
            db.scalar(select(Favorite).where(Favorite.target_id == document["id"]))
            is not None
        )
    finally:
        db.close()


# --------------------------------------------------------------------------
# Order is the caller's own


def test_the_block_reorders_in_one_write(client):
    # A fresh member, so the listing under test is exactly the two entries.
    colleague, _ = second_member()
    first = make_document(colleague, unique("First"))
    second = make_document(colleague, unique("Second"))
    for document in (first, second):
        assert (
            colleague.put(f"/api/favorites/document/{document['id']}").status_code
            == 200
        )
    assert [
        entry["target_id"] for entry in colleague.get("/api/favorites").json()
    ] == [first["id"], second["id"]]

    reordered = colleague.put(
        "/api/favorites/order",
        json={
            "entries": [
                {"kind": "document", "target_id": first["id"], "ordinal": 1},
                {"kind": "document", "target_id": second["id"], "ordinal": 0},
            ]
        },
    )
    assert reordered.status_code == 200, reordered.text
    assert [entry["target_id"] for entry in reordered.json()] == [
        second["id"],
        first["id"],
    ]

    # An order naming an entry the caller does not have saves nothing at all.
    stale = colleague.put(
        "/api/favorites/order",
        json={
            "entries": [
                {"kind": "document", "target_id": first["id"], "ordinal": 5},
                {"kind": "document", "target_id": uuid.uuid4().hex, "ordinal": 0},
            ]
        },
    )
    assert stale.status_code == 404
    assert [entry["target_id"] for entry in colleague.get("/api/favorites").json()] == [
        second["id"],
        first["id"],
    ]
