"""Notification digests: one opt-in daily mail per member, at most once.

The properties worth proving are the ones that make unattended mail safe:

- the digest is OFF by default and a member switches it on for themselves —
  `PUT /api/me/digest` edits the caller's own membership and validates the
  hour at the door;
- the per-member claim is at-most-once per day however many ticks land, and a
  ticker that was down at the member's hour still sends *today's* mail later
  (the period-start comparison, not exact-minute matching);
- the content is `services/inbox_feed.waiting_for` — the same queries the
  Inbox answers with — so one member's digest can never carry another
  member's personal-thread approvals (THE case, per the inbox map);
- an empty queue is a claimed silence, not a retry and not a mail;
- everything interpolated into the HTML arrives escaped.

Cross-tenant DENY for the PUT lives in the isolation suite (SCOPED); the
`read_inbox` refactor onto `inbox_feed` is proven by test_inbox.py staying
green.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from test_agent_approvals import _park_run

from app.database import SessionLocal, engine
from app.main import app
from app.models import (
    AuditEvent,
    Membership,
    Notification,
    SweepClaim,
    ToolPolicy,
    User,
)
from app.services import digests
from app.services.auth import email as email_service

API_ROOT = Path(__file__).resolve().parents[1]

#: Marker planted in every notification this module creates, so cleanup
#: deletes exactly its own rows in the shared per-process database.
MARKER = "digest-test"


@pytest.fixture(autouse=True)
def _clean_digest_state():
    """Reset everything a digest test touches in the shared database.

    The sweep's hourly gate is one global `sweep_claims` row; a previous
    test's claim at a later date would silence every later test's dispatch.
    Membership digest columns and `_park_run`'s ToolPolicy row get the same
    hygiene, and planted notifications leave with the test that made them.
    """
    yield
    db = SessionLocal()
    try:
        db.query(SweepClaim).filter(SweepClaim.name == digests.CLAIM_NAME).delete()
        db.query(ToolPolicy).delete()
        db.query(Notification).filter(Notification.body.contains(MARKER)).delete()
        for membership in db.scalars(select(Membership)):
            membership.digest_enabled = False
            membership.digest_hour_utc = 9
            membership.digest_last_sent_at = None
        db.commit()
    finally:
        db.close()


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def sent_emails(monkeypatch):
    """Capture outbound mail instead of printing it — the house fixture."""
    captured: list[email_service.OutboundEmail] = []

    class Capturing:
        def send(self, message: email_service.OutboundEmail) -> None:
            captured.append(message)

    monkeypatch.setattr(email_service, "get_email_sender", lambda settings: Capturing())
    return captured


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def identity_of(client) -> dict:
    return client.get("/api/bootstrap").json()["identity"]


def membership_of(db: Any, workspace_id: str, user_id: str) -> Membership:
    row = db.scalar(
        select(Membership).where(
            Membership.workspace_id == workspace_id,
            Membership.user_id == user_id,
        )
    )
    assert row is not None
    return row


def enable(client, *, hour: int = 9) -> None:
    response = client.put("/api/me/digest", json={"enabled": True, "hour_utc": hour})
    assert response.status_code == 200, response.text


def plant_mention(db: Any, *, workspace_id: str, user_id: str, title: str) -> str:
    row = Notification(
        workspace_id=workspace_id,
        target_user_id=user_id,
        kind="mention",
        status="open",
        title=title,
        body=f"{MARKER} {uuid.uuid4().hex[:8]}",
    )
    db.add(row)
    db.commit()
    return row.id


def make_member(workspace_id: str) -> Identity:
    """A second, non-owner member of an existing workspace — the roommate."""
    db = SessionLocal()
    try:
        user = User(email=f"digest-{uuid.uuid4().hex[:10]}@example.com", name="Member")
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


def audits(db: Any, action: str, resource_id: str) -> list[AuditEvent]:
    return list(
        db.scalars(
            select(AuditEvent).where(
                AuditEvent.action == action,
                AuditEvent.resource_id == resource_id,
            )
        )
    )


# --------------------------------------------------------------------------
# Schema promises
# --------------------------------------------------------------------------


def test_the_migration_chain_builds_the_digest_columns_the_orm_declares():
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
        migrated_columns = {
            column["name"] for column in migrated.get_columns("memberships")
        }
        assert {"digest_enabled", "digest_hour_utc", "digest_last_sent_at"} <= (
            migrated_columns
        )
        declared = inspect(engine)
        assert migrated_columns == {
            column["name"] for column in declared.get_columns("memberships")
        }
        assert {index["name"] for index in migrated.get_indexes("memberships")} >= {
            index["name"] for index in declared.get_indexes("memberships")
        }


# --------------------------------------------------------------------------
# The preference
# --------------------------------------------------------------------------


def test_the_digest_is_off_by_default_and_bootstrap_says_so(identity_client, db):
    client = identity_client()
    boot = client.get("/api/bootstrap").json()
    assert boot["digest"] == {"enabled": False, "hour_utc": 9}
    identity = boot["identity"]
    membership = membership_of(db, identity["workspace_id"], identity["user_id"])
    assert membership.digest_enabled is False
    assert membership.digest_hour_utc == 9
    assert membership.digest_last_sent_at is None


def test_a_member_sets_their_own_digest_preference(identity_client, db):
    client = identity_client()
    identity = identity_of(client)
    response = client.put("/api/me/digest", json={"enabled": True, "hour_utc": 7})
    assert response.status_code == 200, response.text
    assert response.json() == {"enabled": True, "hour_utc": 7}
    assert client.get("/api/bootstrap").json()["digest"] == {
        "enabled": True,
        "hour_utc": 7,
    }
    membership = membership_of(db, identity["workspace_id"], identity["user_id"])
    assert len(audits(db, "digest.updated", membership.id)) == 1


def test_an_hour_off_the_clock_is_refused_at_the_door(identity_client):
    client = identity_client()
    for hour in (-1, 24):
        refused = client.put("/api/me/digest", json={"enabled": True, "hour_utc": hour})
        assert refused.status_code == 422, refused.text


# --------------------------------------------------------------------------
# The claim
# --------------------------------------------------------------------------


def test_one_send_per_member_per_day_however_many_ticks_land(identity_client, db):
    """A tick 37 minutes after the hour still claims today (the ticker was
    down at 9:00); a second tick the same day claims nothing; the next day
    claims again."""
    client = identity_client()
    identity = identity_of(client)
    enable(client)
    membership = membership_of(db, identity["workspace_id"], identity["user_id"])

    late = digests.dispatch_due(db, moment=at("2026-09-01T09:37"))
    assert membership.id in late

    same_day = digests.dispatch_due(db, moment=at("2026-09-01T15:00"))
    assert membership.id not in same_day

    next_day = digests.dispatch_due(db, moment=at("2026-09-02T09:00"))
    assert membership.id in next_day


def test_the_hour_gate_holds_the_mail_until_the_members_hour(identity_client, db):
    client = identity_client()
    identity = identity_of(client)
    enable(client, hour=12)
    membership = membership_of(db, identity["workspace_id"], identity["user_id"])

    early = digests.dispatch_due(db, moment=at("2026-09-03T09:00"))
    assert membership.id not in early

    due = digests.dispatch_due(db, moment=at("2026-09-03T12:01"))
    assert membership.id in due


def test_a_member_who_never_opted_in_is_never_claimed(identity_client, db):
    """Off by default means off: waiting items or not, no claim, no mail."""
    client = identity_client()
    identity = identity_of(client)
    membership = membership_of(db, identity["workspace_id"], identity["user_id"])
    plant_mention(
        db,
        workspace_id=identity["workspace_id"],
        user_id=identity["user_id"],
        title="Something waiting",
    )
    claimed = digests.dispatch_due(db, moment=at("2026-09-04T09:00"))
    assert membership.id not in claimed


def test_an_empty_digest_sends_no_mail_but_the_claim_stands(
    identity_client, db, sent_emails
):
    """Nothing waiting is today's answer, not a reason to ask again hourly."""
    client = identity_client()
    identity = identity_of(client)
    enable(client)
    membership = membership_of(db, identity["workspace_id"], identity["user_id"])

    claimed = digests.dispatch_due(db, moment=at("2026-09-05T09:10"))
    assert membership.id in claimed
    # The background entrypoint the tick enqueues — own session, real render.
    digests.send_digest(membership.id)
    assert sent_emails == []
    assert audits(db, "digest.sent", membership.id) == []

    again = digests.dispatch_due(db, moment=at("2026-09-05T18:00"))
    assert membership.id not in again


