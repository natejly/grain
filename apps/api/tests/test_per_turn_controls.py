from __future__ import annotations

from types import SimpleNamespace

from pydantic import SecretStr

from app.config import ReasoningEffort, Settings, get_settings
from app.database import SessionLocal
from app.models import Run
from app.services import model
from app.services.harness.openai import OpenAIHarness
from app.services.model import _openai_input, stream_agent_response
from app.services.retrieval import Evidence

# --- Unit level: the per-turn override reaches the provider request ----------
#
# These prove the bottom of the chain (`stream_agent_response`) and the seam
# above it (`OpenAIHarness.build_step`). The endpoint tests below prove the top.
# Together they show a value chosen in the composer survives every hop unchanged,
# and — critically — that *omitting* it reproduces today's request byte-for-byte.


class _FakeStream:
    """One agent turn's worth of Responses API stream events."""

    def __init__(self, text: str):
        self._events = [
            SimpleNamespace(type="response.output_text.delta", delta=text),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(output=[], output_text=text),
            ),
        ]

    def __iter__(self):
        return iter(self._events)


def _openai_settings() -> Settings:
    return Settings(
        _env_file=None,
        model_provider="openai",
        openai_api_key=SecretStr("test-key"),
        openai_model="deployment-default",
        openai_reasoning_effort="low",
    )


def _evidence() -> list[Evidence]:
    return [
        Evidence(
            chunk_id="chunk-1",
            source_id="source-1",
            filename="brief.md",
            ordinal=0,
            excerpt="Maya owns the October launch.",
            score=1.0,
        )
    ]


def _run_stream(settings: Settings, **overrides) -> dict:
    """Drive one turn through a capturing client; return the request kwargs."""
    captured: dict[str, object] = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _FakeStream("Maya owns the launch. [1]")

    list(
        stream_agent_response(
            SimpleNamespace(responses=FakeResponses()),  # type: ignore[arg-type]
            settings,
            user_id="user-1",
            input_items=[
                {"role": "user", "content": _openai_input("Who owns it?", _evidence())}
            ],
            tools=[],
            instructions=model.CHAT_INSTRUCTIONS,
            **overrides,
        )
    )
    return captured


def test_override_changes_the_model_and_effort_the_request_uses():
    """A per-turn override is what the provider is actually asked to run.

    The whole feature is worthless if the chosen model and effort do not reach
    `responses.create`; this pins the two request fields the composer controls to
    the override, not the deployment default they would otherwise carry.
    """
    captured = _run_stream(
        _openai_settings(), model="override-model", effort="high"
    )
    assert captured["model"] == "override-model"
    assert captured["reasoning"] == {"effort": "high"}


def test_absent_override_preserves_the_deployment_defaults():
    """No override must be byte-identical to before the feature existed.

    `model=None`/`effort=None` is the "unset" the whole chain funnels toward, so a
    client that never sends the fields has to fall straight back to
    `settings.openai_model` / `settings.openai_reasoning_effort`.
    """
    captured = _run_stream(_openai_settings())
    assert captured["model"] == "deployment-default"
    assert captured["reasoning"] == {"effort": "low"}


def test_openai_harness_forwards_the_per_turn_overrides(monkeypatch):
    """`build_step` is the seam the loop uses; the overrides must ride through it.

    `agent_loop` hands the run's overrides to `build_step`, which is the only
    caller a real turn takes — every other test reaches `stream_agent_response`
    directly and would miss a harness that dropped either argument.
    """
    from app.services.harness import openai as openai_harness

    captured: dict = {}

    def spy(client, settings, *, user_id, input_items, tools, instructions,
            model=None, effort=None, operation=""):
        captured.update(model=model, effort=effort)
        yield ("completed", object())

    monkeypatch.setattr(openai_harness, "_openai_client", lambda settings: "CLIENT")
    monkeypatch.setattr(openai_harness, "stream_agent_response", spy)

    list(OpenAIHarness().build_step(
        _openai_settings(), prompt="p", user_id="alice", evidence=[],
        model="override-model", effort="high",
    )([], [], ""))
    assert captured == {"model": "override-model", "effort": "high"}

    # ...and with nothing chosen, None flows through so the defaults win downstream.
    captured.clear()
    list(OpenAIHarness().build_step(
        _openai_settings(), prompt="p", user_id="alice", evidence=[]
    )([], [], ""))
    assert captured == {"model": None, "effort": None}


