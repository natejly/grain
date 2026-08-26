"""Per-agent cost attribution, and the spend watch that reads it.

Three layers, tested in order. The *column*: `model_usage.agent_id` is bound
where the run is bound, so a real agent turn's ledger rows name the agent
without any call site remembering to say so. The *panel*: `GET /api/admin/usage`
breaks spend down by agent under the same honesty contract as every other
axis — unpriced calls are counted, never summed as zero, and a deleted agent's
spend keeps its id. The *watch*: the hourly tick sweep that compares each
agent's trailing day against its own prior week and writes one open
`spend_anomaly` notification — at most one per hour (the `sweep_claims`
conditional-UPDATE claim), at most one open per agent (resolve re-arms), and
nothing at all without three days of history to deviate from.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from sqlalchemy import create_engine, inspect, select

from app.clock import utcnow
from app.database import SessionLocal, engine
from app.models import Agent, AuditEvent, ModelUsage, Notification, Run, SweepClaim
from app.services import spend_watch
from app.services import usage as usage_service
from app.services.agent_loop import run_agent_turn

API_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _rearm_the_watch():
    """The claim row and the open anomalies are module-global state in a
    shared, session-scoped database: a claim won by one test would silently
    swallow the next test's sweep, and an open anomaly would dedupe it away.
    Cleared *before* each test so every sweep here starts from silence."""
    session = SessionLocal()
    try:
        session.query(SweepClaim).delete()
        session.query(Notification).filter(
            Notification.kind == "spend_anomaly"
        ).delete()
        session.commit()
    finally:
        session.close()
    yield


def _boot(client) -> tuple[str, str]:
    body = client.get("/api/bootstrap").json()
    return body["identity"]["workspace_id"], body["default_agent_id"]


def _usage_row(
    session: Any,
    *,
    workspace_id: str,
    agent_id: str,
    at: datetime,
    tokens: int = 1_000,
    cost: Optional[float] = None,
) -> None:
    session.add(
        ModelUsage(
            workspace_id=workspace_id,
            agent_id=agent_id,
            operation="chat",
            model="watch-model",
            input_tokens=tokens,
            total_tokens=tokens,
            cost_usd=cost,
            created_at=at,
        )
    )


def _seed_baseline(
    session: Any,
    *,
    workspace_id: str,
    agent_id: str,
    base: datetime,
    days: int,
    tokens: int = 1_000,
    cost: Optional[float] = None,
) -> None:
    """One row per prior day, an hour inside that day's 24h window."""
    for day in range(1, days + 1):
        _usage_row(
            session,
            workspace_id=workspace_id,
            agent_id=agent_id,
            at=base - timedelta(hours=24 * day + 1),
            tokens=tokens,
            cost=cost,
        )


def _anomalies_for(session: Any, workspace_id: str) -> list[Notification]:
    return list(
        session.scalars(
            select(Notification).where(
                Notification.workspace_id == workspace_id,
                Notification.kind == "spend_anomaly",
            )
        )
    )


# --------------------------------------------------------------------------
# Schema: the column and the claim table survive the migration chain
# --------------------------------------------------------------------------


def test_the_ledger_column_and_claim_table_are_declared() -> None:
    """`agent_id` follows the ledger's reference rules (plain string, never
    null) and `sweep_claims` is deliberately the workspace-less exception:
    ticker infrastructure holding a name and a timestamp, nothing owned."""
    usage_columns = {c.name: c for c in ModelUsage.__table__.columns}
    assert "agent_id" in usage_columns
    assert not usage_columns["agent_id"].nullable
    claim_columns = {c.name for c in SweepClaim.__table__.columns}
    assert claim_columns == {"name", "last_dispatched_at"}


def test_the_migration_chain_builds_the_usage_agent_schema() -> None:
    """`alembic upgrade head` from empty must match `create_all` — the parity
    test every schema change gets, per test_workflow_schema.py."""
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
        assert "sweep_claims" in migrated.get_table_names()
        for table in ("model_usage", "sweep_claims"):
            assert {c["name"] for c in migrated.get_columns(table)} == {
                c["name"] for c in declared.get_columns(table)
            }, table
            assert {i["name"] for i in migrated.get_indexes(table)} >= {
                i["name"] for i in declared.get_indexes(table)
            }, table
        assert "agent_id" in {c["name"] for c in migrated.get_columns("model_usage")}


# --------------------------------------------------------------------------
# Attribution: a real turn's ledger rows name the run's agent
# --------------------------------------------------------------------------


