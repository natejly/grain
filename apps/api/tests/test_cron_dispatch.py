"""Personal crons: what one fires, and how it fires exactly once.

A cron is the workflow schedule's sibling — the same claim-based ticker, folded
into the same `POST /api/workflows/tick` behind the same secret — so the
properties worth proving are the ones that make an unattended, unauthenticated
trigger safe to have at all, mirrored one-for-one from `test_workflow_schedule`:

- the claim is at-most-once per cron per minute, whatever the caller does;
- a disabled cron is inert, and the `CATCHUP` window will not replay a day of
  downtime as a day of email;
- and — the load-bearing one — a `kind="task"` fire runs at *workflow* policy
  scope, so the first write parks for a human that a plain chat "always allow"
  would have waved straight through. The whole reason a cron is allowed to run
  with nobody watching is that it cannot cash in a grant made while watching.

Then the ordinary round trip: `kind="message"` posts, and CRUD + run-now are
scoped to the workspace (the isolation suite proves the cross-tenant DENY; here
is the happy path it is the negative of, plus the one 404 that shows a foreign
id is indistinguishable from a missing one).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, List, Optional

import pytest
from conftest import Identity, create_identity
from sqlalchemy import create_engine, inspect, select
from test_workflow_executor import Probe, grant, install

from app.database import SessionLocal, engine
from app.models import (
    AgentToolCall,
    Conversation,
    Cron,
    Message,
    Run,
)
from app.services import crons as cron_service
from app.services.agent_loop import (
    CHAT_SCOPE,
    WORKFLOW_SCOPE,
    policy_scope_for_run,
    run_agent_turn,
)

API_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def identity() -> Identity:
    return create_identity(name="Cron owner", workspace_name="Cron workspace")


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _cron(
    db: Any,
    identity: Identity,
    *,
    kind: str = "task",
    cron: str = "* * * * *",
    timezone: str = "UTC",
    enabled: bool = True,
    prompt: str = "Summarise what changed overnight.",
    body: str = "The standup thread is open.",
    name: str = "Overnight digest",
) -> Cron:
    row = Cron(
        workspace_id=identity.workspace_id,
        created_by=identity.user_id,
        name=name,
        kind=kind,
        schedule_cron=cron,
        schedule_timezone=timezone,
        enabled=enabled,
        prompt=prompt,
        body=body,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def mine(db: Any, run_ids: List[str], identity: Identity) -> List[str]:
    """Only the runs this workspace's crons started.

    `dispatch_due` is deliberately global — one tick serves every workspace — so
    a test that counted everything it returned would be measuring the crons other
    tests left behind.
    """
    kept = []
    for run_id in run_ids:
        run = db.get(Run, run_id)
        if run is not None and run.workspace_id == identity.workspace_id:
            kept.append(run_id)
    return kept


class FakeResponse:
    def __init__(self, output: Optional[List[Any]] = None, output_text: str = "") -> None:
        self.output = output or []
        self.output_text = output_text


def _calls_write_then_stops() -> Callable[..., Any]:
    """A model that asks for one write, then — if it is ever asked again — stops.

    The second step is only reached when the write was *allowed* (a chat run):
    a parked run never returns to the model, so a single write call is all the
    unattended path ever produces.
    """
    state = {"n": 0}

    def step(input_items: Any, tools: Any, instructions: str) -> Any:
        state["n"] += 1
        if state["n"] == 1:
            return [
                (
                    "completed",
                    FakeResponse(
                        output=[
                            SimpleNamespace(
                                type="function_call",
                                name="probe_write",
                                call_id="cron-write-1",
                                arguments='{"text": "hi"}',
                            )
                        ]
                    ),
                )
            ]
        return [("completed", FakeResponse(output=[], output_text="done"))]

    return step


# --------------------------------------------------------------------------
# The claim
# --------------------------------------------------------------------------


def test_a_cron_is_dispatched_once_per_minute_however_often_the_tick_lands(
    db: Any, identity: Identity
) -> None:
    """The at-most-once guarantee, which is the whole reason to store a claim.

    A cron that retries, a load balancer with three instances behind it, and a
    caller in a loop are all the same event as far as this is concerned.
    """
    cron = _cron(db, identity, cron="* * * * *")
    moment = at("2026-08-10T09:00")

    first = mine(db, cron_service.dispatch_due(db, moment=moment), identity)
    again = mine(db, cron_service.dispatch_due(db, moment=moment), identity)
    third = mine(
        db, cron_service.dispatch_due(db, moment=moment + timedelta(seconds=30)), identity
    )

    assert len(first) == 1
    assert again == [] and third == []
    db.refresh(cron)
    assert cron.last_dispatched_at == moment
    # The run it started carries the cron marker that pins it to workflow scope.
    assert db.get(Run, first[0]).cron_id == cron.id

    # A later minute is a new firing.
    later = mine(
        db, cron_service.dispatch_due(db, moment=moment + timedelta(minutes=1)), identity
    )
    assert len(later) == 1


def test_a_claim_lost_to_a_concurrent_ticker_does_not_start_a_run(
    db: Any, identity: Identity
) -> None:
    """The compare-and-swap is what decides, not a read-then-write."""
    cron = _cron(db, identity, cron="* * * * *")
    moment = at("2026-08-10T09:00")
    assert cron_service.claim(db, cron, minute=moment) is True
    assert cron_service.claim(db, cron, minute=moment) is False


def test_a_disabled_cron_never_fires(db: Any, identity: Identity) -> None:
    """The enabled flag is a gate, not a hint. A disabled cron is inert until a
    person re-arms it, at which point the very next tick fires it."""
    cron = _cron(db, identity, cron="* * * * *", enabled=False)
    moment = at("2026-08-10T09:00")

    assert cron_service.due(cron, moment=moment) is False
    assert mine(db, cron_service.dispatch_due(db, moment=moment), identity) == []

    cron.enabled = True
    db.commit()
    assert cron_service.due(cron, moment=moment) is True
    assert len(mine(db, cron_service.dispatch_due(db, moment=moment), identity)) == 1


def test_a_timezone_is_honoured_and_an_unknown_one_is_refused(
    db: Any, identity: Identity
) -> None:
    """09:00 in Chicago is not 09:00 UTC, and guessing would be worse than not
    running: the cron would fire at the wrong hour and never say so."""
    cron = _cron(db, identity, cron="0 9 * * *", timezone="America/Chicago")
    # 14:00 UTC is 09:00 CDT on this date.
    assert cron_service.due(cron, moment=at("2026-08-10T14:00")) is True
    assert cron_service.due(cron, moment=at("2026-08-10T09:00")) is False

    cron.schedule_timezone = "Mars/Olympus_Mons"
    db.commit()
    assert cron_service.due(cron, moment=at("2026-08-10T14:00")) is False


def test_downtime_is_not_replayed(db: Any, identity: Identity) -> None:
    """A ticker that catches up on a day of outage sends a day of email.

    One minute of grace covers a slow request. Everything older is dropped, on
    purpose, and the run that never happened is a gap somebody can see.
    """
    _cron(db, identity, cron="0 9 * * *")
    # The 09:00 firing was missed; the tick arrives at 11:00.
    late = mine(db, cron_service.dispatch_due(db, moment=at("2026-08-10T11:00")), identity)
    assert late == []
    # A tick one minute late still fires it.
    grace = mine(
        db, cron_service.dispatch_due(db, moment=at("2026-08-10T09:01")), identity
    )
    assert len(grace) == 1


# --------------------------------------------------------------------------
# The load-bearing constraint: an unattended task turn is workflow-scoped
# --------------------------------------------------------------------------


def test_a_dispatched_task_run_is_workflow_scoped_and_parks_a_write(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason a cron may run unattended at all.

    The workspace clicked "always allow" on the write once, while typing — a
    *chat* grant. The dispatched task turn is at workflow scope (via `cron_id`),
    so that grant does not reach it: the write parks and waits for a human, and
    nothing is sent. The ticker starts work; it never authorises it.
    """
    writer = Probe("probe_write", read_only=False)
    install(monkeypatch, writer)
    grant(db, identity, "probe_write", "allow", scope=CHAT_SCOPE)

    cron = _cron(db, identity, cron="* * * * *")
    started = mine(
        db, cron_service.dispatch_due(db, moment=at("2026-08-10T09:00")), identity
    )
    assert len(started) == 1

    run = db.get(Run, started[0])
    assert run.cron_id == cron.id
    assert policy_scope_for_run(db, run) == WORKFLOW_SCOPE

    run.status = "running"
    db.commit()
    assert run_agent_turn(db, run, evidence=[], model_step=_calls_write_then_stops()) is None

    assert writer.calls == [], "an unattended cron performed a write"
    assert run.status == "waiting_for_approval"
    parked = db.scalar(
        select(AgentToolCall).where(
            AgentToolCall.run_id == run.id, AgentToolCall.status == "proposed"
        )
    )
    assert parked is not None and parked.name == "probe_write"


