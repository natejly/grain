"""Dashboard subscriptions: scheduled snapshot mail, delivered at most once.

The properties worth proving are the ones that make unattended email safe to
have at all:

- the claim is at-most-once per subscription per scheduled minute, however
  often the tick lands — and unlike a cron's one-minute window, a tick that
  arrives 37 minutes late still delivers *today's* mail (the daily-job claim),
  while a second tick the same day delivers nothing;
- the mail carries LIVE data rendered through `mail_render` — dataset values
  appear, markup in a cell arrives escaped, and the plain-text body is
  complete, so a text-only client is not sent a stub;
- who may be mailed is bounded by membership: a recipient outside the
  workspace 404s at create (indistinguishable from a user that does not
  exist), a member subscribing anyone but themselves needs the owner role, and
  a membership deleted after the fact turns the fire into a skip, never a mail
  to someone who left;
- and every way a fire can deliver nothing — purged dashboard, departed
  member — is a `dashboard.subscription_skipped` audit, because unattended
  mail that silently stops is a support ticket with no evidence.

Cross-tenant DENY lives in the isolation suite; here is the happy path it is
the negative of, plus the schema promises every new table makes.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, create_identity, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select

from app.database import SessionLocal, engine
from app.main import app
from app.models import (
    AuditEvent,
    Dashboard,
    DashboardSubscription,
    Membership,
    User,
)
from app.services import dashboard_subscriptions as subscription_service
from app.services.auth import email as email_service

API_ROOT = Path(__file__).resolve().parents[1]

#: sum(revenue): North 40, South 20 — the numbers the mail must carry.
CSV = "territory,revenue\nNorth,10\nSouth,20\nNorth,30\n"
#: A territory whose name is markup: it must arrive in the mail as text.
CSV_MARKUP = 'territory,revenue\n"<b>North</b>",10\n"<b>North</b>",30\n'


def key() -> dict[str, str]:
    return {"Idempotency-Key": "sub-" + uuid.uuid4().hex}


def unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


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


def make_dashboard(client, content: str = CSV) -> dict:
    upload = client.post(
        "/api/sources",
        headers=key(),
        files={"file": ("deals.csv", content.encode(), "text/csv")},
    )
    assert upload.status_code == 202, upload.text
    dataset = client.post(
        "/api/datasets",
        headers=key(),
        json={
            "name": unique("Deals"),
            "description": "Subscription fixture",
            "source_id": upload.json()["id"],
        },
    )
    assert dataset.status_code == 201, dataset.text
    dashboard = client.post(
        "/api/dashboards",
        headers=key(),
        json={
            "name": unique("Revenue"),
            "description": "",
            "dataset_id": dataset.json()["id"],
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
    assert dashboard.status_code == 201, dashboard.text
    return dashboard.json()


def subscribe(client, dashboard_id: str, *, cron: str = "0 9 * * *", **extra) -> dict:
    response = client.post(
        "/api/dashboard-subscriptions",
        headers=key(),
        json={
            "dashboard_id": dashboard_id,
            "schedule_cron": cron,
            "schedule_timezone": "UTC",
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def load(db: Any, subscription_id: str) -> DashboardSubscription:
    row = db.scalar(
        select(DashboardSubscription).where(
            DashboardSubscription.id == subscription_id
        )
    )
    assert row is not None
    return row


def make_member(workspace_id: str) -> Identity:
    """A second, non-owner member of an existing workspace — the roommate."""
    db = SessionLocal()
    try:
        user = User(email=f"member-{uuid.uuid4().hex[:10]}@example.com", name="Member")
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


def test_the_dashboard_subscriptions_table_is_workspace_scoped():
    """No subscription exists outside a workspace — the column the isolation
    sweep and the tamper digest both hang off."""
    columns = DashboardSubscription.__table__.columns
    assert "workspace_id" in columns
    assert not columns["workspace_id"].nullable


def test_the_migration_chain_builds_the_subscriptions_table_the_orm_declares():
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
        assert "dashboard_subscriptions" in migrated.get_table_names()
        declared = inspect(engine)
        assert {
            column["name"] for column in migrated.get_columns("dashboard_subscriptions")
        } == {
            column["name"] for column in declared.get_columns("dashboard_subscriptions")
        }
        assert {
            index["name"] for index in migrated.get_indexes("dashboard_subscriptions")
        } >= {
            index["name"] for index in declared.get_indexes("dashboard_subscriptions")
        }


# --------------------------------------------------------------------------
# The claim
# --------------------------------------------------------------------------


def test_a_late_tick_still_delivers_today_and_a_second_tick_delivers_nothing(
    client, db
):
    """The daily-job claim: 9:00 mail at 9:37 (the ticker was down at 9:00) is
    delivery; a second dispatch the same day is silence; the next day fires
    again. `dispatch_due` is global, so membership of the returned list is
    checked for OUR id only — other tests' subscriptions may ride along."""
    dashboard = make_dashboard(client)
    created = subscribe(client, dashboard["id"])

    late = subscription_service.dispatch_due(db, moment=at("2026-08-10T09:37"))
    assert created["id"] in late
    # The claim records the *scheduled* minute, not the tick's arrival — the
    # boundary the next day's "already fired?" comparison hangs off.
    db.expire_all()
    assert load(db, created["id"]).last_dispatched_at == at("2026-08-10T09:00")

    same_day = subscription_service.dispatch_due(db, moment=at("2026-08-10T15:00"))
    assert created["id"] not in same_day

    next_day = subscription_service.dispatch_due(db, moment=at("2026-08-11T09:00"))
    assert created["id"] in next_day


