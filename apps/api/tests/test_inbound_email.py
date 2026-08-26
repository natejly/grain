"""Inbound email → thread: the provider door, the addresses, the quiet 200s.

What has to stay true for a public mail door to be safe to have:

- the door is the tick's posture: 503 with no configured secret, 401 for the
  wrong one, and the secret authenticates the *provider* while the hashed
  routing token in the recipient decides (and authorises) the workspace;
- an unknown or revoked token is a quiet ``200 {accepted: false}`` that
  writes nothing — proven with a workspace digest, the invite-accept pattern
  — because a live probe must learn nothing about which addresses exist;
- a delivery is a personal thread plus one user message with ``run_id=""``
  and NO agent turn: external text never makes the model act;
- the provider's message id is the idempotency key, scoped per address —
  redelivery answers the original thread and posts nothing twice, while the
  same mail to a sibling address still lands as its own thread;
- each address caps its landings with a rolling leaky bucket, NOT a fixed
  UTC day (which could be spent twice across midnight) — mail beyond the cap
  is the same quiet 200, landing nothing, audited once at the trip;
- the raw address appears exactly once, at mint time; the table holds only a
  sha256, and no later response echoes a token.

Plus the schema promises every new table makes.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, create_identity
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, func, inspect, select

from app.config import get_settings
from app.database import SessionLocal, engine
from app.main import app
from app.models import AuditEvent, Conversation, InboundAddress, Message, Space
from app.services import inbound_email as address_service

API_ROOT = Path(__file__).resolve().parents[1]

SECRET = "provider-hook-secret"


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def tenant() -> tuple[TestClient, Identity]:
    identity = create_identity(name="Mail owner", workspace_name="Mail workspace")
    client = authenticate(TestClient(app, base_url=TEST_BASE_URL), identity)
    return client, identity


@pytest.fixture
def door(monkeypatch) -> TestClient:
    """An anonymous client, with the provider secret configured."""
    monkeypatch.setattr(
        get_settings(),
        "inbound_email_webhook_secret",
        SecretStr(SECRET),
        raising=False,
    )
    return TestClient(app, base_url=TEST_BASE_URL)


def key() -> dict[str, str]:
    return {"Idempotency-Key": "mail-" + uuid.uuid4().hex}


def bearer(secret: str = SECRET) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def mint(client: TestClient, label: str = "Support inbox", **extra: str) -> dict:
    response = client.post(
        "/api/inbound-addresses",
        headers=key(),
        json={"label": label, **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def delivery(recipient: str, **overrides: str) -> dict[str, str]:
    payload = {
        "recipient": recipient,
        "sender": "reporter@example.com",
        "subject": "Broken export",
        "text": "The CSV export has been empty since Tuesday.",
        "message_id": f"<{uuid.uuid4().hex}@mail.example.com>",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# Schema promises
# --------------------------------------------------------------------------


def test_the_inbound_addresses_table_is_workspace_scoped():
    columns = InboundAddress.__table__.columns
    assert "workspace_id" in columns
    assert not columns["workspace_id"].nullable


def test_the_migration_chain_builds_the_table_the_orm_declares():
    """`alembic upgrade head` from empty must match `create_all` — production
    gets the alembic schema, development the metadata schema, and a difference
    only ever appears in production."""
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
        assert "inbound_addresses" in migrated.get_table_names()
        assert {
            column["name"] for column in migrated.get_columns("inbound_addresses")
        } == {column["name"] for column in declared.get_columns("inbound_addresses")}
        assert {
            index["name"] for index in migrated.get_indexes("inbound_addresses")
        } >= {index["name"] for index in declared.get_indexes("inbound_addresses")}


# --------------------------------------------------------------------------
# The door's lock
# --------------------------------------------------------------------------


def test_the_door_answers_503_while_no_secret_is_configured():
    """Unset means closed, not open — the tick's exact posture."""
    anonymous = TestClient(app, base_url=TEST_BASE_URL)
    response = anonymous.post(
        "/api/hooks/email/inbound",
        json=delivery("inbox+whatever@mail.grain.test"),
    )
    assert response.status_code == 503, response.text


