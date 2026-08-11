"""The spend ceiling: what it stops, what it must not stop, and what it does
when nothing has a price.

Five things are being proved.

*It fires at the boundary, before the call.* The predicate is exercised on both
sides of the line as pure arithmetic, and then again through a real agent turn
whose model step records whether it was ever reached. A ceiling that stops the
seventh call after paying for the sixth is not a ceiling.

*An unpriced model does not slip past it.* `MODEL_PRICES` ships empty, so the
interesting case is not "over the dollar limit" but "the dollar limit cannot see
this spend at all". That case parks, and the reason it reports says so.

*Unattended work is held tighter.* A workflow node is measured against its own
spend and a fraction of the ceiling, because nobody is at the diff.

*Raising the limit resumes the run.* Through the owner's route, on the real
resume path, with the same predicate consulted again — so a raise that is still
not enough releases nothing.

*One workspace's spend is not another's.* The oldest bug in every metering
system, asserted rather than assumed.
"""
from __future__ import annotations

import json
import os
from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, create_identity
from fastapi.testclient import TestClient

from app.clock import utcnow
from app.config import ModelPrice, Settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    AgentToolCall,
    ModelUsage,
    Run,
    RunEvent,
    Workflow,
    WorkflowRun,
    WorkspaceBudget,
)
from app.services import budget
from app.services.agent_loop import (
    PAUSED_FOR_APPROVAL,
    PAUSED_FOR_BUDGET,
    run_agent_turn,
)

PRICE = ModelPrice(input=1.0, output=10.0)
PRICED_MODEL = "priced-budget-model"
UNPRICED_MODEL = "unpriced-budget-model"


def budget_settings(**overrides: object) -> Settings:
    """A Settings whose ceiling is whatever the test is about."""
    return Settings(
        _env_file=None,
        app_env="test",
        model_provider="scripted",
        scripted_model_script="apps/api/tests/scripts/agent.json",
        model_prices={PRICED_MODEL: PRICE},
        **overrides,
    )


def ceiling(usd=None, tokens=None, window_hours=24, source="settings") -> budget.Ceiling:
    return budget.Ceiling(
        window_hours=window_hours, usd=usd, tokens=tokens, source=source
    )


def spend(calls=1, priced_calls=1, cost_usd=0.0, total_tokens=0) -> budget.Spend:
    return budget.Spend(
        calls=calls,
        priced_calls=priced_calls,
        cost_usd=cost_usd,
        total_tokens=total_tokens,
    )


def plant_usage(
    workspace_id: str,
    *,
    cost_usd: float | None,
    total_tokens: int = 1000,
    operation: str = "chat",
    age: timedelta = timedelta(minutes=1),
) -> str:
    """One ledger row, as `record_model_usage` would have written it.

    Written straight to the table: this suite is about the *ceiling*, and
    routing the setup through a real model call would make these tests fail for
    reasons that belong to `test_model_usage.py`.
    """
    db = SessionLocal()
    try:
        row = ModelUsage(
            workspace_id=workspace_id,
            operation=operation,
            model=PRICED_MODEL if cost_usd is not None else UNPRICED_MODEL,
            input_tokens=total_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            created_at=utcnow() - age,
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


@pytest.fixture
def tenant() -> Identity:
    return create_identity(name="Budget owner", workspace_name="Budget workspace")


@pytest.fixture
def owner_client(tenant: Identity) -> TestClient:
    return authenticate(TestClient(app, base_url=TEST_BASE_URL), tenant)


class _Recorder:
    """A model step that remembers whether it was ever reached."""

    def __init__(self, answer: str = "Done.") -> None:
        self.calls = 0
        self.answer = answer

    def __call__(self, input_items, tools, instructions):
        self.calls += 1
        return [
            ("completed", SimpleNamespace(output=[], output_text=self.answer)),
        ]


def make_run(client: TestClient, tenant: Identity, prompt: str = "Summarise") -> str:
    bootstrap = client.get("/api/bootstrap").json()
    conversation_id = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "budget-conv-" + os.urandom(6).hex()},
        json={"title": "Budget"},
    ).json()["id"]
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=tenant.workspace_id,
            conversation_id=conversation_id,
            agent_id=bootstrap["default_agent_id"],
            created_by=tenant.user_id,
            status="running",
            prompt=prompt,
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def back_with_a_workflow(run_id: str, tenant: Identity) -> str:
    """Give a run a `workflow_runs` row, which is what makes it unattended.

    `policy_scope_for_run` reads exactly this, so it is also what decides which
    ceiling the turn is measured against.
    """
    db = SessionLocal()
    try:
        workflow = Workflow(
            workspace_id=tenant.workspace_id,
            created_by=tenant.user_id,
            name="Nightly",
            source_prompt="summarise every night",
            graph_json=json.dumps({"nodes": [], "edges": []}),
        )
        db.add(workflow)
        db.flush()
        workflow_run = WorkflowRun(
            workspace_id=tenant.workspace_id,
            workflow_id=workflow.id,
            created_by=tenant.user_id,
            workflow_version=1,
            graph_json=workflow.graph_json,
            trigger="schedule",
            status="running",
            run_id=run_id,
        )
        db.add(workflow_run)
        db.commit()
        return workflow_run.id
    finally:
        db.close()