def test_a_disabled_subscription_never_fires(client, db):
    dashboard = make_dashboard(client)
    created = subscribe(client, dashboard["id"])
    row = load(db, created["id"])
    row.enabled = False
    db.commit()
    claimed = subscription_service.dispatch_due(db, moment=at("2026-08-12T09:00"))
    assert created["id"] not in claimed


# --------------------------------------------------------------------------
# The mail
# --------------------------------------------------------------------------


def test_the_mail_carries_live_values_in_html_and_a_complete_text_body(
    client, db, sent_emails
):
    dashboard = make_dashboard(client)
    created = subscribe(client, dashboard["id"])
    claimed = subscription_service.dispatch_due(db, moment=at("2026-08-13T09:02"))
    assert created["id"] in claimed
    # The background entrypoint the tick enqueues — own session, real render.
    subscription_service.send_subscription(created["id"])

    assert len(sent_emails) == 1
    message = sent_emails[0]
    assert dashboard["name"] in message.subject
    # Live values from the dataset: sum(revenue) is North 40 / South 20.
    assert "North" in message.html and "40" in message.html
    assert "South" in message.html and "20" in message.html
    # The text alternative is the same numbers, not a stub with a login link.
    assert "North" in message.body and "40" in message.body
    assert "Open in Grain" in message.html

    sent = audits(db, "dashboard.subscription_sent", created["id"])
    assert len(sent) == 1


def test_markup_in_a_dataset_cell_arrives_escaped(client, db, sent_emails):
    dashboard = make_dashboard(client, content=CSV_MARKUP)
    created = subscribe(client, dashboard["id"])
    delivered = subscription_service.deliver(db, load(db, created["id"]))
    assert delivered is True
    assert len(sent_emails) == 1
    html = sent_emails[0].html
    assert "&lt;b&gt;North&lt;/b&gt;" in html
    assert "<b>North</b>" not in html


def test_a_purged_dashboard_makes_the_fire_a_skip_with_an_audit(
    client, db, sent_emails
):
    dashboard = make_dashboard(client)
    created = subscribe(client, dashboard["id"])
    removed = client.delete(f"/api/dashboards/{dashboard['id']}")
    assert removed.status_code in (200, 204), removed.text

    assert subscription_service.deliver(db, load(db, created["id"])) is False
    assert sent_emails == []
    skips = audits(db, "dashboard.subscription_skipped", created["id"])
    assert len(skips) == 1
    assert "dashboard gone" in skips[0].detail_json


def test_a_failed_send_is_audited_as_a_skip_not_a_delivery(
    client, db, monkeypatch
):
    """`send_quietly` swallows SMTP failures by design; the audit must not
    then claim a delivery that never happened. A refused mail is a skip."""
    dashboard = make_dashboard(client)
    created = subscribe(client, dashboard["id"])

    class Exploding:
        def send(self, message: email_service.OutboundEmail) -> None:
            raise RuntimeError("mail host down")

    monkeypatch.setattr(
        email_service, "get_email_sender", lambda settings: Exploding()
    )
    assert subscription_service.deliver(db, load(db, created["id"])) is False
    assert audits(db, "dashboard.subscription_sent", created["id"]) == []
    skips = audits(db, "dashboard.subscription_skipped", created["id"])
    assert len(skips) == 1
    assert "delivery failed" in skips[0].detail_json


