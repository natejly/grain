"""Metric monitors: a threshold question, asked unattended, that never lies twice.

A monitor rides the same tick as crons and workflows, so the claim properties
are mirrored from `test_cron_dispatch` — at-most-once per minute, disabled means
inert, no downtime replay. What is new is what a firing *is*: a dataset read
compared against a threshold, with three load-bearing rules of its own:

- **The edge, not the level.** An alert is written when the value crosses into
  tripped, not on every evaluation that finds it still there — a monitor that
  pages every minute while a number stays high teaches everyone to mute it. A
  recovery back under the line re-arms the edge.
- **The monitor's own workspace, always.** The dataset id is resolved under the
  monitor's workspace at evaluation time, so a foreign id — however it got into
  the row — is a skip, never a read. The routes 404 it at the boundary too.
- **A skip is not a state.** A broken query audits `monitor.skipped` and leaves
  `last_state` alone, so a transient failure can neither raise out of the shared
  ticker nor swallow the next genuine trip.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, List, Tuple

import pytest
from conftest import TEST_BASE_URL, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select

from app.config import get_settings
from app.database import SessionLocal, engine
from app.main import app
from app.models import AuditEvent, Membership, Monitor, Notification, User
from app.services import monitors as monitor_service

API_ROOT = Path(__file__).resolve().parents[1]

#: sum(amount) == 60 — the number every threshold below is aimed at.
CSV = "region,amount\nnorth,10\nsouth,20\nnorth,30\n"


def key() -> dict[str, str]:
    return {"Idempotency-Key": "monitor-" + uuid.uuid4().hex}


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def make_dataset(client: TestClient, content: str = CSV) -> dict:
    upload = client.post(
        "/api/sources",
        headers=key(),
        files={"file": ("figures.csv", content.encode(), "text/csv")},
    )
    assert upload.status_code == 202, upload.text
    response = client.post(
        "/api/datasets",
        headers=key(),
        json={
            "name": f"Figures {uuid.uuid4().hex[:8]}",
            "description": "Monitor fixture",
            "source_id": upload.json()["id"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def monitor_payload(dataset_id: str, **overrides: Any) -> dict:
    payload = {
        "name": "Amount watch",
        "dataset_id": dataset_id,
        "query": {
            "metrics": [{"field": "amount", "operation": "sum", "label": "total"}],
            "limit": 1,
        },
        "comparator": "gt",
        "threshold": 100.0,
        "schedule_cron": "* * * * *",
        "schedule_timezone": "UTC",
    }
    payload.update(overrides)
    return payload


def create_monitor(client: TestClient, dataset_id: str, **overrides: Any) -> dict:
    response = client.post(
        "/api/monitors", headers=key(), json=monitor_payload(dataset_id, **overrides)
    )
    assert response.status_code == 201, response.text
    return response.json()


def alerts_for(db: Any, monitor_id: str) -> List[Notification]:
    return list(
        db.scalars(
            select(Notification).where(
                Notification.monitor_id == monitor_id,
                Notification.kind == "monitor_alert",
            )
        )
    )


def _member(workspace_id: str, *, name: str) -> Tuple[TestClient, str]:
    """A fresh plain member of `workspace_id`, and a client signed in as them."""
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
    client = TestClient(app, base_url=TEST_BASE_URL)
    client.cookies.set(settings.session_cookie_name, token)
    client.headers[settings.csrf_header_name] = csrf_token
    return client, user_id


def _set_threshold(db: Any, monitor_id: str, threshold: float) -> None:
    """Move the line WITHOUT the API, because the PUT deliberately resets
    `last_state` — and half these tests exist to prove the stored edge state
    does its job when the definition has not changed."""
    monitor = db.scalar(select(Monitor).where(Monitor.id == monitor_id))
    monitor.threshold = threshold
    db.commit()


# --------------------------------------------------------------------------
# Schema: workspace-scoped, and the migration builds what the ORM declares
# --------------------------------------------------------------------------


def test_every_monitor_table_is_workspace_scoped() -> None:
    """Non-nullable workspace_id is what enrols the table in the tamper digest
    and makes every query below scopable at all."""
    columns = {column["name"]: column for column in inspect(engine).get_columns("monitors")}
    assert "workspace_id" in columns
    assert not columns["workspace_id"]["nullable"]


def test_the_migration_chain_builds_the_monitor_table_the_orm_declares() -> None:
    """`alembic upgrade head` from empty must match `create_all` — production
    gets the alembic schema, development the metadata one, and a difference is
    a bug that only ever appears in production."""
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
        assert "monitors" in migrated.get_table_names()
        # `notifications` rides along because the alert write is half the
        # feature: a monitors table without the store it alerts into would
        # trip into nothing.
        for table in ("monitors", "notifications"):
            assert {column["name"] for column in migrated.get_columns(table)} == {
                column["name"] for column in declared.get_columns(table)
            }, table
            assert {index["name"] for index in migrated.get_indexes(table)} >= {
                index["name"] for index in declared.get_indexes(table)
            }, table


# --------------------------------------------------------------------------
# CRUD, validated at the boundary
# --------------------------------------------------------------------------


def test_crud_round_trip_and_boundary_validation(
    identity_client: Callable[..., Any]
) -> None:
    """The ordinary lifecycle, plus every refusal the form should get while a
    person is still holding it — a bad cron, a metric-less query — and the one
    that must NOT be a validation message: a foreign dataset is a plain 404."""
    client = identity_client(name="Monitor owner", workspace_name="Monitor workspace")
    dataset = make_dataset(client)

    created = client.post(
        "/api/monitors", headers=key(), json=monitor_payload(dataset["id"])
    )
    assert created.status_code == 201, created.text
    body = created.json()
    monitor_id = body["id"]
    assert body["enabled"] is True
    assert body["last_state"] == ""
    assert body["last_dispatched_at"] is None
    assert body["query"]["metrics"][0]["label"] == "total"

    listed = client.get("/api/monitors")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [monitor_id]

    disabled = client.put(f"/api/monitors/{monitor_id}", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    # Refused at the boundary, not swallowed by a silent tick.
    assert (
        client.put(
            f"/api/monitors/{monitor_id}", json={"schedule_cron": "99 9 * * *"}
        ).status_code
        == 422
    )
    assert (
        client.put(
            f"/api/monitors/{monitor_id}", json={"query": {"metrics": []}}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/monitors",
            headers=key(),
            json=monitor_payload(dataset["id"], schedule_cron="not a cron"),
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/monitors",
            headers=key(),
            json=monitor_payload(dataset["id"], query={"metrics": []}),
        ).status_code
        == 422
    )

    # A foreign dataset is indistinguishable from a missing one — 404, before
    # any validation could say something more revealing.
    other = identity_client(name="Other", workspace_name="Other workspace")
    foreign_dataset = make_dataset(other)
    assert (
        client.post(
            "/api/monitors", headers=key(), json=monitor_payload(foreign_dataset["id"])
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"/api/monitors/{monitor_id}", json={"dataset_id": foreign_dataset["id"]}
        ).status_code
        == 404
    )

    assert client.delete(f"/api/monitors/{monitor_id}").status_code == 204
    assert (
        client.put(f"/api/monitors/{monitor_id}", json={"enabled": True}).status_code
        == 404
    )
    assert client.get("/api/monitors").json() == []


def test_create_replays_on_the_same_idempotency_key(
    identity_client: Callable[..., Any]
) -> None:
    client = identity_client(name="Replay owner", workspace_name="Replay workspace")
    dataset = make_dataset(client)
    shared = key()
    first = client.post(
        "/api/monitors", headers=shared, json=monitor_payload(dataset["id"])
    )
    again = client.post(
        "/api/monitors", headers=shared, json=monitor_payload(dataset["id"])
    )
    assert first.status_code == 201 and again.status_code == 201
    assert first.json()["id"] == again.json()["id"]
    assert len(client.get("/api/monitors").json()) == 1


# --------------------------------------------------------------------------
# The claim, and the edge
# --------------------------------------------------------------------------


def test_a_due_monitor_is_evaluated_once_per_minute_however_often_the_tick_lands(
    db: Any, identity_client: Callable[..., Any]
) -> None:
    """The at-most-once guarantee, and the trip it protects: three ticks in one
    minute produce one evaluation and one alert, not three of each."""
    client = identity_client(name="Sweep owner", workspace_name="Sweep workspace")
    dataset = make_dataset(client)
    monitor_id = create_monitor(client, dataset["id"], threshold=50.0)["id"]
    moment = at("2026-08-11T09:00")

    first = monitor_service.dispatch_due(db, moment=moment)
    again = monitor_service.dispatch_due(db, moment=moment)
    third = monitor_service.dispatch_due(db, moment=moment + timedelta(seconds=30))

    assert monitor_id in first
    assert monitor_id not in again and monitor_id not in third

    monitor = db.scalar(select(Monitor).where(Monitor.id == monitor_id))
    assert monitor.last_dispatched_at == moment
    assert monitor.last_state == "tripped"
    assert json.loads(monitor.last_value_json) == 60
    assert len(alerts_for(db, monitor_id)) == 1
    alert = alerts_for(db, monitor_id)[0]
    assert alert.target_user_id == "", "an alert is for every member"
    assert alert.status == "open"
    assert "60" in alert.title and "50" in alert.title

    # The next minute evaluates again — but the value is still over the line,
    # so the edge rule writes nothing new.
    later = monitor_service.dispatch_due(db, moment=moment + timedelta(minutes=1))
    assert monitor_id in later
    assert len(alerts_for(db, monitor_id)) == 1


def test_the_edge_realerts_only_after_a_recovery(
    db: Any, identity_client: Callable[..., Any]
) -> None:
    """ok → tripped alerts; tripped → tripped is silence; and a recovery back to
    ok re-arms the edge so the next crossing alerts again."""
    client = identity_client(name="Edge owner", workspace_name="Edge workspace")
    dataset = make_dataset(client)
    monitor_id = create_monitor(client, dataset["id"], threshold=1000.0)["id"]

    # Under the line: ok, and nothing to say.
    ran = client.post(f"/api/monitors/{monitor_id}/run-now")
    assert ran.status_code == 200, ran.text
    assert ran.json()["state"] == "ok"
    assert alerts_for(db, monitor_id) == []

    # The value crosses (the line moves under it — same edge): one alert.
    _set_threshold(db, monitor_id, 50.0)
    assert client.post(f"/api/monitors/{monitor_id}/run-now").json()["state"] == "tripped"
    assert len(alerts_for(db, monitor_id)) == 1

    # Still over the line: evaluated, not re-announced.
    assert client.post(f"/api/monitors/{monitor_id}/run-now").json()["state"] == "tripped"
    assert len(alerts_for(db, monitor_id)) == 1

    # Recovery re-arms the edge...
    _set_threshold(db, monitor_id, 1000.0)
    assert client.post(f"/api/monitors/{monitor_id}/run-now").json()["state"] == "ok"
    assert len(alerts_for(db, monitor_id)) == 1

    # ...and the room acknowledges the first alert — one OPEN row per monitor
    # is the contract, so an unacked alert deliberately suppresses a re-alert.
    (alert,) = alerts_for(db, monitor_id)
    assert client.post(f"/api/notifications/{alert.id}/resolve").status_code == 200

    # ...so the next crossing is news again.
    _set_threshold(db, monitor_id, 50.0)
    assert client.post(f"/api/monitors/{monitor_id}/run-now").json()["state"] == "tripped"
    assert len(alerts_for(db, monitor_id)) == 2


def test_one_open_alert_per_monitor_even_when_evaluations_race(
    db: Any, identity_client: Callable[..., Any]
) -> None:
    """The notify is deduped against the OPEN alert, not just the stored edge
    state. Two halves: a recover-and-recross while the first alert sits unacked
    writes no second row (the room is already alerted), and a racing evaluator
    holding a stale `last_state` — run-now is claim-free, so it can see the
    crossing concurrently with the tick — is caught by the same read."""
    client = identity_client(name="Dedup owner", workspace_name="Dedup workspace")
    dataset = make_dataset(client)
    monitor_id = create_monitor(client, dataset["id"], threshold=50.0)["id"]

    assert client.post(f"/api/monitors/{monitor_id}/run-now").json()["state"] == "tripped"
    assert len(alerts_for(db, monitor_id)) == 1

    # Recover, then re-cross with the first alert still open: edge re-armed,
    # but no duplicate open row for the same monitor.
    _set_threshold(db, monitor_id, 1000.0)
    assert client.post(f"/api/monitors/{monitor_id}/run-now").json()["state"] == "ok"
    _set_threshold(db, monitor_id, 50.0)
    assert client.post(f"/api/monitors/{monitor_id}/run-now").json()["state"] == "tripped"
    alerts = alerts_for(db, monitor_id)
    assert len(alerts) == 1 and alerts[0].status == "open"

    # The race shape: an evaluator that read `last_state` before the alert
    # landed would pass the edge check — the open-alert read still stops it.
    monitor = db.scalar(select(Monitor).where(Monitor.id == monitor_id))
    monitor.last_state = ""
    db.commit()
    assert monitor_service.evaluate(db, monitor).state == "tripped"
    assert len(alerts_for(db, monitor_id)) == 1


def test_the_database_itself_refuses_a_second_open_alert_for_one_monitor(
    db: Any, identity_client: Callable[..., Any]
) -> None:
    """`_open_alert_exists` is a check-then-insert; the partial unique index
    (0064) is what actually closes the race. A second OPEN monitor_alert row
    for the same monitor is refused by the database — while other monitors,
    resolved rows, and every ''-monitor_id notification kind are untouched."""
    from sqlalchemy.exc import IntegrityError

    from app.services.notifications import notify, resolve

    client = identity_client(name="Index owner", workspace_name="Index workspace")
    boot = client.get("/api/bootstrap").json()
    workspace_id = boot["identity"]["workspace_id"]

    first = notify(
        db,
        workspace_id=workspace_id,
        kind="monitor_alert",
        title="first crossing",
        monitor_id="idx-monitor-a",
    )
    db.commit()
    with pytest.raises(IntegrityError):
        notify(
            db,
            workspace_id=workspace_id,
            kind="monitor_alert",
            title="racing duplicate",
            monitor_id="idx-monitor-a",
        )
    db.rollback()

    # The index is exactly as narrow as the contract: a different monitor's
    # open alert, and any number of ''-monitor_id rows (mentions and their
    # kin), coexist freely.
    notify(
        db,
        workspace_id=workspace_id,
        kind="monitor_alert",
        title="another monitor",
        monitor_id="idx-monitor-b",
    )
    notify(db, workspace_id=workspace_id, kind="mention", title="hey")
    notify(db, workspace_id=workspace_id, kind="mention", title="hey again")
    db.commit()

    # Resolving the open row vacates the slot: the next genuine crossing may
    # alert again.
    first = db.scalar(select(Notification).where(Notification.id == first.id))
    resolve(db, notification=first, resolved_by=boot["identity"]["user_id"])
    db.commit()
    notify(
        db,
        workspace_id=workspace_id,
        kind="monitor_alert",
        title="re-crossed after ack",
        monitor_id="idx-monitor-a",
    )
    db.commit()


def test_a_nan_metric_is_a_skip_not_an_alert_or_an_invalid_json_write(
    db: Any, identity_client: Callable[..., Any], monkeypatch: Any
) -> None:
    """NaN is a float, so it passes the numeric check; every comparator answers
    False, and `json.dumps` would write the invalid-JSON literal `NaN` into
    `last_value_json`. It must be a skip — reasoned, audited, edge untouched."""
    client = identity_client(name="NaN owner", workspace_name="NaN workspace")
    dataset = make_dataset(client)
    monitor_id = create_monitor(
        client, dataset["id"], comparator="lt", threshold=100.0
    )["id"]

    class _Result:
        rows = [{"total": float("nan")}]

    monkeypatch.setattr(
        monitor_service, "execute_dataset_query", lambda *args, **kwargs: _Result()
    )
    monitor = db.scalar(select(Monitor).where(Monitor.id == monitor_id))
    outcome = monitor_service.evaluate(db, monitor)
    assert outcome.state == "skipped"
    assert "finite" in outcome.reason
    assert alerts_for(db, monitor_id) == []
    db.refresh(monitor)
    assert monitor.last_state == "" and monitor.last_value_json == ""


def test_a_disabled_monitor_never_evaluates(
    db: Any, identity_client: Callable[..., Any]
) -> None:
    """The enabled flag is a gate, not a hint — and re-arming it makes the very
    next tick fire, exactly as a cron behaves."""
    client = identity_client(name="Gate owner", workspace_name="Gate workspace")
    dataset = make_dataset(client)
    monitor_id = create_monitor(client, dataset["id"], threshold=50.0)["id"]
    assert client.put(f"/api/monitors/{monitor_id}", json={"enabled": False}).status_code == 200

    moment = at("2026-08-11T10:00")
    assert monitor_id not in monitor_service.dispatch_due(db, moment=moment)
    assert alerts_for(db, monitor_id) == []

    assert client.put(f"/api/monitors/{monitor_id}", json={"enabled": True}).status_code == 200
    assert monitor_id in monitor_service.dispatch_due(db, moment=moment)


# --------------------------------------------------------------------------
# The failure posture: skip, audit, never raise, never read across
# --------------------------------------------------------------------------


def test_a_foreign_dataset_is_never_read_by_the_sweep(
    db: Any, identity_client: Callable[..., Any]
) -> None:
    """However a foreign dataset id got into the row — the routes 404 it, but
    rows outlive code — the evaluation resolves it under the MONITOR'S OWN
    workspace, finds nothing, and skips. No value is read, no alert is written,
    and the stored edge state stays untouched."""
    victim = identity_client(name="Victim", workspace_name="Victim workspace")
    foreign_dataset = make_dataset(victim)
    attacker = identity_client(name="Attacker", workspace_name="Attacker workspace")

    session = SessionLocal()
    try:
        monitor = Monitor(
            workspace_id=attacker.identity.workspace_id,
            created_by=attacker.identity.user_id,
            name="Cross-tenant watch",
            dataset_id=foreign_dataset["id"],
            query_json=json.dumps(
                {"metrics": [{"field": "amount", "operation": "sum", "label": "total"}]}
            ),
            comparator="gt",
            threshold=0.0,
            schedule_cron="* * * * *",
            schedule_timezone="UTC",
        )
        session.add(monitor)
        session.commit()
        monitor_id = monitor.id
    finally:
        session.close()

    monitor = db.scalar(select(Monitor).where(Monitor.id == monitor_id))
    outcome = monitor_service.evaluate(db, monitor)
    assert outcome.state == "skipped"
    assert outcome.reason, "a skip must say why"
    assert alerts_for(db, monitor_id) == []
    db.refresh(monitor)
    assert monitor.last_state == "" and monitor.last_value_json == ""
    skipped = db.scalar(
        select(AuditEvent).where(
            AuditEvent.workspace_id == attacker.identity.workspace_id,
            AuditEvent.action == "monitor.skipped",
            AuditEvent.resource_id == monitor_id,
        )
    )
    assert skipped is not None, "the skip must be visible in the audit log"


def test_a_bad_query_is_skipped_and_logged_not_raised(
    db: Any, identity_client: Callable[..., Any]
) -> None:
    """A query the schema no longer satisfies (here: a metric over a column that
    does not exist) answers run-now with an honest 'skipped' + reason, audits
    `monitor.skipped`, and would never raise out of the shared tick."""
    client = identity_client(name="Bad query owner", workspace_name="Bad query workspace")
    dataset = make_dataset(client)
    monitor_id = create_monitor(
        client,
        dataset["id"],
        query={"metrics": [{"field": "no_such_column", "operation": "sum", "label": "x"}]},
    )["id"]

    ran = client.post(f"/api/monitors/{monitor_id}/run-now")
    assert ran.status_code == 200, ran.text
    assert ran.json()["state"] == "skipped"
    assert ran.json()["reason"]
    assert alerts_for(db, monitor_id) == []

    # And the sweep path swallows it the same way rather than dying mid-tick.
    monitor = db.scalar(select(Monitor).where(Monitor.id == monitor_id))
    assert monitor_service.evaluate(db, monitor).state == "skipped"


# --------------------------------------------------------------------------
# The alert in the Inbox: member-visible, resolvable, then gone
# --------------------------------------------------------------------------


def test_a_trip_lands_in_every_members_inbox_and_resolve_clears_it(
    db: Any, identity_client: Callable[..., Any]
) -> None:
    """An alert is automation: '' -targeted, so the owner and a plain member see
    the SAME row, and either resolving it clears it for the whole room."""
    owner = identity_client(name="Alert owner", workspace_name="Alert workspace")
    member, _member_id = _member(owner.identity.workspace_id, name="Colleague")
    dataset = make_dataset(owner)
    monitor_id = create_monitor(
        owner, dataset["id"], name="Room-wide watch", threshold=10.0
    )["id"]

    assert owner.post(f"/api/monitors/{monitor_id}/run-now").json()["state"] == "tripped"
    (alert,) = alerts_for(db, monitor_id)

    for viewer in (owner, member):
        feed = viewer.get("/api/inbox")
        assert feed.status_code == 200, feed.text
        rows = feed.json()["alerts"]
        assert [row["id"] for row in rows] == [alert.id]
        assert rows[0]["monitor_id"] == monitor_id
        assert "Room-wide watch" in rows[0]["title"]

    # The plain member may resolve a room-targeted row (target '' admits any
    # member), and the feed simply stops listing it for everyone.
    resolved = member.post(f"/api/notifications/{alert.id}/resolve")
    assert resolved.status_code == 200, resolved.text
    for viewer in (owner, member):
        assert viewer.get("/api/inbox").json()["alerts"] == []


def test_deleting_a_monitor_resolves_its_open_alerts(
    db: Any, identity_client: Callable[..., Any]
) -> None:
    """An open alert outliving its monitor would badge every Inbox forever,
    deep-linking a Monitors row that no longer exists — the delete resolves it
    in the same transaction."""
    client = identity_client(name="Sweep-away owner", workspace_name="Sweep-away workspace")
    dataset = make_dataset(client)
    monitor_id = create_monitor(client, dataset["id"], threshold=10.0)["id"]

    assert client.post(f"/api/monitors/{monitor_id}/run-now").json()["state"] == "tripped"
    (alert,) = alerts_for(db, monitor_id)
    assert alert.status == "open"
    assert [row["id"] for row in client.get("/api/inbox").json()["alerts"]] == [alert.id]

    assert client.delete(f"/api/monitors/{monitor_id}").status_code == 204
    db.expire_all()
    (alert,) = alerts_for(db, monitor_id)
    assert alert.status == "resolved"
    assert client.get("/api/inbox").json()["alerts"] == []