# --------------------------------------------------------------------------
# The mail
# --------------------------------------------------------------------------


def test_the_mail_lists_the_waiting_items_with_markup_escaped(
    client, db, sent_emails
):
    """The digest carries the member's real waiting set — a parked approval
    and a mention — with every interpolated value escaped, a complete
    plain-text alternative, and a `digest.sent` audit."""
    _park_run(client)
    identity = identity_of(client)
    plant_mention(
        db,
        workspace_id=identity["workspace_id"],
        user_id=identity["user_id"],
        title="<b>Ping</b> from a teammate",
    )
    enable(client)
    membership = membership_of(db, identity["workspace_id"], identity["user_id"])
    db.expire_all()

    delivered = digests.deliver(db, membership)
    assert delivered is True
    assert len(sent_emails) == 1
    message = sent_emails[0]
    # The shared dev workspace may hold other tests' waiting rows, so the
    # subject pins the shape, not the number.
    assert re.fullmatch(r"\d+ items? waiting in Grain", message.subject)
    assert "list_datasets" in message.html
    assert "&lt;b&gt;Ping&lt;/b&gt;" in message.html
    assert "<b>Ping</b>" not in message.html
    # The text alternative is the same queue, not a stub with a login link.
    assert "list_datasets" in message.body
    assert "<b>Ping</b> from a teammate" in message.body
    assert "Open your Inbox" in message.html
    # Titles-only, on purpose (QA F13 #8): the notification BODY quotes
    # comment/message content and must stay in-app behind the deep link —
    # the marker planted in the body may never reach either mail body.
    assert MARKER not in message.html
    assert MARKER not in message.body
    assert len(audits(db, "digest.sent", membership.id)) == 1