def test_a_newline_bearing_dashboard_name_cannot_break_the_subject(
    client, db, sent_emails
):
    """A CR/LF in the name would make MIME header assembly raise on every
    fire — a permanent failure — and is header-injection shaped besides. The
    subject collapses it to one line; the body keeps the raw name."""
    dashboard = make_dashboard(client)
    created = subscribe(client, dashboard["id"])
    row = db.scalar(select(Dashboard).where(Dashboard.id == dashboard["id"]))
    assert row is not None
    row.name = "Revenue\r\nBcc: attacker@example.com"
    db.commit()

    assert subscription_service.deliver(db, load(db, created["id"])) is True
    assert len(sent_emails) == 1
    subject = sent_emails[0].subject
    assert "\r" not in subject and "\n" not in subject
    assert subject == "Dashboard: Revenue Bcc: attacker@example.com"


def test_a_departed_member_stops_receiving_mail(client, db, sent_emails):
    """The membership row is the standing permission to receive workspace data;
    deleting it must silence the subscription, not orphan a mail to whatever
    address the user row still holds."""
    dashboard = make_dashboard(client)
    member = make_member(created_workspace_id(client))
    created = subscribe(client, dashboard["id"], recipient_user_id=member.user_id)

    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == member.workspace_id,
            Membership.user_id == member.user_id,
        )
    )
    assert membership is not None
    db.delete(membership)
    db.commit()

    assert subscription_service.deliver(db, load(db, created["id"])) is False
    assert sent_emails == []
    skips = audits(db, "dashboard.subscription_skipped", created["id"])
    assert len(skips) == 1
    assert "no longer a member" in skips[0].detail_json


def test_removing_a_member_disables_their_subscriptions(client, db):
    """`remove_member`'s release sweep, extended: the send-time membership
    check already skips a departed recipient, but a subscription left enabled
    would audit a skip forever — and silently resume mailing if the person
    were ever re-invited. Removal turns it off durably, in the same
    transaction; getting mail again takes an explicit re-enable."""
    dashboard = make_dashboard(client)
    member = make_member(created_workspace_id(client))
    created = subscribe(client, dashboard["id"], recipient_user_id=member.user_id)

    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == member.workspace_id,
            Membership.user_id == member.user_id,
        )
    )
    assert membership is not None
    membership_id = membership.id
    removed = client.delete(f"/api/admin/members/{membership_id}")
    assert removed.status_code == 204, removed.text

    db.expire_all()
    assert load(db, created["id"]).enabled is False
    events = audits(db, "membership.removed", membership_id)
    assert len(events) == 1
    assert json.loads(events[0].detail_json)["subscriptions_disabled"] == 1

    # Re-inviting the person does NOT resume the mail: the disabled row stays
    # disabled, so the ticker never claims it.
    db.add(
        Membership(
            workspace_id=member.workspace_id, user_id=member.user_id, role="member"
        )
    )
    db.commit()
    claimed = subscription_service.dispatch_due(db, moment=at("2026-08-14T09:00"))
    assert created["id"] not in claimed


def created_workspace_id(client) -> str:
    me = client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    return me.json()["workspace_id"]


# --------------------------------------------------------------------------
# Who may be mailed
# --------------------------------------------------------------------------


def test_a_recipient_outside_the_workspace_is_indistinguishable_from_nobody(client):
    dashboard = make_dashboard(client)
    outsider = create_identity(name="Outsider", workspace_name="Elsewhere")
    refused = client.post(
        "/api/dashboard-subscriptions",
        headers=key(),
        json={
            "dashboard_id": dashboard["id"],
            "schedule_cron": "0 9 * * *",
            "schedule_timezone": "UTC",
            "recipient_user_id": outsider.user_id,
        },
    )
    assert refused.status_code == 404, refused.text


