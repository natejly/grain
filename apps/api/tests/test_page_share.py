"""The `page` share kind: the mint gate, the frozen payload, and revoke-on-delete.

THE LOAD-BEARING TEST HERE is "serves the frozen body after the chunk is
rewritten". `/shared/{token}` now serves three live windows and one snapshot
from one function, and a future reader "fixing the inconsistency" by making
pages live would silently destroy the feature. Documentation is weaker than a
test; this is the test. Do not delete it as redundant.
"""
from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Chunk, Message, Page, ShareLink, Source


def _seed_thread(client: TestClient) -> tuple[str, str, str]:
    """A thread with one cited answer. Returns (conversation, chunk, source)."""
    created = client.post(
        "/api/conversations",
        json={"title": "Retention"},
        headers={"Idempotency-Key": "conv-" + uuid.uuid4().hex},
    )
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    db = SessionLocal()
    try:
        workspace_id = client.identity.workspace_id  # type: ignore[attr-defined]
        source = Source(
            workspace_id=workspace_id,
            created_by="",
            filename="policy.md",
            media_type="text/markdown",
            object_key="/x/policy.md",
            byte_size=10,
            status="ready",
        )
        db.add(source)
        db.flush()
        chunk = Chunk(
            workspace_id=workspace_id,
            source_id=source.id,
            ordinal=0,
            content="The window is ninety days.",
            char_start=0,
            char_end=26,
            token_count=5,
        )
        db.add(chunk)
        db.flush()
        db.add(
            Message(
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                run_id="",
                role="assistant",
                content="Ninety days [1].",
                citations_json=json.dumps(
                    [
                        {
                            "chunk_id": chunk.id,
                            "source_id": source.id,
                            "filename": "policy.md",
                            "ordinal": 0,
                            "excerpt": chunk.content,
                            "score": 1.0,
                        }
                    ]
                ),
            )
        )
        db.commit()
        return conversation_id, chunk.id, source.id
    finally:
        db.close()


def _publish(client: TestClient, conversation_id: str, headers: dict) -> dict:
    response = client.post(
        "/api/pages",
        json={"conversation_id": conversation_id, "title": "Retention"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _mint(client: TestClient, page_id: str, headers: dict) -> str:
    response = client.post(
        "/api/share-links",
        json={"resource_kind": "page", "resource_id": page_id},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["token"]


def test_a_page_share_serves_the_frozen_body_after_the_chunk_is_rewritten(
    identity_client, anonymous_client, headers
):
    client = identity_client()
    conversation_id, chunk_id, _source_id = _seed_thread(client)
    page = _publish(client, conversation_id, headers)
    token = _mint(client, page["page"]["id"], {"Idempotency-Key": "share-link-1-aaaa"})

    db = SessionLocal()
    try:
        chunk = db.scalar(select(Chunk).where(Chunk.id == chunk_id))
        assert chunk is not None
        chunk.content = "The window is thirty days."
        db.commit()
    finally:
        db.close()

    response = anonymous_client.get(f"/shared/{token}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "page"
    # THE SNAPSHOT. The live chunk now says thirty; the page says ninety,
    # because ninety is what the answer was written from.
    assert "ninety days" in body["page_citations"][0]["frozen_excerpt"]
    assert "thirty" not in body["page_citations"][0]["frozen_excerpt"]
    assert body["page_drifted"] is False


def test_the_public_payload_reports_drift_once_the_sweep_has_run(
    identity_client, anonymous_client, headers
):
    client = identity_client()
    conversation_id, chunk_id, _source_id = _seed_thread(client)
    page = _publish(client, conversation_id, headers)
    token = _mint(client, page["page"]["id"], {"Idempotency-Key": "share-link-2-aaaa"})

    db = SessionLocal()
    try:
        chunk = db.scalar(select(Chunk).where(Chunk.id == chunk_id))
        assert chunk is not None
        chunk.content = "Rewritten."
        db.commit()
    finally:
        db.close()

    revalidated = client.post(f"/api/pages/{page['page']['id']}/revalidate")
    assert revalidated.status_code == 200, revalidated.text
    assert revalidated.json()["page"]["status"] == "drifted"

    body = anonymous_client.get(f"/shared/{token}").json()
    assert body["page_drifted"] is True
    assert body["page_citations"][0]["status"] == "changed"
    # Still the published text: the sweep TELLS, it does not rewrite.
    assert "ninety days" in body["page_citations"][0]["frozen_excerpt"]


def test_deleting_a_page_revokes_its_links_and_the_token_stops_working(
    identity_client, anonymous_client, headers
):
    client = identity_client()
    conversation_id, _chunk_id, _source_id = _seed_thread(client)
    page = _publish(client, conversation_id, headers)
    page_id = page["page"]["id"]
    token = _mint(client, page_id, {"Idempotency-Key": "share-link-3-aaaa"})
    assert anonymous_client.get(f"/shared/{token}").status_code == 200

    assert client.delete(f"/api/pages/{page_id}").status_code == 204
    gone = anonymous_client.get(f"/shared/{token}")
    assert gone.status_code == 404
    # The same sentence every other not-working link gets.
    assert gone.json()["detail"] == "Share link not found"

    db = SessionLocal()
    try:
        link = db.scalar(
            select(ShareLink).where(
                ShareLink.resource_kind == "page", ShareLink.resource_id == page_id
            )
        )
        assert link is not None and link.revoked_at is not None
        assert db.scalar(select(Page).where(Page.id == page_id)) is None
    finally:
        db.close()


def test_minting_for_a_foreign_page_404s_before_anything_is_recorded(
    identity_client, headers
):
    """The mint gate resolves the page under the CALLER'S workspace first."""
    owner = identity_client()
    conversation_id, _chunk_id, _source_id = _seed_thread(owner)
    page = _publish(owner, conversation_id, headers)

    stranger = identity_client()
    response = stranger.post(
        "/api/share-links",
        json={"resource_kind": "page", "resource_id": page["page"]["id"]},
        headers={"Idempotency-Key": "share-link-foreign-aaaa"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Page not found"


def test_publishing_is_idempotent_under_one_key(identity_client, headers):
    client = identity_client()
    conversation_id, _chunk_id, _source_id = _seed_thread(client)
    first = _publish(client, conversation_id, headers)
    second = _publish(client, conversation_id, headers)
    assert first["page"]["id"] == second["page"]["id"]
    listed = client.get("/api/pages").json()
    assert [item["id"] for item in listed] == [first["page"]["id"]]
