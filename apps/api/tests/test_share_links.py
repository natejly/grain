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

import os
import subprocess
import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from conftest import TEST_BASE_URL, create_identity, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select

from app.api import share_links as share_links_api
from app.clock import utcnow
from app.config import get_settings
from app.database import SessionLocal, engine
from app.main import app
from app.models import AuditEvent, Membership, Message, ShareLink, User

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


def test_an_expiry_minted_through_the_api_takes_effect(
    client, anonymous_client, monkeypatch
):
    """The create request's optional `expires_at` reaches the row and the
    public gate — API-minted end to end, nothing planted in the database."""
    document = make_document(client)
    expiry = (utcnow() + timedelta(hours=1)).replace(microsecond=0)
    created = client.post(
        "/api/share-links",
        headers=key(),
        json={
            "resource_kind": "document",
            "resource_id": document["id"],
            # Sent timezone-aware, as a browser would; stored naive UTC.
            "expires_at": expiry.isoformat() + "+00:00",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["link"]["expires_at"] is not None

    # The row holds the normalized expiry, and the young link still serves.
    db = SessionLocal()
    try:
        row = db.get(ShareLink, body["link"]["id"])
        assert row is not None
        assert row.expires_at == expiry
    finally:
        db.close()
    assert anonymous_client.get(f"/shared/{body['token']}").status_code == 200

    # Past the expiry, the same URL is the uniform dead-link 404.
    monkeypatch.setattr(
        "app.services.share_links.utcnow", lambda: expiry + timedelta(minutes=1)
    )
    after = anonymous_client.get(f"/shared/{body['token']}")
    assert after.status_code == 404
    unknown = anonymous_client.get("/shared/not-a-token-anyone-issued")
    assert after.json() == unknown.json()


def test_an_expiry_in_the_past_is_refused_at_the_form(client):
    document = make_document(client)
    response = client.post(
        "/api/share-links",
        headers=key(),
        json={
            "resource_kind": "document",
            "resource_id": document["id"],
            "expires_at": (utcnow() - timedelta(minutes=1)).isoformat(),
        },
    )
    assert response.status_code == 422, response.text


def test_the_public_window_is_rate_limited_per_address(client, anonymous_client):
    """Every hit — hit or miss — spends the address's budget: a shared
    dashboard is live compute, and the token path is the credential, so misses
    must be priced too. The limiter resets per test (conftest autouse).

    The budget is the shared PUBLIC tier (`api/ratelimit.public_rate_limit`),
    not the credential-endpoint knobs this route borrowed before the security
    audit landed a general limiter: an anonymous content surface a whole office
    reads from behind one NAT address needs more headroom than a login form.
    """
    document = make_document(client)
    created = share(client, "document", document["id"])
    settings = get_settings()
    for _ in range(settings.rate_limit_public_attempts):
        assert anonymous_client.get("/shared/not-a-token").status_code == 404
    # Budget spent: even the working link is refused, with a 429 not a 404.
    blocked = anonymous_client.get(f"/shared/{created['token']}")
    assert blocked.status_code == 429, blocked.text


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


# --------------------------------------------------------------------------
# Conversation share links: the transcript window and the personal-leak gate


def _client_for(identity) -> TestClient:
    settings = get_settings()
    other = TestClient(app, base_url=TEST_BASE_URL)
    other.cookies.set(settings.session_cookie_name, identity.token)
    other.headers[settings.csrf_header_name] = identity.csrf_token
    return other


def _member(workspace_id: str, *, name: str) -> TestClient:
    """A second member of the same workspace — test_conversation_sharing's
    `_member` pattern, since the personal/shared question only exists inside
    one workspace."""
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
    other = TestClient(app, base_url=TEST_BASE_URL)
    other.cookies.set(settings.session_cookie_name, token)
    other.headers[settings.csrf_header_name] = csrf_token
    return other


def _conversation(client, title: str = "Share fixture", incognito: bool = False) -> str:
    created = client.post(
        "/api/conversations",
        headers=key(),
        json={"title": title, "incognito": incognito},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _plant_turns(conversation_id: str, *, workspace_id: str, sender_id: str) -> str:
    """One user prompt and one assistant answer, planted directly so no run is
    needed. Returns the sender's display name."""
    base = utcnow()
    db = SessionLocal()
    try:
        sender = db.get(User, sender_id)
        assert sender is not None
        name = sender.name
        db.add_all(
            [
                Message(
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    run_id="run-share-1",
                    role="user",
                    content="How did the launch land?",
                    created_by=sender_id,
                    created_at=base,
                ),
                Message(
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    run_id="run-share-1",
                    role="assistant",
                    content="Smoothly — three sign-ups in the first hour.",
                    created_by=sender_id,
                    created_at=base + timedelta(seconds=1),
                ),
            ]
        )
        db.commit()
    finally:
        db.close()
    return name


def test_a_shared_conversation_is_served_to_an_anonymous_caller(anonymous_client):
    owner = create_identity(name="Sharer A", workspace_name="Conv share ws")
    client_a = _client_for(owner)
    conversation_id = _conversation(client_a, title="Launch retro")
    name = _plant_turns(
        conversation_id, workspace_id=owner.workspace_id, sender_id=owner.user_id
    )
    assert (
        client_a.put(
            f"/api/conversations/{conversation_id}/share", json={"shared": True}
        ).status_code
        == 200
    )

    # Any member may mint on a shared thread — flat authority, like documents.
    member_b = _member(owner.workspace_id, name="Member B")
    created = share(member_b, "conversation", conversation_id)

    response = anonymous_client.get(f"/shared/{created['token']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "conversation"
    assert body["title"] == "Launch retro"
    assert [m["content"] for m in body["messages"]] == [
        "How did the launch land?",
        "Smoothly — three sign-ups in the first hour.",
    ]
    # Attribution is deliberate: the mint was an explicit act by someone who
    # can read the thread, and an unattributed multi-person transcript
    # misreads who said what.
    assert body["messages"][0]["sender_name"] == name
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][1]["role"] == "assistant"
    # No credential in any form rides the public body.
    assert created["token"] not in response.text
    assert "token" not in response.text


def test_a_personal_thread_public_link_is_creator_only(anonymous_client):
    owner = create_identity(name="Private P", workspace_name="Personal mint ws")
    client_a = _client_for(owner)
    conversation_id = _conversation(client_a, title="P's own notes")
    _plant_turns(
        conversation_id, workspace_id=owner.workspace_id, sender_id=owner.user_id
    )

    # A colleague cannot even see the personal thread, so the mint 404s —
    # indistinguishable from absent, exactly like every other by-id door.
    member_b = _member(owner.workspace_id, name="Member B")
    denied = member_b.post(
        "/api/share-links",
        headers=key(),
        json={"resource_kind": "conversation", "resource_id": conversation_id},
    )
    assert denied.status_code == 404, denied.text

    # The creator may mint on their own personal thread, and the link serves.
    created = share(client_a, "conversation", conversation_id)
    served = anonymous_client.get(f"/shared/{created['token']}")
    assert served.status_code == 200, served.text
    assert served.json()["kind"] == "conversation"


def test_unsharing_darkens_a_colleagues_link_but_not_the_creators(anonymous_client):
    """The personal-leak gate, both directions: a thread unshared after a
    colleague minted goes dark through their link, while the creator's own
    link on their now-personal thread keeps serving."""
    owner = create_identity(name="Gate G", workspace_name="Leak gate ws")
    client_a = _client_for(owner)
    conversation_id = _conversation(client_a, title="Was shared once")
    _plant_turns(
        conversation_id, workspace_id=owner.workspace_id, sender_id=owner.user_id
    )
    assert (
        client_a.put(
            f"/api/conversations/{conversation_id}/share", json={"shared": True}
        ).status_code
        == 200
    )

    member_b = _member(owner.workspace_id, name="Member B")
    link_b = share(member_b, "conversation", conversation_id)
    link_a = share(client_a, "conversation", conversation_id)
    assert anonymous_client.get(f"/shared/{link_b['token']}").status_code == 200
    assert anonymous_client.get(f"/shared/{link_a['token']}").status_code == 200

    assert (
        client_a.put(
            f"/api/conversations/{conversation_id}/share", json={"shared": False}
        ).status_code
        == 200
    )

    # The colleague's link goes dark — the uniform 404, so the anonymous
    # holder learns nothing — while the creator's own keeps serving.
    darkened = anonymous_client.get(f"/shared/{link_b['token']}")
    assert darkened.status_code == 404
    unknown = anonymous_client.get("/shared/not-a-token-anyone-issued")
    assert darkened.json() == unknown.json()
    assert anonymous_client.get(f"/shared/{link_a['token']}").status_code == 200


def test_unshare_revokes_colleague_links_so_a_reshare_cannot_revive_them(
    anonymous_client,
):
    """The re-share leg the darken test stops short of: unshare REVOKES a
    colleague-minted link rather than suspending it. Without the revoke, the
    uniform 404 teaches everyone a leaked URL is dead — and a later re-share
    silently revives it, live transcript and all. The creator's own link
    keeps the existing gate semantics throughout."""
    owner = create_identity(name="Reshare R", workspace_name="Reshare ws")
    client_a = _client_for(owner)
    conversation_id = _conversation(client_a, title="Shared, unshared, reshared")
    _plant_turns(
        conversation_id, workspace_id=owner.workspace_id, sender_id=owner.user_id
    )
    assert (
        client_a.put(
            f"/api/conversations/{conversation_id}/share", json={"shared": True}
        ).status_code
        == 200
    )
    member_b = _member(owner.workspace_id, name="Member B")
    link_b = share(member_b, "conversation", conversation_id)
    link_a = share(client_a, "conversation", conversation_id)
    assert anonymous_client.get(f"/shared/{link_b['token']}").status_code == 200

    assert (
        client_a.put(
            f"/api/conversations/{conversation_id}/share", json={"shared": False}
        ).status_code
        == 200
    )
    assert anonymous_client.get(f"/shared/{link_b['token']}").status_code == 404

    # Re-share for the team: the colleague's leaked link must STAY dead —
    # revoked one-way, indistinguishable from unknown — while the creator's
    # own link serves again.
    assert (
        client_a.put(
            f"/api/conversations/{conversation_id}/share", json={"shared": True}
        ).status_code
        == 200
    )
    revived = anonymous_client.get(f"/shared/{link_b['token']}")
    assert revived.status_code == 404
    unknown = anonymous_client.get("/shared/not-a-token-anyone-issued")
    assert revived.json() == unknown.json()
    assert anonymous_client.get(f"/shared/{link_a['token']}").status_code == 200

    # The lifecycle is honest in the link list (revoked_at is stamped, so the
    # list no longer shows a dead link as live) and in the audit trail.
    rows = {row["id"]: row for row in client_a.get("/api/share-links").json()}
    assert rows[link_b["link"]["id"]]["revoked_at"] is not None
    assert rows[link_a["link"]["id"]]["revoked_at"] is None
    db = SessionLocal()
    try:
        audit = db.scalar(
            select(AuditEvent).where(
                AuditEvent.workspace_id == owner.workspace_id,
                AuditEvent.action == "share_link.revoked",
                AuditEvent.resource_id == link_b["link"]["id"],
            )
        )
        assert audit is not None
    finally:
        db.close()


def test_a_btw_aside_is_marked_in_the_public_transcript(anonymous_client):
    """A "/btw" aside (role user, run_id "") carries `is_aside`, so the public
    page can say what the Markdown export says — otherwise the context note
    renders as a prompt the assistant appears to ignore."""
    owner = create_identity(name="Aside A", workspace_name="Aside share ws")
    client_a = _client_for(owner)
    conversation_id = _conversation(client_a, title="With an aside")
    _plant_turns(
        conversation_id, workspace_id=owner.workspace_id, sender_id=owner.user_id
    )
    db = SessionLocal()
    try:
        db.add(
            Message(
                workspace_id=owner.workspace_id,
                conversation_id=conversation_id,
                run_id="",
                role="user",
                content="btw the client's real budget is 40k",
                created_by=owner.user_id,
                created_at=utcnow() + timedelta(seconds=5),
            )
        )
        db.commit()
    finally:
        db.close()
    created = share(client_a, "conversation", conversation_id)

    body = anonymous_client.get(f"/shared/{created['token']}").json()
    assert [m["is_aside"] for m in body["messages"]] == [False, False, True]
    assert body["messages"][2]["role"] == "user"
    # The full transcript fits, so nothing was cut.
    assert body["truncated"] is False


def test_the_public_conversation_window_is_capped_to_the_newest_turns(
    anonymous_client, monkeypatch
):
    """The dashboard branch's PUBLIC_ROW_CAP philosophy on transcripts: an
    anonymous GET serves a tail window, newest PUBLIC_ROW_CAP turns in
    ascending order, and says so via `truncated`."""
    owner = create_identity(name="Cap C", workspace_name="Cap share ws")
    client_a = _client_for(owner)
    conversation_id = _conversation(client_a, title="A very long thread")
    base = utcnow()
    db = SessionLocal()
    try:
        db.add_all(
            [
                Message(
                    workspace_id=owner.workspace_id,
                    conversation_id=conversation_id,
                    run_id=f"run-cap-{index}",
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"turn {index}",
                    created_by=owner.user_id,
                    created_at=base + timedelta(seconds=index),
                )
                for index in range(7)
            ]
        )
        db.commit()
    finally:
        db.close()
    created = share(client_a, "conversation", conversation_id)

    monkeypatch.setattr(share_links_api, "PUBLIC_ROW_CAP", 5)
    body = anonymous_client.get(f"/shared/{created['token']}").json()
    assert body["truncated"] is True
    # The newest five, oldest of them first — a window, not a scramble.
    assert [m["content"] for m in body["messages"]] == [
        f"turn {index}" for index in range(2, 7)
    ]


def test_a_subject_thread_cannot_be_publicly_shared(client):
    document = make_document(client)
    subject = client.post(
        f"/api/documents/{document['id']}/conversation", headers=key()
    )
    assert subject.status_code in (200, 201), subject.text
    response = client.post(
        "/api/share-links",
        headers=key(),
        json={"resource_kind": "conversation", "resource_id": subject.json()["id"]},
    )
    assert response.status_code == 409, response.text
    assert "subject" in response.json()["detail"]


def test_a_temporary_chat_cannot_be_publicly_shared(client):
    conversation_id = _conversation(client, title="Ephemeral", incognito=True)
    response = client.post(
        "/api/share-links",
        headers=key(),
        json={"resource_kind": "conversation", "resource_id": conversation_id},
    )
    assert response.status_code == 409, response.text
    assert "temporary" in response.json()["detail"]


def test_a_deleted_conversation_link_fails_closed(client, anonymous_client):
    conversation_id = _conversation(client, title="Doomed thread")
    created = share(client, "conversation", conversation_id)
    assert anonymous_client.get(f"/shared/{created['token']}").status_code == 200

    deleted = client.delete(
        f"/api/conversations/{conversation_id}", headers=key()
    )
    assert deleted.status_code in (200, 204), deleted.text

    after = anonymous_client.get(f"/shared/{created['token']}")
    assert after.status_code == 404
    unknown = anonymous_client.get("/shared/not-a-token-anyone-issued")
    assert after.json() == unknown.json()