def drive(run_id: str, settings: Settings, step: _Recorder, *, workflow=False):
    """Take one turn on an existing run and hand back (outcome-ish, run row)."""
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        result = run_agent_turn(
            db,
            run,
            evidence=[],
            settings=settings,
            model_step=step,
            workflow_node=workflow,
        )
        db.refresh(run)
        return result, run
    finally:
        db.close()


def reload_run(run_id: str) -> Run:
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        db.refresh(run)
        db.expunge(run)
        return run
    finally:
        db.close()


# --------------------------------------------------------------------------
# The predicate, as arithmetic


def test_the_dollar_ceiling_stops_at_the_boundary_and_not_before():
    """`>=`, deliberately. Reaching the cap is reaching it.

    The call one cent under must proceed and the call exactly at the line must
    not; letting the boundary through would make every limit mean "the limit
    plus one more turn", and a turn has no size bound of its own.
    """
    limit = ceiling(usd=10.0)
    assert budget.exceeds(limit, spend(cost_usd=9.99)) == ""
    assert budget.exceeds(limit, spend(cost_usd=10.0)) == budget.USD
    assert budget.exceeds(limit, spend(cost_usd=10.01)) == budget.USD


def test_the_token_ceiling_stops_at_its_own_boundary():
    limit = ceiling(tokens=1000)
    assert budget.exceeds(limit, spend(total_tokens=999)) == ""
    assert budget.exceeds(limit, spend(total_tokens=1000)) == budget.TOKENS


def test_no_ceiling_permits_everything():
    """Unset is unlimited, and must stay unlimited however large the spend."""
    assert budget.exceeds(ceiling(), spend(cost_usd=1e9, total_tokens=10**12)) == ""


def test_a_dollar_ceiling_alone_refuses_to_wave_through_spend_it_cannot_price():
    """The failure this whole module exists to prevent.

    A workspace nowhere near its dollar limit, whose every call was made on a
    model with no configured rate: `cost_usd` sums to nothing, the ceiling is
    never reached, and the limit an operator configured does nothing at all.
    Stopping is the only honest answer — not because the workspace is over, but
    because nothing here can tell whether it is, and it was asked to.
    """
    unpriced = spend(calls=40, priced_calls=0, cost_usd=0.0, total_tokens=10**7)
    assert budget.exceeds(ceiling(usd=10.0), unpriced) == budget.UNPRICED


def test_a_token_ceiling_beside_it_is_what_makes_unpriced_spend_bounded():
    """The documented fix, and it must actually lift the refusal.

    With a token ceiling present, unpriced calls are bounded by a measurement
    this app always has — so they are no longer spend nothing is watching, and
    the verdict turns on the tokens rather than on the missing price.
    """
    unpriced = spend(calls=40, priced_calls=0, cost_usd=0.0, total_tokens=900)
    assert budget.exceeds(ceiling(usd=10.0, tokens=1000), unpriced) == ""
    over = spend(calls=40, priced_calls=0, cost_usd=0.0, total_tokens=1000)
    assert budget.exceeds(ceiling(usd=10.0, tokens=1000), over) == budget.TOKENS


def test_the_unattended_ceiling_is_a_fraction_and_unlimited_has_no_fraction():
    assert ceiling(usd=10.0, tokens=1000).scaled(0.5) == ceiling(usd=5.0, tokens=500)
    # Half of no limit is no limit. Anything else would invent the surprise cap
    # this feature promises never to become.
    assert ceiling().scaled(0.5) == ceiling()


# --------------------------------------------------------------------------
# Evaluating against the ledger


