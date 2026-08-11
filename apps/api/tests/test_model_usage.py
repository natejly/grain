"""Token and cost accounting: what it records, what it refuses to record.

Four things are being proved.

*It counts.* Both usage shapes the OpenAI SDK returns land in the same row —
the Responses API's `input_tokens`/`output_tokens` with their detail objects, and
the embeddings API's `prompt_tokens`/`total_tokens` — and the chokepoints in
`services/model.py` and `services/embeddings.py` write one without their callers
doing anything.

*It never breaks a turn.* Missing usage, malformed usage, an unattributed call
and a database that refuses the insert all return quietly. The assertion is that
nothing raises, because a run that failed because it could not bill itself would
be strictly worse than a gap in the ledger.

*It does not invent a price.* No configured rate means tokens and a null cost,
and the rate that produced a cost is frozen into the row so a later price change
cannot reprice history.

*It holds no content.* The column set is pinned, so a "preview" field cannot join
this table without a test failing first.
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
from app.models import ModelUsage
from app.services import embeddings as embeddings_service
from app.services import model as model_service
from app.services import usage as usage_service
from app.services.agent_loop import CHAT_SCOPE, WORKFLOW_SCOPE, _billing_operation

# A rate that makes the arithmetic checkable by hand: 1 USD per million input,
# 10 per million output, a tenth for cached input.
PRICE = ModelPrice(input=1.0, output=10.0, cached_input=0.1)
PRICED_MODEL = "priced-test-model"
UNPRICED_MODEL = "unpriced-test-model"


def priced_settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        model_provider="scripted",
        scripted_model_script="apps/api/tests/scripts/agent.json",
        model_prices={PRICED_MODEL: PRICE},
        **overrides,
    )


def responses_usage(
    input_tokens: int = 1000,
    output_tokens: int = 200,
    cached: int = 400,
    reasoning: int = 150,
) -> SimpleNamespace:
    """The shape the Responses API reports."""
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_tokens_details=SimpleNamespace(cached_tokens=cached),
        output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning),
    )


def rows_for(workspace_id: str) -> list[ModelUsage]:
    db = SessionLocal()
    try:
        return list(
            db.query(ModelUsage)
            .filter(ModelUsage.workspace_id == workspace_id)
            .order_by(ModelUsage.created_at, ModelUsage.id)
            .all()
        )
    finally:
        db.close()


@pytest.fixture
def tenant() -> Identity:
    return create_identity(name="Usage owner", workspace_name="Usage workspace")


@pytest.fixture
def owner_client(tenant: Identity) -> TestClient:
    return authenticate(TestClient(app, base_url=TEST_BASE_URL), tenant)


# --------------------------------------------------------------------------
# Parsing what a provider sends back


def test_responses_usage_is_parsed_with_its_detail_objects():
    counts = usage_service.token_counts(responses_usage())
    assert counts is not None
    assert counts.input_tokens == 1000
    assert counts.cached_input_tokens == 400
    assert counts.output_tokens == 200
    assert counts.reasoning_tokens == 150
    assert counts.total_tokens == 1200
    # The uncached remainder is what gets billed at the full input rate.
    assert counts.uncached_input_tokens == 600


def test_embedding_usage_is_parsed_from_the_other_shape():
    """The embeddings API reports prompt/total and has no completion half.

    Easy to forget precisely because it looks like nothing: no prompt anyone
    typed, no answer anyone read, and one call per chunk of every document ever
    uploaded.
    """
    counts = usage_service.token_counts(
        SimpleNamespace(prompt_tokens=812, total_tokens=812)
    )
    assert counts is not None
    assert counts.input_tokens == 812
    assert counts.output_tokens == 0
    assert counts.total_tokens == 812


def test_a_dict_shaped_usage_object_is_read_the_same_way():
    counts = usage_service.token_counts(
        {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12}
    )
    assert counts is not None and counts.total_tokens == 12


def test_missing_usage_records_nothing_rather_than_a_row_of_zeros():
    """The scripted provider's path, and any SDK object without the field.

    A zero row would claim a measurement that was never taken, which is worse
    than the gap: it is indistinguishable from a call that genuinely cost nothing.
    """
    assert usage_service.token_counts(None) is None
    assert usage_service.token_counts(SimpleNamespace()) is None
    assert usage_service.token_counts(SimpleNamespace(input_tokens=0)) is None


def test_malformed_counts_degrade_to_the_fields_that_parsed():
    """Every value here crossed a network from a provider free to change shape."""
    counts = usage_service.token_counts(
        SimpleNamespace(
            input_tokens="not a number",
            output_tokens=None,
            total_tokens=99,
            input_tokens_details=None,
            output_tokens_details=SimpleNamespace(reasoning_tokens=-5),
        )
    )
    assert counts is not None
    assert counts.input_tokens == 0
    assert counts.output_tokens == 0
    assert counts.reasoning_tokens == 0
    assert counts.total_tokens == 99


def test_cached_tokens_cannot_exceed_the_input_they_are_a_subset_of():
    counts = usage_service.token_counts(
        SimpleNamespace(
            input_tokens=100,
            output_tokens=0,
            total_tokens=100,
            input_tokens_details=SimpleNamespace(cached_tokens=999),
        )
    )
    assert counts is not None
    assert counts.cached_input_tokens == 100
    # And therefore never a negative amount billed at the full rate.
    assert counts.uncached_input_tokens == 0


# --------------------------------------------------------------------------
# Costing


def test_cost_bills_cached_input_at_its_own_rate():
    counts = usage_service.token_counts(responses_usage())
    assert counts is not None
    cost, rate = usage_service.compute_cost(counts, PRICE)
    # 600 uncached @ 1.0 + 400 cached @ 0.1 + 200 output @ 10.0, per million.
    assert cost == pytest.approx((600 * 1.0 + 400 * 0.1 + 200 * 10.0) / 1_000_000)
    assert rate is PRICE


def test_reasoning_tokens_are_not_charged_twice():
    """They are already inside output_tokens; adding them would inflate every
    reasoning-heavy turn by however much the model happened to think."""
    with_reasoning = usage_service.token_counts(responses_usage(reasoning=150))
    without = usage_service.token_counts(responses_usage(reasoning=0))
    assert with_reasoning is not None and without is not None
    assert usage_service.compute_cost(with_reasoning, PRICE) == (
        usage_service.compute_cost(without, PRICE)
    )


def test_an_unconfigured_model_costs_null_not_zero():
    counts = usage_service.token_counts(responses_usage())
    assert counts is not None
    assert usage_service.compute_cost(counts, None) == (None, None)


def test_a_missing_cached_rate_falls_back_to_the_full_input_rate():
    """The conservative direction: a discount we do not know about makes the
    recorded cost too high, never too low."""
    price = ModelPrice(input=2.0, output=0.0)
    assert price.effective_cached_input == 2.0


def test_prices_are_configuration_not_code(monkeypatch):
    """A price change has to be an env change, not a deploy."""
    monkeypatch.setenv(
        "MODEL_PRICES", json.dumps({"gpt-x": {"input": 3.5, "output": 14.0}})
    )
    settings = Settings(_env_file=None)
    price = settings.price_for("gpt-x")
    assert price is not None and price.input == 3.5 and price.output == 14.0
    assert settings.price_for("gpt-x-2026-01-01") is None, "no prefix matching"
    assert settings.price_for("something-else") is None


# --------------------------------------------------------------------------
# Attribution


def test_scopes_nest_and_inherit_rather_than_overwrite():
    """The property that lets an outer and an inner binder coexist: the inner one
    only knows the workspace, and the run it was called under survives."""
    with usage_service.usage_scope(
        workspace_id="ws", run_id="run", conversation_id="conv", user_id="user"
    ):
        with usage_service.usage_scope(workspace_id="ws"):
            inner = usage_service.current_attribution()
            assert inner.run_id == "run"
            assert inner.conversation_id == "conv"
            assert inner.user_id == "user"


def test_a_scope_is_reset_even_when_the_body_raises():
    """A scope left bound bills the next task on this thread to the wrong tenant,
    which is a worse failure than a missing row."""
    with pytest.raises(RuntimeError):
        with usage_service.usage_scope(workspace_id="ws-leak"):
            raise RuntimeError("boom")
    assert usage_service.current_attribution().workspace_id == ""


def test_a_workflow_run_is_billed_as_unattended_work():
    assert _billing_operation(WORKFLOW_SCOPE) == usage_service.WORKFLOW_NODE
    assert _billing_operation(CHAT_SCOPE) == usage_service.CHAT


# --------------------------------------------------------------------------
# Recording


def test_a_priced_call_records_tokens_cost_and_the_rate_that_produced_it(
    tenant: Identity,
):
    with usage_service.usage_scope(
        workspace_id=tenant.workspace_id,
        run_id="run-1",
        conversation_id="conv-1",
        user_id=tenant.user_id,
    ):
        row_id = usage_service.record_model_usage(
            model=PRICED_MODEL,
            operation=usage_service.CHAT,
            usage=responses_usage(),
            settings=priced_settings(),
        )
    assert row_id is not None
    rows = rows_for(tenant.workspace_id)
    assert len(rows) == 1
    row = rows[0]
    assert (row.run_id, row.conversation_id, row.user_id) == (
        "run-1",
        "conv-1",
        tenant.user_id,
    )
    assert row.operation == usage_service.CHAT
    assert (row.input_tokens, row.output_tokens) == (1000, 200)
    assert (row.cached_input_tokens, row.reasoning_tokens) == (400, 150)
    # 600 uncached @ 1.0 + 400 cached @ 0.1 + 200 output @ 10.0, per million.
    assert row.cost_usd == pytest.approx(0.00264)
    assert row.input_rate_usd_per_mtok == 1.0
    assert row.cached_input_rate_usd_per_mtok == 0.1
    assert row.output_rate_usd_per_mtok == 10.0


def test_a_stored_row_does_not_reprice_when_the_rate_changes(tenant: Identity):
    """The whole reason the rate is a column and not a lookup."""
    with usage_service.usage_scope(workspace_id=tenant.workspace_id):
        usage_service.record_model_usage(
            model=PRICED_MODEL,
            operation=usage_service.CHAT,
            usage=responses_usage(),
            settings=priced_settings(),
        )
    before = rows_for(tenant.workspace_id)[0]
    original_cost, original_rate = before.cost_usd, before.input_rate_usd_per_mtok

    dearer = Settings(
        _env_file=None,
        app_env="test",
        model_provider="scripted",
        scripted_model_script="apps/api/tests/scripts/agent.json",
        model_prices={PRICED_MODEL: ModelPrice(input=100.0, output=1000.0)},
    )
    with usage_service.usage_scope(workspace_id=tenant.workspace_id):
        usage_service.record_model_usage(
            model=PRICED_MODEL,
            operation=usage_service.CHAT,
            usage=responses_usage(),
            settings=dearer,
        )
    rows = rows_for(tenant.workspace_id)
    assert len(rows) == 2
    assert rows[0].cost_usd == original_cost
    assert rows[0].input_rate_usd_per_mtok == original_rate
    assert rows[1].cost_usd > original_cost


def test_an_unpriced_model_records_tokens_and_a_null_cost(tenant: Identity):
    with usage_service.usage_scope(workspace_id=tenant.workspace_id):
        usage_service.record_model_usage(
            model=UNPRICED_MODEL,
            operation=usage_service.EMBEDDING,
            usage=SimpleNamespace(prompt_tokens=512, total_tokens=512),
            settings=priced_settings(),
        )
    row = rows_for(tenant.workspace_id)[0]
    assert row.total_tokens == 512
    assert row.cost_usd is None
    assert row.input_rate_usd_per_mtok is None


def test_an_unattributed_call_is_dropped_rather_than_written_to_nobody(tenant):
    """Everything in this schema is workspace-scoped, so a tenant-less row could
    be neither shown nor scoped. The warning is the signal that a new call site
    forgot its usage_scope."""
    assert usage_service.current_attribution().workspace_id == ""
    assert (
        usage_service.record_model_usage(
            model=PRICED_MODEL,
            operation=usage_service.CHAT,
            usage=responses_usage(),
            settings=priced_settings(),
        )
        is None
    )
    assert rows_for(tenant.workspace_id) == []


def test_a_failing_ledger_write_does_not_reach_the_caller(tenant, monkeypatch):
    """The constraint that outranks the feature: a run must not fail because it
    could not bill itself."""

    def explode():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(usage_service, "SessionLocal", explode)
    with usage_service.usage_scope(workspace_id=tenant.workspace_id):
        assert (
            usage_service.record_model_usage(
                model=PRICED_MODEL,
                operation=usage_service.CHAT,
                usage=responses_usage(),
                settings=priced_settings(),
            )
            is None
        )


# --------------------------------------------------------------------------
# The chokepoints


def test_every_non_streaming_responses_call_is_billed_by_one_helper(tenant):
    """`call_responses` is the reason a sixth caller cannot skip accounting."""

    class FakeResponses:
        def create(self, **kwargs):
            return SimpleNamespace(output_text="ok", usage=responses_usage())

    with usage_service.usage_scope(workspace_id=tenant.workspace_id):
        model_service.call_responses(
            SimpleNamespace(responses=FakeResponses()),
            operation=usage_service.CODEGEN,
            settings=priced_settings(),
            model=PRICED_MODEL,
            input="anything",
        )
    rows = rows_for(tenant.workspace_id)
    assert len(rows) == 1
    assert rows[0].operation == usage_service.CODEGEN
    assert rows[0].model == PRICED_MODEL
    assert rows[0].input_tokens == 1000


def test_a_streamed_turn_is_billed_from_its_terminal_response(tenant):
    settings = priced_settings(openai_model=PRICED_MODEL)

    class FakeStream:
        def __iter__(self):
            return iter(
                [
                    SimpleNamespace(type="response.output_text.delta", delta="hi"),
                    SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(
                            output=[], output_text="hi", usage=responses_usage()
                        ),
                    ),
                ]
            )

    class FakeResponses:
        def create(self, **kwargs):
            return FakeStream()

    # No `operation` argument: an agent turn is billed by what *started* the run,
    # which the scope carries and the step function reaching this call does not.
    # It is why `_default_model_step` — the seam every test double replaces —
    # needed no new parameter for unattended work to be distinguishable.
    with usage_service.usage_scope(
        workspace_id=tenant.workspace_id,
        run_id="run-stream",
        operation=usage_service.WORKFLOW_NODE,
    ):
        events = list(
            model_service.stream_agent_response(
                SimpleNamespace(responses=FakeResponses()),
                settings,
                user_id=tenant.user_id,
                input_items=[],
                tools=[],
                instructions="x",
            )
        )
    assert events[-1][0] == "completed"
    rows = rows_for(tenant.workspace_id)
    assert len(rows) == 1
    assert rows[0].operation == usage_service.WORKFLOW_NODE
    assert rows[0].run_id == "run-stream"


def test_a_turn_with_no_operation_anywhere_falls_back_to_chat(tenant):
    class FakeStream:
        def __iter__(self):
            return iter(
                [
                    SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(
                            output=[], output_text="hi", usage=responses_usage()
                        ),
                    )
                ]
            )

    with usage_service.usage_scope(workspace_id=tenant.workspace_id):
        list(
            model_service.stream_agent_response(
                SimpleNamespace(responses=SimpleNamespace(create=lambda **k: FakeStream())),
                priced_settings(openai_model=PRICED_MODEL),
                user_id=tenant.user_id,
                input_items=[],
                tools=[],
                instructions="x",
            )
        )
    assert rows_for(tenant.workspace_id)[0].operation == usage_service.CHAT


def test_a_stream_without_usage_bills_nothing_and_still_answers(tenant):
    """The scripted-provider shape: a terminal response carrying no usage at all."""

    class FakeStream:
        def __iter__(self):
            return iter(
                [
                    SimpleNamespace(
                        type="response.completed",
                        response=SimpleNamespace(output=[], output_text="hi"),
                    )
                ]
            )

    class FakeResponses:
        def create(self, **kwargs):
            return FakeStream()

    with usage_service.usage_scope(workspace_id=tenant.workspace_id):
        events = list(
            model_service.stream_agent_response(
                SimpleNamespace(responses=FakeResponses()),
                priced_settings(),
                user_id=tenant.user_id,
                input_items=[],
                tools=[],
                instructions="x",
            )
        )
    assert events[-1][0] == "completed"
    assert rows_for(tenant.workspace_id) == []


def test_embeddings_are_billed_at_their_own_chokepoint(tenant, monkeypatch):
    """The spend everyone forgets: one call per chunk of every upload."""
    import openai

    class FakeEmbeddings:
        def create(self, **kwargs):
            return SimpleNamespace(
                data=[SimpleNamespace(index=0, embedding=[0.1, 0.2, 0.3])],
                usage=SimpleNamespace(prompt_tokens=64, total_tokens=64),
            )

    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda **kwargs: SimpleNamespace(embeddings=FakeEmbeddings()),
    )
    settings = Settings(
        _env_file=None,
        app_env="test",
        model_provider="openai",
        openai_api_key="test-key",
        openai_embedding_model=PRICED_MODEL,
        model_prices={PRICED_MODEL: PRICE},
    )
    with usage_service.usage_scope(workspace_id=tenant.workspace_id):
        vectors = embeddings_service.embed_texts(["a passage"], settings)
    assert vectors is not None and len(vectors) == 1
    rows = rows_for(tenant.workspace_id)
    assert len(rows) == 1
    assert rows[0].operation == usage_service.EMBEDDING
    assert rows[0].model == PRICED_MODEL
    assert rows[0].total_tokens == 64
    assert rows[0].cost_usd == pytest.approx(64 / 1_000_000)


# --------------------------------------------------------------------------
# The table holds no content


def test_the_ledger_has_no_column_that_could_hold_a_prompt():
    """Pinned rather than described. This table outlives the conversations it
    describes and is read by an admin panel, so a "preview" column would be a
    second copy of everyone's messages with none of the rules guarding the first.
    """
    assert {column.name for column in ModelUsage.__table__.columns} == {
        "id",
        "workspace_id",
        "run_id",
        "conversation_id",
        "user_id",
        "operation",
        "provider",
        "model",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "cost_usd",
        "input_rate_usd_per_mtok",
        "cached_input_rate_usd_per_mtok",
        "output_rate_usd_per_mtok",
        "created_at",
    }


# --------------------------------------------------------------------------
# The admin route


def plant_usage(workspace_id: str, user_id: str, **overrides: object) -> ModelUsage:
    defaults: dict[str, object] = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "run_id": "",
        "conversation_id": "",
        "operation": usage_service.CHAT,
        "model": PRICED_MODEL,
        "input_tokens": 1000,
        "cached_input_tokens": 100,
        "output_tokens": 100,
        "reasoning_tokens": 40,
        "total_tokens": 1100,
        "cost_usd": 0.002,
        "input_rate_usd_per_mtok": 1.0,
        "output_rate_usd_per_mtok": 10.0,
    }
    defaults.update(overrides)
    db = SessionLocal()
    try:
        row = ModelUsage(**defaults)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def test_usage_reports_totals_and_every_breakdown(
    tenant: Identity, owner_client: TestClient
):
    plant_usage(tenant.workspace_id, tenant.user_id, run_id="run-a", cost_usd=0.002)
    plant_usage(tenant.workspace_id, tenant.user_id, run_id="run-b", cost_usd=0.008)
    plant_usage(
        tenant.workspace_id,
        tenant.user_id,
        operation=usage_service.EMBEDDING,
        model=UNPRICED_MODEL,
        cost_usd=None,
        input_rate_usd_per_mtok=None,
        output_rate_usd_per_mtok=None,
    )

    body = owner_client.get("/api/admin/usage").json()
    assert body["window_days"] == 30
    totals = body["totals"]
    assert totals["calls"] == 3
    assert totals["total_tokens"] == 3300
    assert totals["cached_input_tokens"] == 300
    assert totals["reasoning_tokens"] == 120
    # Summed over priced rows only, and the unpriced remainder is stated rather
    # than folded in as zero.
    assert totals["cost_usd"] == pytest.approx(0.010)
    assert (totals["priced_calls"], totals["unpriced_calls"]) == (2, 1)

    models = {row["key"]: row for row in body["by_model"]}
    assert set(models) == {PRICED_MODEL, UNPRICED_MODEL}
    assert models[UNPRICED_MODEL]["unpriced_calls"] == 1
    assert models[UNPRICED_MODEL]["cost_usd"] == 0.0

    operations = {row["key"] for row in body["by_operation"]}
    assert operations == {usage_service.CHAT, usage_service.EMBEDDING}

    users = body["by_user"]
    assert [row["key"] for row in users] == [tenant.user_id]
    # Resolved to a person, so the panel does not have to join ids itself.
    assert users[0]["label"] == "Usage owner"

    # Top runs, costliest first, and the row with no run (the embedding) is not
    # invented as a run of its own.
    assert [row["run_id"] for row in body["top_runs"]] == ["run-b", "run-a"]
    assert body["top_runs"][0]["cost_usd"] == pytest.approx(0.008)

    assert body["unpriced_models"] == [UNPRICED_MODEL]


def test_usage_is_bounded_by_its_window(tenant: Identity, owner_client: TestClient):
    """The fastest-growing table in the schema; an all-time aggregate would get
    slower every day someone looked at it."""
    plant_usage(tenant.workspace_id, tenant.user_id, run_id="recent")
    old = plant_usage(tenant.workspace_id, tenant.user_id, run_id="ancient")
    db = SessionLocal()
    try:
        row = db.get(ModelUsage, old.id)
        assert row is not None
        row.created_at = utcnow() - timedelta(days=45)
        db.commit()
    finally:
        db.close()

    default_window = owner_client.get("/api/admin/usage").json()
    assert default_window["totals"]["calls"] == 1
    assert [run["run_id"] for run in default_window["top_runs"]] == ["recent"]

    wide = owner_client.get("/api/admin/usage", params={"days": 90}).json()
    assert wide["totals"]["calls"] == 2

    assert owner_client.get("/api/admin/usage", params={"days": 0}).status_code == 422
    assert owner_client.get("/api/admin/usage", params={"days": 400}).status_code == 422


def test_usage_says_when_no_rate_is_configured_at_all(
    tenant: Identity, owner_client: TestClient
):
    """So a panel can say "not configured" instead of a confident $0.00."""
    body = owner_client.get("/api/admin/usage").json()
    # The suite runs with MODEL_PRICES unset, which is also the shipped default.
    assert body["pricing_configured"] is False


def test_usage_from_another_workspace_never_appears(tenant: Identity, owner_client):
    """A breakdown leaks values, not only ids: the stranger's model name and user
    id are both scanned for."""
    stranger = create_identity(name="Stranger owner", workspace_name="Stranger space")
    plant_usage(
        stranger.workspace_id,
        stranger.user_id,
        run_id="stranger-run",
        model="stranger-secret-model",
    )
    plant_usage(tenant.workspace_id, tenant.user_id, run_id="own-run")

    body = owner_client.get("/api/admin/usage")
    assert body.status_code == 200
    for marker in (
        stranger.workspace_id,
        stranger.user_id,
        "stranger-run",
        "stranger-secret-model",
    ):
        assert marker not in body.text
    assert body.json()["totals"]["calls"] == 1


def test_usage_refuses_a_member_and_an_anonymous_caller(
    tenant: Identity, anonymous_client
):
    """Covered by the router-wide sweeps in test_admin.py too; pinned here so the
    money route is not the one that quietly loses its gate."""
    from conftest import issue_session

    from app.models import Membership, User

    db = SessionLocal()
    try:
        member = User(email=f"{os.urandom(6).hex()}@example.com", name="Plain member")
        db.add(member)
        db.flush()
        db.add(
            Membership(
                workspace_id=tenant.workspace_id, user_id=member.id, role="member"
            )
        )
        db.commit()
        member_id = member.id
    finally:
        db.close()
    token, csrf = issue_session(member_id)
    member_client = authenticate(
        TestClient(app, base_url=TEST_BASE_URL),
        Identity(
            user_id=member_id,
            workspace_id=tenant.workspace_id,
            token=token,
            csrf_token=csrf,
        ),
    )
    assert member_client.get("/api/admin/usage").status_code == 403
    assert anonymous_client.get("/api/admin/usage").status_code == 401