def test_a_digest_never_carries_another_members_personal_approvals(
    client, db, sent_emails
):
    """THE case: the owner's personal-thread approval is the owner's business.

    The roommate's digest is built through the same `run_activity_predicate`
    the Inbox uses, so a mail to them lists their own mention and nothing of
    the owner's personal thread — and the owner's own digest does carry it."""
    _park_run(client)
    identity = identity_of(client)
    roommate = make_member(identity["workspace_id"])
    roommate_client = authenticate(
        TestClient(app, base_url=TEST_BASE_URL), roommate
    )
    plant_mention(
        db,
        workspace_id=identity["workspace_id"],
        user_id=roommate.user_id,
        title="Roommate ping",
    )
    enable(roommate_client)
    membership = membership_of(db, identity["workspace_id"], roommate.user_id)
    db.expire_all()

    delivered = digests.deliver(db, membership)
    assert delivered is True
    assert len(sent_emails) == 1
    message = sent_emails[0]
    assert "Roommate ping" in message.html
    # The owner's parked personal-thread approval: absent from both bodies.
    assert "list_datasets" not in message.html
    assert "list_datasets" not in message.body
    assert "Approvals" not in message.html

    # The owner's own digest does carry it — the predicate, not an accident.
    enable(client)
    owner_membership = membership_of(db, identity["workspace_id"], identity["user_id"])
    db.expire_all()
    assert digests.deliver(db, owner_membership) is True
    assert "list_datasets" in sent_emails[1].html


def test_a_membership_gone_between_claim_and_send_is_a_quiet_skip(sent_emails):
    digests.send_digest("membership-that-never-existed")
    assert sent_emails == []


def test_a_deactivated_user_receives_no_digest(identity_client, db, sent_emails):
    """QA F13 #7: deactivation keeps the membership row, but workspace mail
    stops with the account — a deactivated-but-still-membered user's deliver
    is a quiet skip, never a mail to the address the user row still holds."""
    client = identity_client()
    identity = identity_of(client)
    enable(client)
    plant_mention(
        db,
        workspace_id=identity["workspace_id"],
        user_id=identity["user_id"],
        title="Still waiting",
    )
    membership = membership_of(db, identity["workspace_id"], identity["user_id"])
    user = db.scalar(select(User).where(User.id == identity["user_id"]))
    assert user is not None
    user.status = "disabled"
    db.commit()
    db.expire_all()

    assert digests.deliver(db, membership) is False
    assert sent_emails == []
    assert audits(db, "digest.sent", membership.id) == []


def test_a_failed_send_is_not_audited_as_a_delivery(identity_client, db, monkeypatch):
    """`send_quietly` swallows SMTP failures by design; the audit must not
    then claim a delivery that never happened — the subscription mailer's
    honesty branch, mirrored. The per-member claim stands: best effort, no
    same-day retry."""
    client = identity_client()
    identity = identity_of(client)
    enable(client)
    plant_mention(
        db,
        workspace_id=identity["workspace_id"],
        user_id=identity["user_id"],
        title="Never delivered",
    )
    membership = membership_of(db, identity["workspace_id"], identity["user_id"])
    db.expire_all()

    class Exploding:
        def send(self, message: email_service.OutboundEmail) -> None:
            raise RuntimeError("mail host down")

    monkeypatch.setattr(
        email_service, "get_email_sender", lambda settings: Exploding()
    )
    assert digests.deliver(db, membership) is False
    assert audits(db, "digest.sent", membership.id) == []


def test_a_mail_that_cannot_be_built_leaves_a_skip_not_silence(
    identity_client, db, sent_emails, monkeypatch
):
    """A render that raises must audit, because `send_digest`'s blanket
    `except` would otherwise turn a permanently broken mailer into a log line
    on a host nobody reads — a member with a full waiting set, mailed nothing,
    every day, with no evidence anywhere the product can show. `WEB_ORIGIN` is
    guarded at boot now, so this exercises the general case: whatever makes the
    build raise, the outcome is a `digest.skipped` row and no mail."""
    client = identity_client()
    identity = identity_of(client)
    enable(client)
    plant_mention(
        db,
        workspace_id=identity["workspace_id"],
        user_id=identity["user_id"],
        title="Unrenderable",
    )
    membership = membership_of(db, identity["workspace_id"], identity["user_id"])
    db.expire_all()

    def explode(label: str, url: str) -> str:
        raise ValueError("link button URL must be http(s), got scheme ''")

    monkeypatch.setattr(digests.mail_render, "render_link_button", explode)

    # The background entrypoint the tick enqueues — its own session, and the
    # `except` that used to swallow this whole failure.
    digests.send_digest(membership.id)

    assert sent_emails == []
    assert audits(db, "digest.sent", membership.id) == []
    skips = audits(db, "digest.skipped", membership.id)
    assert len(skips) == 1
    assert "render failed" in skips[0].detail_json