def test_an_unset_ceiling_never_reads_the_ledger(tenant, monkeypatch):
    """The common path must cost nothing.

    Every model step in every deployment that never asked for a ceiling calls
    this, so the fast path is a property worth pinning: `window_spend` is
    replaced with something that raises, and the verdict is still allow.
    """

    def explode(*args, **kwargs):
        raise AssertionError("an unset ceiling must not query the ledger")

    monkeypatch.setattr(budget, "window_spend", explode)
    db = SessionLocal()
    try:
        verdict = budget.evaluate(
            db,
            workspace_id=tenant.workspace_id,
            unattended=False,
            settings=budget_settings(),
        )
    finally:
        db.close()
    assert verdict.allowed


def test_one_workspace_spend_is_not_counted_against_another(tenant):
    """Cross-tenant: the neighbour's runaway must not stop this workspace."""
    neighbour = create_identity(name="Neighbour", workspace_name="Neighbour workspace")
    plant_usage(neighbour.workspace_id, cost_usd=500.0, total_tokens=10**6)
    settings = budget_settings(budget_usd_per_window=10.0)
    db = SessionLocal()
    try:
        mine = budget.evaluate(
            db,
            workspace_id=tenant.workspace_id,
            unattended=False,
            settings=settings,
        )
        theirs = budget.evaluate(
            db,
            workspace_id=neighbour.workspace_id,
            unattended=False,
            settings=settings,
        )
    finally:
        db.close()
    assert mine.allowed
    assert not theirs.allowed and theirs.reason == budget.USD


def test_spend_outside_the_window_has_rolled_off(tenant):
    plant_usage(tenant.workspace_id, cost_usd=50.0, age=timedelta(hours=30))
    db = SessionLocal()
    try:
        verdict = budget.evaluate(
            db,
            workspace_id=tenant.workspace_id,
            unattended=False,
            settings=budget_settings(
                budget_usd_per_window=10.0, budget_window_hours=24
            ),
        )
    finally:
        db.close()
    assert verdict.allowed


def test_unattended_work_is_held_to_a_tighter_ceiling_than_a_person_typing(tenant):
    """The scheduled-workflow risk, which is the one nobody is watching.

    Six dollars of workflow spend against a ten dollar ceiling: a person typing
    is well under and proceeds, and the automation that produced the spend is
    over its own half-ceiling and does not.
    """
    plant_usage(tenant.workspace_id, cost_usd=6.0, operation="workflow_node")
    settings = budget_settings(
        budget_usd_per_window=10.0, unattended_budget_fraction=0.5
    )
    db = SessionLocal()
    try:
        interactive = budget.evaluate(
            db, workspace_id=tenant.workspace_id, unattended=False, settings=settings
        )
        automated = budget.evaluate(
            db, workspace_id=tenant.workspace_id, unattended=True, settings=settings
        )
    finally:
        db.close()
    assert interactive.allowed
    assert not automated.allowed
    assert automated.unattended and automated.reason == budget.USD


def test_a_persons_spend_does_not_exhaust_the_automation_budget(tenant):
    """The other direction, and the reason the two ceilings read different rows.

    A busy afternoon of conversation must not stop tonight's report: the
    unattended ceiling is measured over unattended spend, not over the
    workspace's.
    """
    plant_usage(tenant.workspace_id, cost_usd=6.0, operation="chat")
    db = SessionLocal()
    try:
        automated = budget.evaluate(
            db,
            workspace_id=tenant.workspace_id,
            unattended=True,
            settings=budget_settings(
                budget_usd_per_window=10.0, unattended_budget_fraction=0.5
            ),
        )
    finally:
        db.close()
    assert automated.allowed


def test_a_ledger_that_cannot_be_read_allows_the_call(tenant, monkeypatch):
    """Accounting never breaks a turn, and neither does the ceiling on top of it.

    This is a spend control, not a security control. The worst case of failing
    open is an invoice; the worst case of failing closed is a product that stops
    working because a SELECT timed out.
    """

    def explode(*args, **kwargs):
        raise RuntimeError("the database is having a day")

    monkeypatch.setattr(budget, "window_spend", explode)
    db = SessionLocal()
    try:
        verdict = budget.evaluate(
            db,
            workspace_id=tenant.workspace_id,
            unattended=False,
            settings=budget_settings(budget_usd_per_window=0.01),
        )
    finally:
        db.close()
    assert verdict.allowed
    assert verdict.ceiling.source == "unavailable"


