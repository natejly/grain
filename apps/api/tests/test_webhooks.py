"""Outbound webhooks: owner-configured URLs, signed deliveries, bounded retry.

What has to stay true for org egress to be safe to have:

- the destination is vetted twice — at create, while the owner still holds
  the form, and again at every send — with HTTPS-only + internal-network
  blocking; the `allowed_tool_hosts` allowlist is deliberately NOT applied
  (the owner configured the URL; the policy is documented in
  services/webhooks) but a URL that resolves into this deployment's network
  is refused in both places;
- every delivery is signed: X-Grain-Signature is HMAC-SHA256 of the exact
  body bytes under the endpoint's decrypted secret, verifiable by the
  receiver, and the secret itself is never echoed (`has_secret` only);
- emit fans out only to enabled endpoints subscribed to that event in that
  workspace — an event in A writes nothing for B;
- the tick's claim is a conditional UPDATE bumping `attempts`; a failure
  stays pending for a later tick until MAX_ATTEMPTS closes it as `failed`,
  and a sent row is never claimed again;
- payload bodies carry ids and titles only — pinned here at the emit
  chokepoint (a workflow completing), not re-derived.

Plus the schema promises every new table makes (three of them at once).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, List

import httpx
import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, create_identity
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, select

from app.config import get_settings
from app.database import SessionLocal, engine
from app.main import app
from app.models import ApiToken, WebhookDelivery, WebhookEndpoint
from app.services import webhooks as webhook_service
from app.services.crypto import decrypt_secret

API_ROOT = Path(__file__).resolve().parents[1]

TABLES = ("api_tokens", "webhook_endpoints", "webhook_deliveries")

PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True, scope="module")
def _fernet_key():
    """Webhook signing secrets are Fernet-encrypted; give the module a key."""
    settings = get_settings()
    original = settings.integrations_encryption_key
    settings.integrations_encryption_key = SecretStr(Fernet.generate_key().decode())
    yield
    settings.integrations_encryption_key = original


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def tenant() -> tuple[TestClient, Identity]:
    identity = create_identity(name="Hook owner", workspace_name="Hook workspace")
    client = authenticate(TestClient(app, base_url=TEST_BASE_URL), identity)
    return client, identity


@pytest.fixture
def public_dns(monkeypatch):
    """Every resolution answers a public address — hermetic and permissive."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443))
        ],
    )


def key() -> dict[str, str]:
    return {"Idempotency-Key": "hook-" + uuid.uuid4().hex}