# --- Endpoint level: the request contract and the bootstrap exposure ---------


def _conversation(client, suffix: str) -> str:
    return client.post(
        "/api/conversations",
        headers={"Idempotency-Key": f"per-turn-conv-{suffix}"},
        json={"title": "Per-turn"},
    ).json()["id"]


def _send(client, conversation_id: str, suffix: str, **body):
    return client.post(
        f"/api/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": f"per-turn-msg-{suffix}"},
        json={"content": "Who owns the launch?", **body},
    )


def _run(run_id: str) -> Run:
    db = SessionLocal()
    try:
        return db.get(Run, run_id)
    finally:
        db.close()


def test_bootstrap_exposes_the_selectable_models_and_effort_ladder(client):
    """The composer's dropdowns are seeded from bootstrap, so the allow-list and
    the ladder have to appear there — a client cannot honour a list it was never
    told. The scripted double talks to no provider, so its only selectable
    "model" is itself; the ladder is the full `ReasoningEffort` and the pre-select
    is the deployment default, so an untouched composer sends today's request.
    """
    from typing import get_args

    provider = client.get("/api/bootstrap").json()["model_provider"]
    assert provider["selectable_models"] == ["scripted-double"]
    assert provider["reasoning_efforts"] == list(get_args(ReasoningEffort))
    assert provider["default_effort"] == get_settings().openai_reasoning_effort


def test_effort_off_the_ladder_is_refused(client):
    """An effort the model would reject must fail at the edge, not on the turn.

    `effort` is a `ReasoningEffort` Literal on the request, so pydantic refuses an
    off-ladder value as a 422 with no endpoint code — the same wall that keeps the
    setting itself honest.
    """
    conversation = _conversation(client, "bad-effort")
    response = _send(client, conversation, "bad-effort", effort="ludicrous")
    assert response.status_code == 422


def test_model_off_the_allow_list_is_refused(client):
    """A model the deployment never vouched for must never reach the provider.

    An arbitrary string would record a null cost or 500 the turn; the endpoint
    checks it against `settings.selectable_models` and answers 422, not 202.
    """
    conversation = _conversation(client, "bad-model")
    response = _send(client, conversation, "bad-model", model="totally-made-up-model")
    assert response.status_code == 422
    assert response.json()["detail"] == "Model is not selectable"


def test_absent_overrides_leave_the_run_on_the_deployment_defaults(client):
    """No override persists as "", the unset convention that resolves to defaults.

    This is the byte-for-byte guarantee at the persistence layer: a message with
    no per-turn fields stores empty strings, which `stream_agent_response` reads
    as "use the deployment model and effort".
    """
    conversation = _conversation(client, "no-override")
    response = _send(client, conversation, "no-override")
    assert response.status_code == 202
    run = _run(response.json()["run"]["id"])
    assert run.requested_model == ""
    assert run.requested_effort == ""


def test_overrides_are_persisted_on_the_run(client):
    """A chosen model and effort are recorded on the run the turn reads off.

    `process_run` re-opens a fresh session and reads the overrides off the row, so
    a choice that did not survive to the `Run` columns would be silently lost. The
    model is taken from the live allow-list so the test tracks whatever the
    deployment actually prices.
    """
    model_name = get_settings().selectable_models[0]
    conversation = _conversation(client, "override")
    response = _send(
        client, conversation, "override", model=model_name, effort="high"
    )
    assert response.status_code == 202
    run = _run(response.json()["run"]["id"])
    assert run.requested_model == model_name
    assert run.requested_effort == "high"


def test_fast_maps_to_low_and_an_explicit_effort_wins(client):
    """`fast` is an honest shortcut to "low", and never overrides a real choice.

    It maps to "low" (not "none", which the small models reject), so the toggle
    cannot promise a setting a model refuses; and when a turn sends both, the
    explicit `effort` is what the run records — `fast` only fills the gap.
    """
    conversation = _conversation(client, "fast-only")
    fast_only = _send(client, conversation, "fast-only", fast=True)
    assert _run(fast_only.json()["run"]["id"]).requested_effort == "low"

    conversation = _conversation(client, "fast-and-effort")
    both = _send(client, conversation, "fast-and-effort", fast=True, effort="high")
    assert _run(both.json()["run"]["id"]).requested_effort == "high"