def test_a_turns_ledger_rows_carry_the_runs_agent(client):
    """Driven through `run_agent_turn` — the frame that binds the scope — with
    a model step that bills one call, exactly as the streaming chokepoint
    would. Nothing between the binder and the recorder names the agent; the
    row naming it proves the ambient scope carried it the whole way down."""
    workspace_id, agent_id = _boot(client)
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "usage-conv-" + os.urandom(6).hex()},
        json={"title": "Attribution"},
    ).json()

    def model_step(input_items, tools, instructions):
        usage_service.record_model_usage(
            model="attribution-probe",
            operation=usage_service.CHAT,
            usage=SimpleNamespace(input_tokens=100, output_tokens=20, total_tokens=120),
        )
        return [("completed", SimpleNamespace(output=[], output_text="Done."))]

    session = SessionLocal()
    try:
        run = Run(
            workspace_id=workspace_id,
            conversation_id=conversation["id"],
            agent_id=agent_id,
            created_by=client.get("/api/bootstrap").json()["identity"]["user_id"],
            status="running",
            prompt="Spend something",
        )
        session.add(run)
        session.commit()
        run_id = run.id
        result = run_agent_turn(session, run, evidence=[], model_step=model_step)
        assert result is not None
        rows = list(
            session.scalars(
                select(ModelUsage).where(
                    ModelUsage.workspace_id == workspace_id,
                    ModelUsage.run_id == run_id,
                )
            )
        )
        assert rows, "the probe call should have landed in the ledger"
        assert {row.agent_id for row in rows} == {agent_id}
    finally:
        session.close()


# --------------------------------------------------------------------------
# The admin panel: by_agent, under the unpriced contract
# --------------------------------------------------------------------------


def test_admin_usage_breaks_spend_down_by_agent(identity_client, db):
    """Three kinds of row: a live agent (labelled by name), a deleted agent
    (its id survives as the key, label empty), and background work with no
    agent at all. The unpriced count rides each group, never folded into the
    dollar figure as zero."""
    client = identity_client(name="Usage owner", workspace_name="Usage workspace")
    workspace_id, agent_id = _boot(client)
    now = utcnow()
    _usage_row(db, workspace_id=workspace_id, agent_id=agent_id, at=now, cost=1.0)
    _usage_row(db, workspace_id=workspace_id, agent_id=agent_id, at=now, cost=2.0)
    _usage_row(db, workspace_id=workspace_id, agent_id="ghost-agent", at=now, cost=None)
    _usage_row(db, workspace_id=workspace_id, agent_id="", at=now, cost=None)
    db.commit()
    agent_name = db.scalar(select(Agent.name).where(Agent.id == agent_id))

    body = client.get("/api/admin/usage").json()
    groups = {group["key"]: group for group in body["by_agent"]}
    assert set(groups) == {agent_id, "ghost-agent", ""}

    live = groups[agent_id]
    assert live["label"] == agent_name
    assert live["calls"] == 2
    assert live["cost_usd"] == pytest.approx(3.0)
    assert live["unpriced_calls"] == 0

    ghost = groups["ghost-agent"]
    # `_group_out` falls back to the key, so a deleted agent's label IS its
    # id — never a borrowed name, never a dropped row.
    assert ghost["label"] == "ghost-agent"
    assert ghost["calls"] == 1
    assert ghost["unpriced_calls"] == 1

    assert groups[""]["calls"] == 1


def test_a_foreign_agents_name_never_labels_another_workspaces_row(
    identity_client, db
):
    """The name join is scoped to the caller's workspace: a ledger row whose
    agent id happens to equal a FOREIGN workspace's agent renders as an id,
    because confirming the name would leak what that workspace calls its
    agents."""
    victim = identity_client(name="Victim", workspace_name="Victim workspace")
    _, victim_agent = _boot(victim)
    spy = identity_client(name="Spy", workspace_name="Spy workspace")
    spy_workspace, _ = _boot(spy)
    _usage_row(
        db,
        workspace_id=spy_workspace,
        agent_id=victim_agent,
        at=utcnow(),
        cost=1.0,
    )
    db.commit()
    body = spy.get("/api/admin/usage").json()
    groups = {group["key"]: group for group in body["by_agent"]}
    assert groups[victim_agent]["label"] == victim_agent


# --------------------------------------------------------------------------
# The watch: fires on a 3× day, once, and re-arms on resolve
# --------------------------------------------------------------------------


