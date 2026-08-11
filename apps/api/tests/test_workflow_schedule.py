"""The schedule ticker: what a cron fires, and how it fires exactly once.

ADR 0007 stored `schedule_cron` and validated it and dispatched nothing, and said
plainly why that gap mattered: "it will run every Monday" is a promise, and a
promise the system does not keep is worse than a missing feature. This closes it
with a claim-based endpoint an external cron calls — no new always-on service.

The properties worth testing are the ones that make an unauthenticated endpoint
safe to expose:

- the matcher and the validator agree about the grammar, because a field one
  accepts and the other expands differently is an automation that runs on the
  wrong day;
- the claim is at-most-once per workflow per minute, whatever the caller does;
- the caller cannot choose the workflow, the time, or the workspace;
- and with no secret configured, nothing happens at all — which keeps ADR 0007's
  warning honest for any deployment that has not turned scheduling on.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from conftest import Identity, create_identity
from fastapi.testclient import TestClient
from test_workflow_executor import Probe, graph, install, store, tool_node

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import Workflow, WorkflowRun
from app.services.workflows import cron_matches, executor, schedule

TEST_BASE_URL = "https://testserver"


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


def mine(runs: Any, workflow: Workflow) -> list:
    """Only this workflow's runs.

    `dispatch_due` is deliberately global — one cron serves every workspace — so
    a test that counted everything it returned would be measuring the workflows
    other tests left behind.
    """
    return [run for run in runs if run.workflow_id == workflow.id]


# --------------------------------------------------------------------------
# The matcher
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression,moment,expected",
    [
        # Every Monday at 09:00 — the ADR's own example.
        ("0 9 * * 1", at("2026-08-10T09:00"), True),
        ("0 9 * * mon", at("2026-08-10T09:00"), True),
        ("0 9 * * 1", at("2026-08-11T09:00"), False),
        ("0 9 * * 1", at("2026-08-10T09:01"), False),
        ("* * * * *", at("2026-08-10T03:17"), True),
        ("*/15 * * * *", at("2026-08-10T03:15"), True),
        ("*/15 * * * *", at("2026-08-10T03:16"), False),
        ("0 0 1 * *", at("2026-09-01T00:00"), True),
        ("0 0 1 * *", at("2026-09-02T00:00"), False),
        ("0 12 * jul *", at("2026-07-04T12:00"), True),
        ("0 12 * jul *", at("2026-08-04T12:00"), False),
        ("30 2 * * 5-6", at("2026-08-14T02:30"), True),
        # 0 and 7 are both Sunday, and a cron written with one must match a
        # weekday computed as the other.
        ("0 9 * * 0", at("2026-08-09T09:00"), True),
        ("0 9 * * 7", at("2026-08-09T09:00"), True),
        # POSIX: with day-of-month *and* day-of-week both restricted, either
        # matching is enough. Strange rule, universal behaviour.
        ("0 9 13 * 1", at("2026-08-13T09:00"), True),
        ("0 9 13 * 1", at("2026-08-10T09:00"), True),
        ("0 9 13 * 1", at("2026-08-12T09:00"), False),
        # `5/15` is "from 5 upward every 15", not a bare 5.
        ("5/15 * * * *", at("2026-08-10T03:20"), True),
        ("5/15 * * * *", at("2026-08-10T03:06"), False),
    ],
)
def test_the_matcher_agrees_with_the_grammar_the_validator_accepts(
    expression: str, moment: datetime, expected: bool
) -> None:
    from app.services.workflows import cron_error

    assert cron_error(expression) is None, "the validator would reject this"
    assert cron_matches(expression, moment) is expected


def test_an_expression_the_validator_rejects_never_fires() -> None:
    """Fail closed. A cron nobody could have written must not match everything."""
    for bad in ("@daily", "", "0 9 * *", "99 9 * * *", "0 9 * * funday"):
        assert cron_matches(bad, at("2026-08-10T09:00")) is False


def test_a_timezone_is_honoured_and_an_unknown_one_is_refused(
    db: Any, identity: Identity
) -> None:
    """09:00 in Chicago is not 09:00 UTC, and guessing would be worse than not
    running: the workflow would fire at the wrong hour and never say so."""
    workflow = _scheduled(db, identity, cron="0 9 * * *", timezone="America/Chicago")
    # 14:00 UTC is 09:00 CDT on this date.
    assert schedule.due(workflow, moment=at("2026-08-10T14:00")) is True
    assert schedule.due(workflow, moment=at("2026-08-10T09:00")) is False

    workflow.schedule_timezone = "Mars/Olympus_Mons"
    db.commit()
    assert schedule.due(workflow, moment=at("2026-08-10T14:00")) is False


def _scheduled(
    db: Any,
    identity: Identity,
    *,
    cron: str = "0 9 * * *",
    timezone: str = "UTC",
    status: str = "active",
) -> Workflow:
    document = graph(
        [tool_node("only", "probe_read")],
        trigger={"kind": "schedule", "cron": cron, "timezone": timezone},
    )
    workflow = store(db, identity, document)
    workflow.status = status
    db.commit()
    return workflow


# --------------------------------------------------------------------------
# The claim
# --------------------------------------------------------------------------


def test_only_active_scheduled_workflows_are_due(db: Any, identity: Identity) -> None:
    draft = _scheduled(db, identity, status="draft")
    assert schedule.due(draft, moment=at("2026-08-10T09:00")) is False

    manual = store(db, identity, graph([tool_node("only", "probe_read")]))
    manual.status = "active"
    db.commit()
    assert schedule.due(manual, moment=at("2026-08-10T09:00")) is False


def test_a_workflow_is_dispatched_once_per_minute_however_often_the_tick_lands(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The at-most-once guarantee, which is the whole reason to store a claim.

    A cron that retries, a load balancer with three instances behind it, and a
    caller in a loop are all the same event as far as this is concerned.
    """
    install(monkeypatch, Probe("probe_read"))
    workflow = _scheduled(db, identity, cron="* * * * *")
    moment = at("2026-08-10T09:00")

    first = mine(schedule.dispatch_due(db, moment=moment), workflow)
    again = mine(schedule.dispatch_due(db, moment=moment), workflow)
    third = mine(
        schedule.dispatch_due(db, moment=moment + timedelta(seconds=30)), workflow
    )

    assert len(first) == 1
    assert again == [] and third == []
    assert first[0].trigger == "schedule"
    db.refresh(workflow)
    assert workflow.last_dispatched_at == moment

    # A later minute is a new firing.
    later = schedule.dispatch_due(db, moment=moment + timedelta(minutes=1))
    assert len(mine(later, workflow)) == 1