def test_a_workspace_row_replaces_the_deployment_ceiling(tenant):
    db = SessionLocal()
    try:
        db.add(
            WorkspaceBudget(
                workspace_id=tenant.workspace_id,
                window_hours=6,
                usd_per_window=99.0,
                tokens_per_window=None,
            )
        )
        db.commit()
        found = budget.effective_ceiling(
            db,
            workspace_id=tenant.workspace_id,
            settings=budget_settings(
                budget_usd_per_window=1.0, budget_window_hours=24
            ),
        )
    finally:
        db.close()
    assert found == budget.Ceiling(
        window_hours=6, usd=99.0, tokens=None, source="workspace"
    )


# --------------------------------------------------------------------------
# Enforcement, inside a real turn


def test_a_turn_at_the_ceiling_parks_before_the_model_is_called(
    tenant, owner_client
):
    """The whole point: the check runs *before* the expensive call.

    The model step counts its own invocations, so this asserts the thing that
    matters rather than its shadow — not "the run parked" but "the provider was
    never asked".
    """
    plant_usage(tenant.workspace_id, cost_usd=10.0)
    run_id = make_run(owner_client, tenant)
    step = _Recorder()
    result, run = drive(
        run_id, budget_settings(budget_usd_per_window=10.0), step
    )
    assert step.calls == 0
    assert result is None
    assert run.status == "waiting_for_approval"
    assert run.paused_reason == PAUSED_FOR_BUDGET
    # The turn is resumable, which is the difference between parking and killing.
    assert run.agent_state_json


def test_a_turn_one_cent_under_the_ceiling_runs(tenant, owner_client):
    """The boundary's other side, on the same machinery as the test above."""
    plant_usage(tenant.workspace_id, cost_usd=9.99)
    run_id = make_run(owner_client, tenant)
    step = _Recorder()
    result, run = drive(
        run_id, budget_settings(budget_usd_per_window=10.0), step
    )
    assert step.calls == 1
    assert result is not None and result.answer == "Done."


def test_an_unpriced_model_parks_rather_than_slipping_past_the_dollar_ceiling(
    tenant, owner_client
):
    """A deployment with no price list configured is the shipped default."""
    for _ in range(3):
        plant_usage(tenant.workspace_id, cost_usd=None, total_tokens=50_000)
    run_id = make_run(owner_client, tenant)
    step = _Recorder()
    _result, run = drive(
        run_id, budget_settings(budget_usd_per_window=1000.0), step
    )
    assert step.calls == 0
    assert run.paused_reason == PAUSED_FOR_BUDGET

    event = _last_budget_event(run.id)
    assert event["reason"] == budget.UNPRICED
    assert event["unpriced_calls"] == 3
    # The message names the fix, not only the fault.
    assert "MODEL_PRICES" in event["message"]


def test_an_unpriced_model_is_bounded_by_the_token_ceiling_when_one_is_set(
    tenant, owner_client
):
    """The documented fallback, end to end: tokens govern what dollars cannot."""
    plant_usage(tenant.workspace_id, cost_usd=None, total_tokens=400)
    run_id = make_run(owner_client, tenant)
    under = _Recorder()
    _result, run = drive(
        run_id,
        budget_settings(budget_usd_per_window=1000.0, budget_tokens_per_window=1000),
        under,
    )
    assert under.calls == 1
    assert run.paused_reason == ""

    plant_usage(tenant.workspace_id, cost_usd=None, total_tokens=600)
    second = make_run(owner_client, tenant)
    over = _Recorder()
    _result, run = drive(
        second,
        budget_settings(budget_usd_per_window=1000.0, budget_tokens_per_window=1000),
        over,
    )
    assert over.calls == 0
    assert _last_budget_event(run.id)["reason"] == budget.TOKENS


def test_a_parked_on_budget_run_is_distinguishable_from_a_parked_on_approval_run(
    tenant, owner_client
):
    """Two runs, both `waiting_for_approval`, told apart by the column and the
    event — and only one of them has a card to click."""
    plant_usage(tenant.workspace_id, cost_usd=10.0)
    run_id = make_run(owner_client, tenant)
    _result, run = drive(run_id, budget_settings(budget_usd_per_window=10.0), _Recorder())

    assert run.status == "waiting_for_approval"
    assert run.paused_reason == PAUSED_FOR_BUDGET
    assert run.paused_reason != PAUSED_FOR_APPROVAL

    db = SessionLocal()
    try:
        # No approval row, because there is no proposed call: the model had not
        # been asked yet. A card here would approve nothing.
        assert (
            db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).count() == 0
        )
        kinds = [
            event.event_type
            for event in db.query(RunEvent).filter(RunEvent.run_id == run_id).all()
        ]
    finally:
        db.close()
    assert "run.waiting_for_budget" in kinds
    assert "run.waiting_for_approval" not in kinds