def test_the_watch_flags_a_spike_once_and_rearms_on_resolve(identity_client, db):
    base = datetime(2031, 3, 10, 12, 0)
    client = identity_client(name="Watch owner", workspace_name="Watch workspace")
    workspace_id, agent_id = _boot(client)
    _seed_baseline(
        db, workspace_id=workspace_id, agent_id=agent_id, base=base, days=7, cost=0.3
    )
    _usage_row(
        db,
        workspace_id=workspace_id,
        agent_id=agent_id,
        at=base - timedelta(hours=1),
        cost=2.0,
    )
    db.commit()

    flagged = spend_watch.sweep(db, moment=base)
    rows = _anomalies_for(db, workspace_id)
    assert len(rows) == 1
    anomaly = rows[0]
    assert anomaly.id in flagged
    assert anomaly.status == "open"
    assert anomaly.target_user_id == "", "an anomaly is a workspace fact"
    assert anomaly.agent_id == agent_id
    agent_name = db.scalar(select(Agent.name).where(Agent.id == agent_id))
    assert anomaly.title == f"{agent_name} is at 3× its usual spend"
    assert "$2.00" in anomaly.body
    audits = list(
        db.scalars(
            select(AuditEvent).where(
                AuditEvent.workspace_id == workspace_id,
                AuditEvent.action == "spend.anomaly_flagged",
            )
        )
    )
    assert len(audits) == 1
    assert audits[0].resource_id == agent_id
    assert json.loads(audits[0].detail_json)["unit"] == "USD"

    # Same hour: the claim already advanced; the sweep does not even look.
    assert spend_watch.sweep(db, moment=base + timedelta(minutes=5)) == []
    # Next hour: the claim passes, but the room has an unanswered anomaly —
    # repeating it hourly would teach everyone to mute it.
    assert spend_watch.sweep(db, moment=base + timedelta(hours=1, minutes=1)) == []
    db.expire_all()
    assert len(_anomalies_for(db, workspace_id)) == 1

    # Resolving re-arms the watch: the spend is still anomalous, so the next
    # hourly pass may speak again.
    resolved = client.post(f"/api/notifications/{anomaly.id}/resolve")
    assert resolved.status_code == 200
    flagged_again = spend_watch.sweep(db, moment=base + timedelta(hours=2))
    db.expire_all()
    rows = _anomalies_for(db, workspace_id)
    assert len(rows) == 2
    assert [row for row in rows if row.status == "open"][0].id in flagged_again


def test_the_watch_is_quiet_without_three_baseline_days(identity_client, db):
    """A brand-new agent's first busy day is not a deviation from anything —
    two days of history is a coincidence, not a 'usual'."""
    base = datetime(2031, 5, 20, 9, 0)
    client = identity_client(name="Newcomer", workspace_name="Newcomer workspace")
    workspace_id, agent_id = _boot(client)
    _seed_baseline(
        db, workspace_id=workspace_id, agent_id=agent_id, base=base, days=2, cost=0.3
    )
    _usage_row(
        db,
        workspace_id=workspace_id,
        agent_id=agent_id,
        at=base - timedelta(hours=1),
        cost=50.0,
    )
    db.commit()
    spend_watch.sweep(db, moment=base)
    assert _anomalies_for(db, workspace_id) == []


def test_the_watch_is_quiet_over_usual_spend_and_tripled_pennies(
    identity_client, db
):
    """Neither an ordinary day nor a tripling that stays under the dollar
    floor is worth a page — 3× of almost nothing is still almost nothing."""
    base = datetime(2031, 7, 4, 15, 0)
    client = identity_client(name="Steady", workspace_name="Steady workspace")
    workspace_id, agent_id = _boot(client)
    # Usual: today ~ the mean.
    _seed_baseline(
        db, workspace_id=workspace_id, agent_id=agent_id, base=base, days=7, cost=0.5
    )
    _usage_row(
        db,
        workspace_id=workspace_id,
        agent_id=agent_id,
        at=base - timedelta(hours=1),
        cost=0.6,
    )
    # Tripled pennies on a second agent: over 3× the mean, under the floor.
    _seed_baseline(
        db, workspace_id=workspace_id, agent_id="penny-agent", base=base, days=3, cost=0.01
    )
    _usage_row(
        db,
        workspace_id=workspace_id,
        agent_id="penny-agent",
        at=base - timedelta(hours=1),
        cost=0.09,
    )
    db.commit()
    spend_watch.sweep(db, moment=base)
    assert _anomalies_for(db, workspace_id) == []


def test_unpriced_spend_is_watched_in_tokens_with_its_own_floor(
    identity_client, db
):
    """With no configured rates the dollar sums are blind, so the comparison
    happens in the one thing that is always measured — tokens — under the
    100k floor. The spiking agent flags; the small one stays quiet."""
    base = datetime(2031, 9, 1, 8, 0)
    client = identity_client(name="Tokens owner", workspace_name="Tokens workspace")
    workspace_id, _ = _boot(client)
    _seed_baseline(
        db,
        workspace_id=workspace_id,
        agent_id="spike-agent",
        base=base,
        days=3,
        tokens=10_000,
    )
    _usage_row(
        db,
        workspace_id=workspace_id,
        agent_id="spike-agent",
        at=base - timedelta(hours=1),
        tokens=200_000,
    )
    _seed_baseline(
        db,
        workspace_id=workspace_id,
        agent_id="small-agent",
        base=base,
        days=3,
        tokens=1_000,
    )
    _usage_row(
        db,
        workspace_id=workspace_id,
        agent_id="small-agent",
        at=base - timedelta(hours=1),
        tokens=50_000,
    )
    db.commit()
    spend_watch.sweep(db, moment=base)
    rows = _anomalies_for(db, workspace_id)
    assert [row.agent_id for row in rows] == ["spike-agent"]
    # A watch with no name to hand still says who: the id is the title.
    assert rows[0].title.startswith("spike-agent")
    assert "tokens" in rows[0].body