def test_a_claim_lost_to_a_concurrent_ticker_does_not_start_a_run(
    db: Any, identity: Identity
) -> None:
    """The compare-and-swap is what decides, not a read-then-write."""
    workflow = _scheduled(db, identity, cron="* * * * *")
    moment = at("2026-08-10T09:00")
    assert schedule.claim(db, workflow, minute=moment) is True
    assert schedule.claim(db, workflow, minute=moment) is False


def test_downtime_is_not_replayed(db: Any, identity: Identity) -> None:
    """A ticker that catches up on a day of outage sends a day of email.

    One minute of grace covers a slow request. Everything older is dropped, on
    purpose, and the run that never happened is a gap somebody can see.
    """
    workflow = _scheduled(db, identity, cron="0 9 * * *")
    # The 09:00 firing was missed; the tick arrives at 11:00.
    late = schedule.dispatch_due(db, moment=at("2026-08-10T11:00"))
    assert mine(late, workflow) == []
    # A tick one minute late still fires it.
    grace = schedule.dispatch_due(db, moment=at("2026-08-10T09:01"))
    assert len(mine(grace, workflow)) == 1


def test_a_dispatched_run_is_still_policy_gated(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ticker starts work; it never authorises it.

    A scheduled run reaching a write parks exactly as a manual one does, which
    is the behaviour that makes an unattended schedule safe to have at all.
    """
    writer = Probe("probe_write", read_only=False)
    install(monkeypatch, writer)
    document = graph(
        [tool_node("send", "probe_write")],
        trigger={"kind": "schedule", "cron": "* * * * *", "timezone": "UTC"},
    )
    workflow = store(db, identity, document)
    workflow.status = "active"
    db.commit()

    started = mine(schedule.dispatch_due(db, moment=at("2026-08-10T09:00")), workflow)
    assert len(started) == 1
    executor.advance_run(db, started[0])

    assert started[0].status == "waiting_for_approval"
    assert started[0].trigger == "schedule"
    assert writer.calls == []


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------


def test_the_tick_endpoint_is_inert_without_a_configured_secret(
    identity: Identity,
) -> None:
    """The default. A deployment that has not turned scheduling on has genuinely
    inert schedules rather than an endpoint anyone on the internet can drive."""
    with TestClient(app, base_url=TEST_BASE_URL) as client:
        response = client.post("/api/workflows/tick")
    assert response.status_code == 503


def test_the_tick_endpoint_refuses_a_wrong_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _secret(monkeypatch, "correct-horse"):
        with TestClient(app, base_url=TEST_BASE_URL) as client:
            wrong = client.post(
                "/api/workflows/tick", headers={"Authorization": "Bearer nope"}
            )
            missing = client.post("/api/workflows/tick")
    assert wrong.status_code == 401
    assert missing.status_code == 401


def test_the_tick_endpoint_dispatches_what_is_due(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, Probe("probe_read", reply="gathered"))
    workflow = _scheduled(db, identity, cron="* * * * *")

    with _secret(monkeypatch, "correct-horse"):
        with TestClient(app, base_url=TEST_BASE_URL) as client:
            response = client.post(
                "/api/workflows/tick",
                headers={"Authorization": "Bearer correct-horse"},
            )
    assert response.status_code == 200, response.text
    db.expire_all()
    runs = [db.get(WorkflowRun, run_id) for run_id in response.json()["dispatched"]]
    ours = [run for run in runs if run is not None and run.workflow_id == workflow.id]
    assert len(ours) == 1

    run = ours[0]
    assert run.trigger == "schedule"
    # The background task ran it to completion, unattended, read-only.
    assert run.status == "succeeded", run.error


def test_the_tick_endpoint_takes_no_arguments_at_all(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no foreign resource to point it at, which is why it can be
    unauthenticated: it cannot name a workflow, a workspace, or a time."""
    install(monkeypatch, Probe("probe_read"))
    workflow = _scheduled(db, identity, cron="0 9 * * *")

    with _secret(monkeypatch, "correct-horse"):
        with TestClient(app, base_url=TEST_BASE_URL) as client:
            response = client.post(
                "/api/workflows/tick",
                headers={"Authorization": "Bearer correct-horse"},
                json={"workflow_id": "anything", "moment": "2026-08-10T09:00"},
                params={"workflow_id": "anything"},
            )
    assert response.status_code == 200, response.text
    db.expire_all()
    runs = [db.get(WorkflowRun, run_id) for run_id in response.json()["dispatched"]]
    # 09:00 is not now, and neither the body nor the query string could make it so.
    assert [run for run in runs if run is not None and run.workflow_id == workflow.id] == []


def test_the_tick_also_picks_up_a_run_a_dead_process_left_behind(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery on a schedule, not only at boot.

    `recover_durable_work` runs once, at process start, which is the right hook
    for a deploy and no hook at all for a box that does not restart. The tick is
    the only thing that happens every minute, so it is where an orphaned run gets
    picked up — claimed synchronously, executed on a background task, and
    reported separately from what the cron actually dispatched.
    """
    from test_workflow_executor import Kill, orphan

    # `TestClient(app)` runs the lifespan, which starts the boot-time sweep on a
    # thread — and that sweep would race this one for the same run and usually
    # win. Silencing it is what leaves the tick as the only claimant, which is
    # the thing under test.
    from app import main as main_module

    monkeypatch.setattr(main_module, "recover_durable_work", lambda: (0, 0, 0))

    probe = Probe("probe_read", raises=Kill, raise_on=1)
    install(monkeypatch, probe)
    workflow = store(db, identity, graph([tool_node("only", "probe_read")]))
    workflow_run = executor.start_run(db, workflow, user_id=identity.user_id)
    db.commit()
    with pytest.raises(Kill):
        executor.advance_run(db, workflow_run)
    orphan(db, workflow_run)

    with _secret(monkeypatch, "correct-horse"):
        with TestClient(app, base_url=TEST_BASE_URL) as client:
            response = client.post(
                "/api/workflows/tick",
                headers={"Authorization": "Bearer correct-horse"},
            )

    assert response.status_code == 200, response.text
    assert workflow_run.id in response.json()["recovered"]
    db.expire_all()
    resumed = db.get(WorkflowRun, workflow_run.id)
    assert resumed is not None and resumed.status == "succeeded", resumed
    assert len(probe.calls) == 2


class _secret:
    """Point `Settings` at a cron secret for the duration of a block."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        self.monkeypatch = monkeypatch
        self.value = value

    def __enter__(self) -> None:
        from pydantic import SecretStr

        settings = get_settings()
        self.monkeypatch.setattr(
            settings, "workflow_cron_secret", SecretStr(self.value), raising=False
        )

    def __exit__(self, *exc: Any) -> None:
        self.monkeypatch.undo()