def test_a_scheduled_workflow_turn_is_stopped_by_the_tighter_ceiling(
    tenant, owner_client
):
    """The runaway that nobody is watching, stopped end to end.

    The same six dollars that leaves a person typing well inside the ceiling
    parks the automation that spent it.
    """
    plant_usage(tenant.workspace_id, cost_usd=6.0, operation="workflow_node")
    run_id = make_run(owner_client, tenant)
    back_with_a_workflow(run_id, tenant)
    step = _Recorder()
    _result, run = drive(
        run_id,
        budget_settings(budget_usd_per_window=10.0, unattended_budget_fraction=0.5),
        step,
        workflow=True,
    )
    assert step.calls == 0
    assert run.paused_reason == PAUSED_FOR_BUDGET
    assert _last_budget_event(run.id)["unattended"] is True


def _last_budget_event(run_id: str) -> dict:
    db = SessionLocal()
    try:
        row = (
            db.query(RunEvent)
            .filter(
                RunEvent.run_id == run_id,
                RunEvent.event_type == "run.waiting_for_budget",
            )
            .order_by(RunEvent.sequence.desc())
            .first()
        )
        assert row is not None, "no run.waiting_for_budget event was written"
        return json.loads(row.payload_json)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Raising the limit, and resuming


def test_raising_the_limit_releases_the_parked_run(tenant, owner_client, monkeypatch):
    """The owner's way out, on the real resume path.

    The ceiling here is the workspace's own row, because that is the one an
    owner can move at 3am; the turn resumes through `resume_run_after_budget`
    and the run finishes.
    """
    monkeypatch.setattr(
        "app.services.agent_loop._default_model_step",
        lambda settings, run, evidence: (
            lambda input_items, tools, instructions: [
                ("completed", SimpleNamespace(output=[], output_text="Resumed."))
            ]
        ),
    )
    assert (
        owner_client.put(
            "/api/admin/budget",
            json={"window_hours": 24, "usd_per_window": 5.0},
        ).status_code
        == 200
    )
    plant_usage(tenant.workspace_id, cost_usd=6.0)
    run_id = make_run(owner_client, tenant)
    _result, run = drive(run_id, budget_settings(), _Recorder())
    assert run.paused_reason == PAUSED_FOR_BUDGET

    response = owner_client.put(
        "/api/admin/budget", json={"window_hours": 24, "usd_per_window": 100.0}
    )
    assert response.status_code == 200
    assert response.json()["resumed_run_ids"] == [run_id]

    resumed = reload_run(run_id)
    assert resumed.status == "completed"
    assert resumed.paused_reason == ""
    assert resumed.agent_state_json is None


def test_a_raise_that_is_still_not_enough_releases_nothing(tenant, owner_client):
    """The release path consults the same predicate the loop enforces, so it
    cannot hold a more generous opinion than the ceiling itself."""
    owner_client.put(
        "/api/admin/budget", json={"window_hours": 24, "usd_per_window": 5.0}
    )
    plant_usage(tenant.workspace_id, cost_usd=6.0)
    run_id = make_run(owner_client, tenant)
    _result, run = drive(run_id, budget_settings(), _Recorder())
    assert run.paused_reason == PAUSED_FOR_BUDGET

    response = owner_client.put(
        "/api/admin/budget", json={"window_hours": 24, "usd_per_window": 5.5}
    )
    assert response.json()["resumed_run_ids"] == []
    assert reload_run(run_id).paused_reason == PAUSED_FOR_BUDGET


def test_raising_a_limit_does_not_touch_a_run_parked_on_an_approval(
    tenant, owner_client
):
    """`paused_reason` earning its keep: an approval is waiting on a decision
    this path does not have and must not invent."""
    run_id = make_run(owner_client, tenant)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        run.status = "waiting_for_approval"
        run.paused_reason = PAUSED_FOR_APPROVAL
        run.agent_state_json = json.dumps({"input_items": [], "pending_calls": []})
        db.commit()
    finally:
        db.close()

    response = owner_client.put(
        "/api/admin/budget", json={"window_hours": 24, "usd_per_window": 1000.0}
    )
    assert response.json()["resumed_run_ids"] == []
    assert response.json()["runs_parked_on_budget"] == []
    still_parked = reload_run(run_id)
    assert still_parked.status == "waiting_for_approval"
    assert still_parked.paused_reason == PAUSED_FOR_APPROVAL