def test_subscribing_another_member_is_an_owners_move(client):
    """A member signs themselves up freely; routing a colleague's attention —
    recurring mail they did not ask for — needs the owner role."""
    dashboard = make_dashboard(client)
    workspace_id = created_workspace_id(client)
    member = make_member(workspace_id)
    member_client = authenticate(TestClient(app, base_url=TEST_BASE_URL), member)
    # Themselves: fine.
    own = subscribe(member_client, dashboard["id"])
    assert own["recipient_user_id"] == member.user_id
    # Somebody else: refused, and the somebody demonstrably exists.
    refused = member_client.post(
        "/api/dashboard-subscriptions",
        headers=key(),
        json={
            "dashboard_id": dashboard["id"],
            "schedule_cron": "0 9 * * *",
            "schedule_timezone": "UTC",
            "recipient_user_id": owner_id_of(client),
        },
    )
    assert refused.status_code == 403, refused.text
    # The owner doing the same thing for the member: fine.
    routed = subscribe(client, dashboard["id"], recipient_user_id=member.user_id)
    assert routed["recipient_user_id"] == member.user_id


def owner_id_of(client) -> str:
    me = client.get("/api/auth/me")
    assert me.status_code == 200, me.text
    return me.json()["user_id"]


def test_a_member_sees_and_deletes_only_their_own_subscriptions(client):
    """Visibility and delete share one predicate: a row a member cannot list
    is a row they cannot delete — 404, confirming nothing."""
    dashboard = make_dashboard(client)
    owners = subscribe(client, dashboard["id"])
    member = make_member(created_workspace_id(client))
    member_client = authenticate(TestClient(app, base_url=TEST_BASE_URL), member)
    mine = subscribe(member_client, dashboard["id"])

    listed = member_client.get("/api/dashboard-subscriptions")
    assert listed.status_code == 200
    listed_ids = {row["id"] for row in listed.json()}
    assert mine["id"] in listed_ids
    assert owners["id"] not in listed_ids
    # The rows a member can see name their dashboard — the list is the UI's
    # whole answer to "what mail is standing?".
    named = next(row for row in listed.json() if row["id"] == mine["id"])
    assert named["dashboard_name"] == dashboard["name"]

    refused = member_client.delete(f"/api/dashboard-subscriptions/{owners['id']}")
    assert refused.status_code == 404, refused.text

    removed = member_client.delete(f"/api/dashboard-subscriptions/{mine['id']}")
    assert removed.status_code == 204, removed.text

    # The owner sees everything, including what the member left behind.
    owner_listed = client.get("/api/dashboard-subscriptions")
    owner_ids = {row["id"] for row in owner_listed.json()}
    assert owners["id"] in owner_ids
    assert mine["id"] not in owner_ids  # deleted above


# --------------------------------------------------------------------------
# The boundary
# --------------------------------------------------------------------------


def test_a_bad_schedule_or_dashboard_is_refused_at_the_form(client):
    dashboard = make_dashboard(client)
    bad_cron = client.post(
        "/api/dashboard-subscriptions",
        headers=key(),
        json={
            "dashboard_id": dashboard["id"],
            "schedule_cron": "not a cron",
            "schedule_timezone": "UTC",
        },
    )
    assert bad_cron.status_code == 422, bad_cron.text
    bad_zone = client.post(
        "/api/dashboard-subscriptions",
        headers=key(),
        json={
            "dashboard_id": dashboard["id"],
            "schedule_cron": "0 9 * * *",
            "schedule_timezone": "Mars/Olympus",
        },
    )
    assert bad_zone.status_code == 422, bad_zone.text
    missing = client.post(
        "/api/dashboard-subscriptions",
        headers=key(),
        json={
            "dashboard_id": uuid.uuid4().hex[:32],
            "schedule_cron": "0 9 * * *",
            "schedule_timezone": "UTC",
        },
    )
    assert missing.status_code == 404, missing.text


def test_the_create_replays_idempotently(client):
    dashboard = make_dashboard(client)
    idempotency = key()
    first = client.post(
        "/api/dashboard-subscriptions",
        headers=idempotency,
        json={
            "dashboard_id": dashboard["id"],
            "schedule_cron": "0 9 * * *",
            "schedule_timezone": "UTC",
        },
    )
    assert first.status_code == 201, first.text
    again = client.post(
        "/api/dashboard-subscriptions",
        headers=idempotency,
        json={
            "dashboard_id": dashboard["id"],
            "schedule_cron": "0 9 * * *",
            "schedule_timezone": "UTC",
        },
    )
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