def make_endpoint(
    client: TestClient,
    *,
    url: str = "https://hooks.example.com/sink",
    events: List[str] | None = None,
    secret: str = "shhh-sign-me",
) -> dict:
    response = client.post(
        "/api/webhooks",
        headers=key(),
        json={
            "name": "CI sink",
            "url": url,
            "events": events if events is not None else ["run.completed"],
            "secret": secret,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def capture_transport(status_code: int = 200):
    """A MockTransport that records every request it answers."""
    seen: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status_code, text="ok")

    return httpx.MockTransport(handler), seen


def load(db: Any, delivery_id: str) -> WebhookDelivery:
    row = db.scalar(
        select(WebhookDelivery).where(WebhookDelivery.id == delivery_id)
    )
    assert row is not None
    db.refresh(row)
    return row


# --------------------------------------------------------------------------
# Schema promises — three tables at once
# --------------------------------------------------------------------------


def test_every_webhook_and_token_table_is_workspace_scoped():
    for model in (ApiToken, WebhookEndpoint, WebhookDelivery):
        columns = model.__table__.columns
        assert "workspace_id" in columns, model.__tablename__
        assert not columns["workspace_id"].nullable, model.__tablename__


def test_the_migration_chain_builds_the_tables_the_orm_declares():
    """`alembic upgrade head` from empty must match `create_all` for all three
    new tables — production gets the alembic schema, development the metadata
    schema, and a difference only ever appears in production."""
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
        declared = inspect(engine)
        for table in TABLES:
            assert table in migrated.get_table_names()
            assert {
                column["name"] for column in migrated.get_columns(table)
            } == {column["name"] for column in declared.get_columns(table)}, table
            assert {
                index["name"] for index in migrated.get_indexes(table)
            } >= {index["name"] for index in declared.get_indexes(table)}, table


# --------------------------------------------------------------------------
# The management surface
# --------------------------------------------------------------------------


def test_create_vets_the_url_and_the_event_vocabulary(tenant, public_dns):
    client, _ = tenant
    plain_http = client.post(
        "/api/webhooks",
        headers=key(),
        json={"url": "http://hooks.example.com/sink", "events": []},
    )
    assert plain_http.status_code == 422, plain_http.text
    unknown_event = client.post(
        "/api/webhooks",
        headers=key(),
        json={"url": "https://hooks.example.com/sink", "events": ["run.deleted"]},
    )
    assert unknown_event.status_code == 422, unknown_event.text
    # The allowlist is NOT consulted for owner-configured endpoints — the
    # policy decision this module documents. hooks.example.com is nowhere near
    # TOOL_HOST_ALLOWLIST and must still be accepted.
    created = make_endpoint(client)
    assert created["url"] == "https://hooks.example.com/sink"
    assert created["enabled"] is True


def test_an_internal_destination_is_refused_at_create(tenant, monkeypatch):
    client, _ = tenant
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))
        ],
    )
    refused = client.post(
        "/api/webhooks",
        headers=key(),
        json={"url": "https://intranet.example.com/sink", "events": []},
    )
    assert refused.status_code == 422, refused.text
    assert "blocked" in refused.json()["detail"]


def test_the_secret_is_stored_encrypted_and_never_echoed(tenant, public_dns, db):
    client, _ = tenant
    created = make_endpoint(client, secret="raw-signing-secret")
    assert created["has_secret"] is True
    assert "raw-signing-secret" not in json.dumps(created)
    row = db.scalar(
        select(WebhookEndpoint).where(WebhookEndpoint.id == created["id"])
    )
    assert row is not None
    assert row.secret_encrypted != "raw-signing-secret"
    assert decrypt_secret(row.secret_encrypted) == "raw-signing-secret"
    listed = client.get("/api/webhooks")
    assert "raw-signing-secret" not in listed.text
    assert row.secret_encrypted not in listed.text


def test_webhook_management_is_an_owners_surface(tenant):
    client, identity = tenant
    from conftest import issue_session
    from test_api_tokens import make_member

    member_id = make_member(identity.workspace_id)
    token, csrf = issue_session(member_id)
    member_client = authenticate(
        TestClient(app, base_url=TEST_BASE_URL),
        Identity(
            user_id=member_id,
            workspace_id=identity.workspace_id,
            token=token,
            csrf_token=csrf,
        ),
    )
    assert member_client.get("/api/webhooks").status_code == 403
    assert (
        member_client.post(
            "/api/webhooks",
            headers=key(),
            json={"url": "https://hooks.example.com/sink", "events": []},
        ).status_code
        == 403
    )


def test_update_toggles_and_revalidates(tenant, public_dns, db):
    client, _ = tenant
    created = make_endpoint(client)
    toggled = client.put(
        f"/api/webhooks/{created['id']}",
        json={"enabled": False, "events": ["monitor.tripped"]},
    )
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["enabled"] is False
    assert toggled.json()["events"] == ["monitor.tripped"]
    bad_url = client.put(
        f"/api/webhooks/{created['id']}", json={"url": "http://downgrade.example"}
    )
    assert bad_url.status_code == 422, bad_url.text


# --------------------------------------------------------------------------
# emit: the fan-out
# --------------------------------------------------------------------------