def test_cancelling_a_parked_run_ends_the_park_and_the_reason_with_it(
    tenant, owner_client
):
    """`paused_reason` describes a park, so it must not outlive one.

    Load-bearing rather than tidy: the release query finds runs by
    `paused_reason`, so a stale one on a cancelled run would put a run nobody
    can resume into the owner's "parked on budget" list forever.
    """
    plant_usage(tenant.workspace_id, cost_usd=10.0)
    run_id = make_run(owner_client, tenant)
    drive(run_id, budget_settings(budget_usd_per_window=10.0), _Recorder())
    assert reload_run(run_id).paused_reason == PAUSED_FOR_BUDGET

    response = owner_client.post(
        f"/api/runs/{run_id}/cancel",
        headers={"Idempotency-Key": "budget-cancel-" + os.urandom(6).hex()},
    )
    assert response.status_code == 200
    cancelled = reload_run(run_id)
    assert cancelled.status == "cancelled"
    assert cancelled.paused_reason == ""
    assert owner_client.get("/api/admin/budget").json()["runs_parked_on_budget"] == []


# --------------------------------------------------------------------------
# The route, and the configuration guard


def test_the_budget_route_reports_the_ceiling_the_spend_and_what_it_holds(
    tenant, owner_client
):
    plant_usage(tenant.workspace_id, cost_usd=2.0, total_tokens=1000)
    plant_usage(tenant.workspace_id, cost_usd=None, total_tokens=500)
    owner_client.put(
        "/api/admin/budget",
        json={"window_hours": 24, "usd_per_window": 10.0, "tokens_per_window": 9000},
    )
    body = owner_client.get("/api/admin/budget").json()
    assert body["ceiling"] == {
        "window_hours": 24,
        "usd_per_window": 10.0,
        "tokens_per_window": 9000,
        "source": "workspace",
    }
    # Halved for unattended work, from the same numbers.
    assert body["unattended_ceiling"]["usd_per_window"] == 5.0
    assert body["unattended_ceiling"]["tokens_per_window"] == 4500
    assert body["spend"]["cost_usd"] == 2.0
    assert body["spend"]["total_tokens"] == 1500
    # The figure that says whether the dollar ceiling can see everything.
    assert body["spend"]["unpriced_calls"] == 1
    assert body["enforced"] is True
    assert body["runs_parked_on_budget"] == []


def test_the_budget_route_reports_an_unconfigured_deployment_as_unenforced(
    owner_client,
):
    body = owner_client.get("/api/admin/budget").json()
    assert body["enforced"] is False
    assert body["ceiling"]["source"] == "settings"
    assert body["ceiling"]["usd_per_window"] is None


def test_a_dollar_ceiling_with_no_price_list_refuses_to_boot():
    """The mistake `_guard_budget` exists to make impossible.

    A configured ceiling that no call can ever reach is worse than no ceiling,
    because the operator stops watching. Failing at startup is the same
    structural gate the model provider and the sandbox already use.
    """
    with pytest.raises(ValueError) as caught:
        Settings(
            _env_file=None,
            app_env="test",
            model_provider="scripted",
            scripted_model_script="apps/api/tests/scripts/agent.json",
            model_prices={},
            budget_usd_per_window=100.0,
        )
    message = str(caught.value)
    # The error names all three ways out, because which is right is the
    # operator's call and not ours.
    assert "MODEL_PRICES" in message
    assert "BUDGET_TOKENS_PER_WINDOW" in message


def test_a_dollar_ceiling_boots_once_the_spend_it_cannot_price_is_bounded():
    settings = Settings(
        _env_file=None,
        app_env="test",
        model_provider="scripted",
        scripted_model_script="apps/api/tests/scripts/agent.json",
        model_prices={},
        budget_usd_per_window=100.0,
        budget_tokens_per_window=1_000_000,
    )
    assert settings.budget_tokens_per_window == 1_000_000


def test_the_default_configuration_imposes_no_ceiling():
    """An existing deployment upgrading into this release must not discover a
    cap nobody chose."""
    settings = budget_settings()
    assert settings.budget_usd_per_window is None
    assert settings.budget_tokens_per_window is None