def test_a_wrong_secret_is_a_uniform_401(door):
    response = door.post(
        "/api/hooks/email/inbound",
        headers=bearer("not-the-secret"),
        json=delivery("inbox+whatever@mail.grain.test"),
    )
    assert response.status_code == 401, response.text


def test_an_unknown_token_is_quietly_refused_and_writes_nothing(door, tenant):
    """accepted:false with a 200 — and not one byte lands anywhere.

    The digest is the whole workspace, so this also pins that a probe naming
    a syntactically plausible token cannot land rows in ANY workspace it
    guesses at — the invite-accept pattern.
    """
    from test_tenant_isolation import workspace_digest

    _, identity = tenant
    before = workspace_digest(identity.workspace_id)
    response = door.post(
        "/api/hooks/email/inbound",
        headers=bearer(),
        json=delivery("inbox+this-token-was-never-minted-0001@mail.grain.test"),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "accepted": False,
        "conversation_id": "",
        "message_id": "",
    }
    assert workspace_digest(identity.workspace_id) == before


def test_a_revoked_address_is_exactly_as_inert_as_an_unknown_one(door, tenant, db):
    from test_tenant_isolation import workspace_digest

    client, identity = tenant
    minted = mint(client, label="Soon revoked")
    revoked = client.post(f"/api/inbound-addresses/{minted['id']}/revoke")
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_at"] is not None

    before = workspace_digest(identity.workspace_id)
    response = door.post(
        "/api/hooks/email/inbound",
        headers=bearer(),
        json=delivery(minted["address"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] is False
    assert workspace_digest(identity.workspace_id) == before


# --------------------------------------------------------------------------
# A delivery that lands
# --------------------------------------------------------------------------


def test_a_valid_delivery_creates_a_personal_thread_in_the_right_workspace(
    door, tenant, db
):
    client, identity = tenant
    minted = mint(client)
    response = door.post(
        "/api/hooks/email/inbound",
        headers=bearer(),
        json=delivery(minted["address"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted"] is True

    conversation = db.get(Conversation, body["conversation_id"])
    assert conversation is not None
    assert conversation.workspace_id == identity.workspace_id
    assert conversation.created_by == identity.user_id
    assert conversation.shared is False
    assert conversation.title == "Email: Broken export"

    message = db.get(Message, body["message_id"])
    assert message is not None
    assert message.conversation_id == conversation.id
    assert message.role == "user"
    assert message.run_id == "", "no agent turn may ride an inbound email"
    assert "From: reporter@example.com" in message.content
    assert "Subject: Broken export" in message.content
    assert "empty since Tuesday" in message.content

    audit = db.scalar(
        select(AuditEvent).where(
            AuditEvent.workspace_id == identity.workspace_id,
            AuditEvent.action == "email.received",
            AuditEvent.resource_id == conversation.id,
        )
    )
    assert audit is not None


def test_the_thread_files_under_the_address_target_space(door, tenant, db):
    client, identity = tenant
    space = Space(
        workspace_id=identity.workspace_id,
        name="Support",
        instructions="",
        created_by=identity.user_id,
    )
    db.add(space)
    db.commit()
    minted = mint(client, label="Spaced", target_space_id=space.id)
    response = door.post(
        "/api/hooks/email/inbound",
        headers=bearer(),
        json=delivery(minted["address"]),
    )
    assert response.status_code == 200, response.text
    conversation = db.get(Conversation, response.json()["conversation_id"])
    assert conversation is not None and conversation.space_id == space.id


def test_html_only_mail_lands_as_stripped_text(door, tenant, db):
    client, _ = tenant
    minted = mint(client, label="HTML mail")
    response = door.post(
        "/api/hooks/email/inbound",
        headers=bearer(),
        json=delivery(
            minted["address"],
            text="",
            html="<div><b>Bold</b> claim &amp; a <a href='https://x.example'>link</a></div>",
        ),
    )
    assert response.status_code == 200, response.text
    message = db.get(Message, response.json()["message_id"])
    assert message is not None
    assert "Bold claim & a link" in message.content
    assert "<" not in message.content.split("\n\n", 1)[1]


def test_a_redelivered_message_id_answers_the_original_and_posts_nothing_twice(
    door, tenant, db
):
    client, _ = tenant
    minted = mint(client, label="Dedup")
    payload = delivery(minted["address"])
    first = door.post(
        "/api/hooks/email/inbound", headers=bearer(), json=payload
    )
    assert first.status_code == 200 and first.json()["accepted"] is True
    second = door.post(
        "/api/hooks/email/inbound", headers=bearer(), json=payload
    )
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    count = db.scalar(
        select(func.count(Message.id)).where(
            Message.conversation_id == first.json()["conversation_id"]
        )
    )
    assert count == 1


def test_the_same_message_id_lands_once_per_address_not_once_per_workspace(
    door, tenant, db
):
    """Dedup is scoped to the address: one mail sent to two of the
    workspace's addresses is two threads, and a sender cannot pre-burn a
    message id through one address to suppress later mail through another."""
    client, _ = tenant
    first_address = mint(client, label="First")
    second_address = mint(client, label="Second")
    shared_id = f"<{uuid.uuid4().hex}@mail.example.com>"

    first = door.post(
        "/api/hooks/email/inbound",
        headers=bearer(),
        json=delivery(first_address["address"], message_id=shared_id),
    )
    second = door.post(
        "/api/hooks/email/inbound",
        headers=bearer(),
        json=delivery(second_address["address"], message_id=shared_id),
    )
    assert first.json()["accepted"] and second.json()["accepted"]
    assert (
        first.json()["conversation_id"] != second.json()["conversation_id"]
    ), "the second address's mail must land as its own thread"


def test_mail_beyond_the_daily_cap_is_a_quiet_200_that_lands_nothing(
    door, tenant, db, monkeypatch
):
    client, identity = tenant
    monkeypatch.setattr(address_service, "DAILY_CAP", 2)
    minted = mint(client, label="Flooded")
    for _ in range(2):
        landed = door.post(
            "/api/hooks/email/inbound",
            headers=bearer(),
            json=delivery(minted["address"]),
        )
        assert landed.status_code == 200 and landed.json()["accepted"] is True

    over = door.post(
        "/api/hooks/email/inbound",
        headers=bearer(),
        json=delivery(minted["address"]),
    )
    assert over.status_code == 200, over.text
    assert over.json() == {
        "accepted": False,
        "conversation_id": "",
        "message_id": "",
    }
    again = door.post(
        "/api/hooks/email/inbound",
        headers=bearer(),
        json=delivery(minted["address"]),
    )
    assert again.json()["accepted"] is False

    threads = db.scalar(
        select(func.count(Conversation.id)).where(
            Conversation.workspace_id == identity.workspace_id,
            Conversation.title == "Email: Broken export",
        )
    )
    assert threads == 2, "nothing beyond the cap may land"
    # Audited exactly once, at the trip — a day-long flood cannot flood the
    # audit trail too.
    capped = db.scalars(
        select(AuditEvent).where(
            AuditEvent.workspace_id == identity.workspace_id,
            AuditEvent.action == "email.capped",
            AuditEvent.resource_id == minted["id"],
        )
    ).all()
    assert len(capped) == 1


def test_the_cap_cannot_be_spent_twice_across_midnight(tenant, db, monkeypatch):
    """The finding the cap was reshaped for.

    A fixed UTC-day counter let a flood spend the whole cap at 23:59 and the
    whole cap again at 00:01 — 2x the cap from one address inside two
    minutes. The bucket rolls, so crossing midnight buys nothing; only time
    does, and it pays out one landing per drain interval.
    """
    client, _ = tenant
    monkeypatch.setattr(address_service, "DAILY_CAP", 3)
    minted = mint(client, label="Boundary")
    address = db.scalar(
        select(InboundAddress).where(InboundAddress.id == minted["id"])
    )
    drain = timedelta(seconds=address_service.WINDOW_SECONDS / 3)

    eve = datetime(2026, 8, 25, 23, 59, 0)
    for _ in range(3):
        assert address_service.count_delivery(address, now=eve).allowed
    tripped = address_service.count_delivery(address, now=eve)
    assert tripped == address_service.CapVerdict(allowed=False, tripped=True)

    past_midnight = datetime(2026, 8, 26, 0, 1, 0)
    verdict = address_service.count_delivery(address, now=past_midnight)
    assert verdict.allowed is False, "a fresh UTC day must not refill the bucket"
    assert verdict.tripped is False, "one audit per episode, not one per day"

    # Time, and only time, reopens it — and because every refused attempt
    # restarts the drain clock, a sender who keeps probing never drains.
    assert not address_service.count_delivery(address, now=past_midnight + drain).allowed
    last_probe = past_midnight + drain * 2
    assert not address_service.count_delivery(address, now=last_probe).allowed
    # Quiet for two drain intervals after that last probe, and it reopens.
    landed = address_service.count_delivery(address, now=last_probe + drain * 2)
    assert landed.allowed, "the bucket drains back one credit at a time"


# --------------------------------------------------------------------------
# The management surface
# --------------------------------------------------------------------------


def test_the_mint_reveals_the_address_once_and_stores_only_a_hash(tenant, db):
    client, identity = tenant
    minted = mint(client, label="Reveal once")
    domain = get_settings().inbound_email_domain
    assert minted["address"].startswith("inbox+")
    assert minted["address"].endswith(f"@{domain}")
    token = address_service.token_from_recipient(minted["address"])
    assert token, "the minted address must carry a parseable routing token"

    row = db.scalar(
        select(InboundAddress).where(InboundAddress.id == minted["id"])
    )
    assert row is not None
    assert row.workspace_id == identity.workspace_id
    assert token not in row.token_hash
    assert row.token_hash == address_service.hash_token(token)

    listed = client.get("/api/inbound-addresses")
    assert listed.status_code == 200, listed.text
    assert token not in listed.text, "no later response may echo the token"
    assert any(item["id"] == minted["id"] for item in listed.json())


def test_an_idempotent_replay_cannot_repeat_the_address(tenant):
    client, _ = tenant
    headers = key()
    first = client.post(
        "/api/inbound-addresses", headers=headers, json={"label": "Replayed"}
    )
    assert first.status_code == 201, first.text
    again = client.post(
        "/api/inbound-addresses", headers=headers, json={"label": "Replayed"}
    )
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["address"] == ""


def test_minting_against_a_foreign_space_is_a_plain_404(tenant, db):
    client, _ = tenant
    other = create_identity(name="Other", workspace_name="Other workspace")
    space = Space(
        workspace_id=other.workspace_id,
        name="Not yours",
        instructions="",
        created_by=other.user_id,
    )
    db.add(space)
    db.commit()
    response = client.post(
        "/api/inbound-addresses",
        headers=key(),
        json={"label": "Cross probe", "target_space_id": space.id},
    )
    assert response.status_code == 404, response.text


def test_minting_is_refused_while_no_domain_is_configured(tenant, monkeypatch):
    client, _ = tenant
    monkeypatch.setattr(
        get_settings(), "inbound_email_domain", "", raising=False
    )
    response = client.post(
        "/api/inbound-addresses", headers=key(), json={"label": "Inert"}
    )
    assert response.status_code == 503, response.text