def test_emit_reaches_only_subscribed_enabled_endpoints_in_the_workspace(
    tenant, public_dns, db
):
    client, identity = tenant
    subscribed = make_endpoint(client, events=["run.completed"])
    other_event = make_endpoint(client, events=["monitor.tripped"])
    disabled = make_endpoint(client, events=["run.completed"])
    client.put(f"/api/webhooks/{disabled['id']}", json={"enabled": False})

    foreign = create_identity(name="Bystander", workspace_name="Bystander workspace")
    foreign_client = authenticate(TestClient(app, base_url=TEST_BASE_URL), foreign)
    foreign_endpoint = make_endpoint(foreign_client, events=["run.completed"])

    delivery_ids = webhook_service.emit(
        db,
        workspace_id=identity.workspace_id,
        event="run.completed",
        payload={"run_id": "r-1"},
    )
    db.commit()
    assert len(delivery_ids) == 1
    delivery = load(db, delivery_ids[0])
    assert delivery.endpoint_id == subscribed["id"]
    assert delivery.status == "pending"
    for silent in (other_event["id"], disabled["id"], foreign_endpoint["id"]):
        assert (
            db.scalars(
                select(WebhookDelivery).where(
                    WebhookDelivery.endpoint_id == silent
                )
            ).all()
            == []
        ), silent


def test_emit_refuses_an_event_outside_the_vocabulary(tenant, db):
    _, identity = tenant
    assert (
        webhook_service.emit(
            db,
            workspace_id=identity.workspace_id,
            event="user.password_changed",
            payload={},
        )
        == []
    )


# --------------------------------------------------------------------------
# Delivery: signature, retry, the claim
# --------------------------------------------------------------------------


def emit_one(db: Any, identity: Identity, endpoint_id: str) -> str:
    ids = webhook_service.emit(
        db,
        workspace_id=identity.workspace_id,
        event="run.completed",
        payload={"run_id": "r-42", "conversation_id": "c-7", "status": "completed"},
    )
    db.commit()
    assert [d for d in ids if _endpoint_of(db, d) == endpoint_id], ids
    return next(d for d in ids if _endpoint_of(db, d) == endpoint_id)


def _endpoint_of(db: Any, delivery_id: str) -> str:
    return load(db, delivery_id).endpoint_id


def test_a_delivery_is_signed_and_the_signature_verifies(
    tenant, public_dns, db, monkeypatch
):
    client, identity = tenant
    created = make_endpoint(client, secret="verify-me")
    delivery_id = emit_one(db, identity, created["id"])

    transport, seen = capture_transport(200)
    monkeypatch.setattr(webhook_service, "HTTP_TRANSPORT", transport)
    claimed = webhook_service.claim_due(db, limit=500)
    assert delivery_id in claimed
    webhook_service.send_delivery(delivery_id)

    ours = [r for r in seen if r.url == "https://hooks.example.com/sink"]
    assert len(ours) == 1
    request = ours[0]
    body = request.read()
    expected = hmac.new(b"verify-me", body, hashlib.sha256).hexdigest()
    assert request.headers["X-Grain-Signature"] == expected
    parsed = json.loads(body)
    assert parsed["event"] == "run.completed"
    assert parsed["delivery_id"] == delivery_id
    # Ids and titles only — never message or tool content.
    assert parsed["payload"] == {
        "run_id": "r-42",
        "conversation_id": "c-7",
        "status": "completed",
    }

    row = load(db, delivery_id)
    assert row.status == "sent"
    assert row.sent_at is not None
    assert row.attempts == 1
    # A sent row is never claimed again.
    assert delivery_id not in webhook_service.claim_due(db, limit=500)


def test_a_failing_endpoint_retries_then_fails_for_good(
    tenant, public_dns, db, monkeypatch
):
    client, identity = tenant
    created = make_endpoint(client, secret="")
    delivery_id = emit_one(db, identity, created["id"])
    transport, seen = capture_transport(500)
    monkeypatch.setattr(webhook_service, "HTTP_TRANSPORT", transport)

    for attempt in (1, 2, 3):
        assert delivery_id in webhook_service.claim_due(db, limit=500)
        webhook_service.send_delivery(delivery_id)
        row = load(db, delivery_id)
        assert row.attempts == attempt
        assert "500" in row.last_error
    assert row.status == "failed"
    assert delivery_id not in webhook_service.claim_due(db, limit=500)
    # Exactly three requests ever left for it.
    assert len([r for r in seen if str(r.url).endswith("/sink")]) == 3


