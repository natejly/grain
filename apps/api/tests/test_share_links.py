"""Share links: a revocable window onto one dashboard or document, and nothing else.

Three promises are pinned here, each the whole point of the feature:

- **Raw exactly once.** The token appears in the 201 that minted it and in no
  later response: not in the list (which omits even the hash), not in an
  idempotent replay of the create (the database holds only a digest, so the
  replay honestly answers with the token blank).
- **Live, not a snapshot.** A shared dashboard re-runs its query at request
  time. Data the workspace has since corrected is what the anonymous reader
  sees — a frozen copy would keep leaking the mistake for as long as the link
  lived.
- **Fail-closed, indistinguishably.** Unknown, revoked, expired and
  deleted-resource all answer the same 404: to an anonymous caller "this link
  serves nothing" must be one fact, or the differences become an oracle.

The cross-tenant DENY/SCOPED verdicts and the raw-token leak scan live in the
isolation sweep (`tests/isolation.py` plants a live link and its raw token in
`build_tenant`); this module holds the targeted behaviour the PUBLIC verdict
deliberately leaves to it.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, inspect, select

from app.clock import utcnow
from app.database import SessionLocal, engine
from app.models import AuditEvent, ShareLink

API_ROOT = Path(__file__).resolve().parents[1]

#: sum(revenue) == 60. The second fixture below sums to 600, so a shared
#: dashboard that answers 60 after the data changed is serving a snapshot.
CSV = "territory,revenue\nNorth,10\nSouth,20\nNorth,30\n"
CSV_CORRECTED = "territory,revenue\nNorth,100\nSouth,200\nNorth,300\n"


def key() -> dict[str, str]:
    return {"Idempotency-Key": "share-" + uuid.uuid4().hex}


def unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


def make_source(client, content: str) -> str:
    upload = client.post(
        "/api/sources",
        headers=key(),
        files={"file": ("deals.csv", content.encode(), "text/csv")},
    )
    assert upload.status_code == 202, upload.text
    return upload.json()["id"]


def make_dataset(client, content: str = CSV) -> dict:
    response = client.post(
        "/api/datasets",
        headers=key(),
        json={
            "name": unique("Deals"),
            "description": "Share fixture",
            "source_id": make_source(client, content),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_dashboard(client, dataset_id: str) -> dict:
    response = client.post(
        "/api/dashboards",
        headers=key(),
        json={
            "name": unique("Revenue"),
            "description": "",
            "dataset_id": dataset_id,
            "spec": {
                "visualization": "table",
                "query": {
                    "group_by": "territory",
                    "metrics": [
                        {"field": "revenue", "operation": "sum", "label": "total"}
                    ],
                    "order_by": "territory",
                },
                "x_field": "territory",
                "y_fields": ["total"],
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_document(client, content: str = "shared body") -> dict:
    response = client.post(
        "/api/documents",
        headers=key(),
        json={"title": unique("Brief"), "content": content, "kind": "markdown"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def share(client, resource_kind: str, resource_id: str) -> dict:
    response = client.post(
        "/api/share-links",
        headers=key(),
        json={"resource_kind": resource_kind, "resource_id": resource_id},
    )
    assert response.status_code == 201, response.text
    return response.json()


def total_of(rows: list[dict]) -> float:
    return sum(row["total"] for row in rows)


# --------------------------------------------------------------------------
# Schema promises


def test_the_share_links_table_is_workspace_scoped():
    """No link exists outside a workspace — the column the isolation sweep and
    the tamper digest both hang off."""
    columns = ShareLink.__table__.columns
    assert "workspace_id" in columns
    assert not columns["workspace_id"].nullable


def test_the_migration_chain_builds_the_share_links_table_the_orm_declares():
    """`alembic upgrade head` from an empty database must match `create_all` —
    production gets the alembic schema, development the metadata schema, and a
    difference between them is a bug that only appears in production."""
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'chain.db'}"
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=API_ROOT,
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "DATABASE_URL": url,
                "APP_ENV": "test",
                "MODEL_PROVIDER": "scripted",
                "SCRIPTED_MODEL_SCRIPT": "tests/scripts/agent.json",
                "PYTHONPATH": str(API_ROOT),
            },
        )
        assert result.returncode == 0, result.stderr

        migrated = inspect(create_engine(url))
        assert "share_links" in migrated.get_table_names()
        declared = inspect(engine)
        assert {column["name"] for column in migrated.get_columns("share_links")} == {
            column["name"] for column in declared.get_columns("share_links")
        }
        assert {index["name"] for index in migrated.get_indexes("share_links")} >= {
            index["name"] for index in declared.get_indexes("share_links")
        }


# --------------------------------------------------------------------------
# Raw exactly once


def test_the_raw_token_appears_once_and_never_again(client):
    document = make_document(client)
    idempotency = key()
    created = client.post(
        "/api/share-links",
        headers=idempotency,
        json={"resource_kind": "document", "resource_id": document["id"]},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    token = body["token"]
    assert token, "the 201 is the one response that carries the raw token"
    assert body["url_path"] == f"/share/{token}"
    assert body["link"]["resource_kind"] == "document"
    assert body["link"]["resource_id"] == document["id"]
    assert body["link"]["revoked_at"] is None

    # The idempotent replay names the same link but cannot re-derive the raw
    # value from the stored digest — and must not pretend otherwise.
    replayed = client.post(
        "/api/share-links",
        headers=idempotency,
        json={"resource_kind": "document", "resource_id": document["id"]},
    )
    assert replayed.status_code == 201, replayed.text
    assert replayed.json()["link"]["id"] == body["link"]["id"]
    assert replayed.json()["token"] == ""
    assert replayed.json()["url_path"] == ""

    # The list omits the token in every form: raw, hashed, or as a field name.
    listed = client.get("/api/share-links")
    assert listed.status_code == 200
    assert token not in listed.text
    assert "token" not in listed.text
    ours = [row for row in listed.json() if row["id"] == body["link"]["id"]]
    assert len(ours) == 1

    # And the audit trail recorded the mint without the credential.
    db = SessionLocal()
    try:
        audit = db.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "share_link.created",
                AuditEvent.resource_id == body["link"]["id"],
            )
        ).all()
        assert len(audit) == 1
        assert token not in audit[0].detail_json
    finally:
        db.close()


def test_sharing_something_that_is_not_yours_or_not_there_is_absent(client):
    missing = "00000000-0000-4000-8000-0000000000ff"
    for kind in ("dashboard", "document"):
        response = client.post(
            "/api/share-links",
            headers=key(),
            json={"resource_kind": kind, "resource_id": missing},
        )
        assert response.status_code == 404, response.text
    # A kind the product does not share is a validation failure, not a 500.
    response = client.post(
        "/api/share-links",
        headers=key(),
        json={"resource_kind": "workspace", "resource_id": missing},
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# The public window


def test_a_shared_document_is_served_to_an_anonymous_caller(client, anonymous_client):
    document = make_document(client, content="# Quarterly notes\nshared body")
    created = share(client, "document", document["id"])
    response = anonymous_client.get(f"/shared/{created['token']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "document"
    assert body["title"] == document["title"]
    assert body["document_kind"] == "markdown"
    assert body["content"] == "# Quarterly notes\nshared body"


def test_a_shared_dashboard_serves_live_data_not_a_snapshot(client, anonymous_client):
    dataset = make_dataset(client, CSV)
    dashboard = make_dashboard(client, dataset["id"])
    created = share(client, "dashboard", dashboard["id"])

    first = anonymous_client.get(f"/shared/{created['token']}")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["kind"] == "dashboard"
    assert body["title"] == dashboard["name"]
    assert body["columns"] == ["territory", "total"]
    assert total_of(body["rows"]) == 60
    assert body["generated_at"]
    assert body["spec_json"]

    # The workspace corrects its data. The link is a window, not a snapshot:
    # the same URL must now answer with the corrected numbers.
    versioned = client.post(
        f"/api/datasets/{dataset['id']}/versions",
        headers=key(),
        json={"source_id": make_source(client, CSV_CORRECTED)},
    )
    assert versioned.status_code == 201, versioned.text
    second = anonymous_client.get(f"/shared/{created['token']}")
    assert second.status_code == 200, second.text
    assert total_of(second.json()["rows"]) == 600


def test_every_dead_link_answers_the_same_404(client, anonymous_client):
    """Unknown, revoked, expired, deleted-resource: one indistinguishable no."""
    unknown = anonymous_client.get("/shared/not-a-token-anyone-issued")
    assert unknown.status_code == 404

    # Revoked.
    document = make_document(client)
    revocable = share(client, "document", document["id"])
    revoked = client.post(f"/api/share-links/{revocable['link']['id']}/revoke")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_at"] is not None
    after_revoke = anonymous_client.get(f"/shared/{revocable['token']}")
    assert after_revoke.status_code == 404
    assert after_revoke.json() == unknown.json()

    # Expired — future-dated links are minted API-side without an expiry, so
    # the boundary is planted directly.
    expiring = share(client, "document", document["id"])
    db = SessionLocal()
    try:
        link = db.get(ShareLink, expiring["link"]["id"])
        assert link is not None
        link.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()
    after_expiry = anonymous_client.get(f"/shared/{expiring['token']}")
    assert after_expiry.status_code == 404
    assert after_expiry.json() == unknown.json()

    # The shared thing itself was deleted.
    doomed = make_document(client)
    dangling = share(client, "document", doomed["id"])
    assert client.delete(f"/api/documents/{doomed['id']}").status_code == 204
    after_delete = anonymous_client.get(f"/shared/{dangling['token']}")
    assert after_delete.status_code == 404
    assert after_delete.json() == unknown.json()


def test_revoking_twice_keeps_the_first_timestamp_and_audits_once(client):
    document = make_document(client)
    created = share(client, "document", document["id"])
    link_id = created["link"]["id"]
    first = client.post(f"/api/share-links/{link_id}/revoke")
    assert first.status_code == 200
    second = client.post(f"/api/share-links/{link_id}/revoke")
    assert second.status_code == 200
    assert second.json()["revoked_at"] == first.json()["revoked_at"]
    db = SessionLocal()
    try:
        audits = db.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "share_link.revoked",
                AuditEvent.resource_id == link_id,
            )
        ).all()
        assert len(audits) == 1
    finally:
        db.close()


def test_a_foreign_tenants_link_cannot_be_revoked_or_listed(client, identity_client):
    """The DENY verdicts, held close to the feature as well as in the sweep."""
    document = make_document(client)
    created = share(client, "document", document["id"])
    other = identity_client()
    stolen = other.post(f"/api/share-links/{created['link']['id']}/revoke")
    assert stolen.status_code == 404
    listed = other.get("/api/share-links")
    assert listed.status_code == 200
    assert created["link"]["id"] not in listed.text
    # And the failed revoke changed nothing: the link still serves.
    assert client.get(f"/shared/{created['token']}").status_code == 200