def test_the_same_grant_lets_an_ordinary_chat_run_write(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the split: the grant is real, it is only the *scope*
    that withholds it from a cron. An identical run with no `cron_id` resolves at
    chat scope, the chat allow applies, and the write goes through — so the park
    above is the scope talking, not the write tool always parking."""
    writer = Probe("probe_write", read_only=False)
    install(monkeypatch, writer)
    grant(db, identity, "probe_write", "allow", scope=CHAT_SCOPE)

    from app.models import Agent

    agent = db.scalar(select(Agent).where(Agent.workspace_id == identity.workspace_id))
    conversation = Conversation(
        workspace_id=identity.workspace_id, created_by=identity.user_id
    )
    db.add(conversation)
    db.flush()
    run = Run(
        workspace_id=identity.workspace_id,
        conversation_id=conversation.id,
        agent_id=agent.id,
        created_by=identity.user_id,
        status="running",
        prompt="write it",
    )
    db.add(run)
    db.commit()

    assert policy_scope_for_run(db, run) == CHAT_SCOPE
    result = run_agent_turn(db, run, evidence=[], model_step=_calls_write_then_stops())
    assert result is not None, "the chat run parked despite a standing allow"
    assert writer.calls == [{"text": "hi"}]


# --------------------------------------------------------------------------
# kind="message"
# --------------------------------------------------------------------------


def test_a_message_cron_posts_into_a_created_then_reused_conversation(
    db: Any, identity: Identity
) -> None:
    """A message cron creates its conversation on the first fire and reuses it on
    the next, so the thread accumulates rather than fragmenting — and it starts
    no run, because there is nothing to run."""
    cron = _cron(db, identity, kind="message", cron="* * * * *", body="Daily nudge.")

    first = mine(db, cron_service.dispatch_due(db, moment=at("2026-08-10T09:00")), identity)
    assert first == [], "a message cron has no task run to enqueue"

    db.refresh(cron)
    conversation_id = cron.target_conversation_id
    assert conversation_id, "the first fire did not name a conversation"

    second = mine(
        db, cron_service.dispatch_due(db, moment=at("2026-08-10T09:01")), identity
    )
    assert second == []
    db.refresh(cron)
    assert cron.target_conversation_id == conversation_id, "the thread fragmented"

    posts = list(
        db.scalars(
            select(Message).where(Message.conversation_id == conversation_id)
        )
    )
    assert len(posts) == 2
    assert {m.content for m in posts} == {"Daily nudge."}
    assert {m.role for m in posts} == {"user"}
    assert {m.run_id for m in posts} == {""}


# --------------------------------------------------------------------------
# CRUD + run-now, scoped to the workspace
# --------------------------------------------------------------------------


def test_crud_round_trip_and_run_now(
    db: Any, identity_client: Callable[..., Any]
) -> None:
    """The ordinary lifecycle, through the API a person drives: create, list,
    enable/disable by PATCH, run-now, delete, and a 404 after."""
    client = identity_client(name="CRUD owner", workspace_name="CRUD workspace")

    created = client.post(
        "/api/crons",
        json={
            "name": "Morning brief",
            "kind": "task",
            "schedule_cron": "0 9 * * *",
            "schedule_timezone": "UTC",
            "prompt": "What changed overnight?",
        },
    )
    assert created.status_code == 201, created.text
    cron_id = created.json()["id"]
    assert created.json()["enabled"] is True
    assert created.json()["last_dispatched_at"] is None

    listed = client.get("/api/crons")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [cron_id]

    disabled = client.patch(f"/api/crons/{cron_id}", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    # run-now is claim-free: it fires regardless of the schedule or last tick.
    ran = client.post(f"/api/crons/{cron_id}/run-now")
    assert ran.status_code == 202, ran.text
    run_id = ran.json()["run_id"]
    assert run_id, "a task run-now returned no run to watch"
    run = db.get(Run, run_id)
    assert run is not None and run.cron_id == cron_id

    # A bad schedule is refused at the boundary, not swallowed by a silent tick.
    rejected = client.patch(f"/api/crons/{cron_id}", json={"schedule_cron": "99 9 * * *"})
    assert rejected.status_code == 422

    assert client.delete(f"/api/crons/{cron_id}").status_code == 204
    assert client.get(f"/api/crons/{cron_id}").status_code == 404


def test_a_foreign_cron_is_indistinguishable_from_a_missing_one(
    identity_client: Callable[..., Any]
) -> None:
    """Scoping, from the outside: a second tenant cannot see, read, mutate, or
    fire another workspace's cron — every route answers 404, the same as a name
    nobody ever created."""
    owner = identity_client(name="Owner", workspace_name="Owner workspace")
    other = identity_client(name="Other", workspace_name="Other workspace")

    cron_id = owner.post(
        "/api/crons",
        json={"name": "Private", "kind": "task", "schedule_cron": "0 9 * * *"},
    ).json()["id"]

    assert other.get("/api/crons").json() == []
    assert other.get(f"/api/crons/{cron_id}").status_code == 404
    assert other.patch(f"/api/crons/{cron_id}", json={"enabled": False}).status_code == 404
    assert other.post(f"/api/crons/{cron_id}/run-now").status_code == 404
    assert other.delete(f"/api/crons/{cron_id}").status_code == 404
    # And the cron is untouched: its owner still sees it.
    assert owner.get(f"/api/crons/{cron_id}").json()["enabled"] is True


# --------------------------------------------------------------------------
# ORM <-> migration parity
# --------------------------------------------------------------------------


def test_the_migration_chain_builds_the_cron_table_the_orm_declares() -> None:
    """`alembic upgrade head` from an empty database must match `create_all`.

    The whole chain, on a scratch database, because a migration that only works
    on top of a schema someone already had fails on the first new deployment.
    Then `crons` and the new `runs.cron_id` column are compared against the ORM:
    production gets the alembic schema and development gets the metadata one, and
    a difference between them is a bug that only ever appears in production.
    """
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
        assert "crons" in migrated.get_table_names()
        # `conversations`/`messages` gained columns in 0037; a shared thread's
        # visibility rides `conversations.shared`, so a missing column would be a
        # production-only leak or crash.
        for table in ("crons", "runs", "conversations", "messages"):
            assert {column["name"] for column in migrated.get_columns(table)} == {
                column["name"] for column in declared.get_columns(table)
            }, table
            assert {index["name"] for index in migrated.get_indexes(table)} >= {
                index["name"] for index in declared.get_indexes(table)
            }, table