def test_an_internal_destination_is_refused_again_at_send(
    tenant, public_dns, db, monkeypatch
):
    """The DNS answer may have changed since create — the send re-vets."""
    client, identity = tenant
    created = make_endpoint(client, secret="")
    delivery_id = emit_one(db, identity, created["id"])
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.9", 443))
        ],
    )
    transport, seen = capture_transport(200)
    monkeypatch.setattr(webhook_service, "HTTP_TRANSPORT", transport)
    assert delivery_id in webhook_service.claim_due(db, limit=500)
    webhook_service.send_delivery(delivery_id)
    row = load(db, delivery_id)
    assert row.status == "pending"
    assert "blocked" in row.last_error
    assert seen == []


def test_a_delivery_for_a_deleted_endpoint_fails_closed(
    tenant, public_dns, db, monkeypatch
):
    client, identity = tenant
    created = make_endpoint(client, secret="")
    delivery_id = emit_one(db, identity, created["id"])
    assert client.delete(f"/api/webhooks/{created['id']}").status_code == 204
    transport, seen = capture_transport(200)
    monkeypatch.setattr(webhook_service, "HTTP_TRANSPORT", transport)
    assert delivery_id in webhook_service.claim_due(db, limit=500)
    webhook_service.send_delivery(delivery_id)
    row = load(db, delivery_id)
    assert row.status == "failed"
    assert "gone" in row.last_error
    assert seen == []


def test_exhausted_claims_are_closed_out_by_the_sweep(tenant, public_dns, db):
    """A process that died between claim and send three times must not leave
    the row pending-forever: the next sweep files it as failed."""
    client, identity = tenant
    created = make_endpoint(client, secret="")
    delivery_id = emit_one(db, identity, created["id"])
    for _ in range(3):
        assert delivery_id in webhook_service.claim_due(db, limit=500)
        # ...and the send never happens.
    assert delivery_id not in webhook_service.claim_due(db, limit=500)
    assert load(db, delivery_id).status == "failed"


# --------------------------------------------------------------------------
# The chokepoint, end to end
# --------------------------------------------------------------------------


def test_a_completed_workflow_run_queues_a_delivery(
    tenant, public_dns, db, monkeypatch
):
    """`workflow_run.completed` flows from the executor's own commit — no
    route in between — and the payload names ids and a status, nothing else."""
    from test_workflow_executor import Probe, begin, graph, install, tool_node

    from app.services.workflows import executor

    client, identity = tenant
    created = make_endpoint(client, events=["workflow_run.completed"], secret="")
    reader = Probe("probe_read", reply="the digest")
    install(monkeypatch, reader)
    workflow_run = begin(db, identity, graph([tool_node("read", "probe_read")]))
    executor.advance_run(db, workflow_run)
    assert workflow_run.status == "succeeded", workflow_run.error

    rows = db.scalars(
        select(WebhookDelivery).where(
            WebhookDelivery.endpoint_id == created["id"]
        )
    ).all()
    assert len(rows) == 1
    payload = json.loads(rows[0].payload_json)
    assert payload == {
        "workflow_run_id": workflow_run.id,
        "workflow_id": workflow_run.workflow_id,
        "status": "succeeded",
    }


def test_the_deliveries_panel_lists_recent_rows(tenant, public_dns, db):
    client, identity = tenant
    created = make_endpoint(client, secret="")
    delivery_id = emit_one(db, identity, created["id"])
    listed = client.get("/api/webhooks/deliveries")
    assert listed.status_code == 200, listed.text
    match = [row for row in listed.json() if row["id"] == delivery_id]
    assert len(match) == 1
    assert match[0]["status"] == "pending"
    assert match[0]["event"] == "run.completed"
    # No payload bodies on this surface — it answers "is my endpoint healthy".
    assert "payload" not in match[0]
