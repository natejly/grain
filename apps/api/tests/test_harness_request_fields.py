"""The three request fields the harness seam exists to carry.

Each one is a provider field nothing above the harness can see, so each is
pinned at the layer that actually builds the request rather than at a call site
that merely forwards an argument:

* ``prompt_cache_key`` — per workspace, which is why `build_step` had to grow
  an identity at all.
* ``include=["reasoning.encrypted_content"]`` — the other half of ``store=False``.
* ``tool_choice="none"`` on the final round, with the tools array intact.

These are pure request-shape tests: the OpenAI client and the Anthropic client
are both replaced with recorders, so nothing here touches a network or a key.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.services.harness import anthropic as anthropic_harness
from app.services.harness import openai as openai_harness
from app.services.harness.anthropic import AnthropicHarness
from app.services.harness.openai import OpenAIHarness
from app.services.model import privacy_safe_identifier

WORKSPACE = "ws-cache-key"


def _openai_settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        model_provider="openai",
        openai_api_key=SecretStr("test-key"),
        **overrides,
    )


def _anthropic_settings() -> Settings:
    return Settings(
        _env_file=None,
        model_provider="anthropic",
        anthropic_api_key=SecretStr("test-key"),
    )


class _Completed:
    type = "response.completed"
    response = None


class _RecordingResponses:
    """Captures the kwargs of one `responses.create` and streams nothing."""

    def __init__(self, seen: Dict[str, Any]) -> None:
        self._seen = seen

    def create(self, **kwargs: Any) -> Any:
        self._seen.update(kwargs)
        return iter([_Completed()])


class _RecordingClient:
    def __init__(self, seen: Dict[str, Any]) -> None:
        self.responses = _RecordingResponses(seen)


def _drive(
    settings: Settings,
    *,
    tools: List[Dict[str, Any]] | None = None,
    call: Dict[str, Any] | None = None,
    **build: Any,
) -> Dict[str, Any]:
    """One OpenAI round against a recorder, returning the request's kwargs.

    The patch target is `harness.openai._openai_client`, NOT the definition in
    `services.model`: the harness imported the name at module load, so patching
    the source module leaves the bound name alone and the request goes to the
    real provider. That is not a hypothetical — it is what this file did on its
    first run.
    """
    seen: Dict[str, Any] = {}
    original = openai_harness._openai_client
    try:
        openai_harness._openai_client = lambda _settings: _RecordingClient(seen)
        step = OpenAIHarness().build_step(
            settings, prompt="p", user_id="alice", evidence=[], **build
        )
        list(
            step(
                [{"role": "user"}],
                tools if tools is not None else [{"type": "function", "name": "t"}],
                "instr",
                **(call or {}),
            )
        )
    finally:
        openai_harness._openai_client = original
    assert seen, "the recorder must have seen the request, not the network"
    return seen


# --- prompt_cache_key --------------------------------------------------------


def test_the_cache_key_is_the_workspace_not_the_run():
    """One key per workspace. A per-run key would never hit: every run is new.

    Hashed with the same derivation `safety_identifier` uses — cache routing
    needs a STABLE key, not a meaningful one, and the raw id has no reason to
    leave this deployment.
    """
    seen = _drive(_openai_settings(), workspace_id=WORKSPACE, run_id="run-1")
    assert seen["prompt_cache_key"] == privacy_safe_identifier(WORKSPACE)
    assert WORKSPACE not in seen["prompt_cache_key"]

    other = _drive(_openai_settings(), workspace_id=WORKSPACE, run_id="run-2")
    assert other["prompt_cache_key"] == seen["prompt_cache_key"]


def test_two_workspaces_land_on_two_keys():
    first = _drive(_openai_settings(), workspace_id="ws-a")
    second = _drive(_openai_settings(), workspace_id="ws-b")
    assert first["prompt_cache_key"] != second["prompt_cache_key"]


def test_an_unidentified_turn_sends_no_key_at_all():
    """Omitted, never sent empty: "" would be one shared shard for every turn
    whose caller did not know its workspace."""
    seen = _drive(_openai_settings())
    assert "prompt_cache_key" not in seen


# --- encrypted reasoning replay ---------------------------------------------


def test_reasoning_is_requested_encrypted_alongside_store_false():
    """The pair is the point. `store=False` leaves the provider no state to
    look a reasoning item up in, so without `include` the model re-derives its
    chain of thought after every tool round."""
    seen = _drive(_openai_settings(), workspace_id=WORKSPACE)
    assert seen["store"] is False
    assert seen["include"] == ["reasoning.encrypted_content"]


def test_the_replay_knob_removes_the_field_rather_than_blanking_it():
    seen = _drive(_openai_settings(openai_reasoning_replay=False))
    assert "include" not in seen


# --- tool_choice -------------------------------------------------------------


def test_an_ordinary_round_states_no_tool_choice():
    """"auto" is the provider default, so stating it would change the request
    prefix for every round — which is the cost this feature exists to avoid."""
    seen = _drive(_openai_settings())
    assert "tool_choice" not in seen


def test_the_final_round_forbids_calls_and_keeps_the_tools():
    seen = _drive(
        _openai_settings(),
        tools=[{"type": "function", "name": "search_sources"}],
        call={"tool_choice": "none"},
    )
    assert seen["tool_choice"] == "none"
    # The whole trade: the tools array is still there, so the cached prefix is
    # unchanged. `tools=[]` would have been a different request.
    assert seen["tools"] == [{"type": "function", "name": "search_sources"}]


# --- the Anthropic path ------------------------------------------------------


class _FakeStream:
    def __init__(self) -> None:
        self._message = type(
            "Message", (), {"content": [], "usage": None}
        )()

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self) -> Any:
        return iter(())

    def get_final_message(self) -> Any:
        return self._message


class _RecordingMessages:
    def __init__(self, seen: Dict[str, Any]) -> None:
        self._seen = seen

    def stream(self, **kwargs: Any) -> _FakeStream:
        self._seen.update(kwargs)
        return _FakeStream()


class _RecordingAnthropic:
    def __init__(self, seen: Dict[str, Any]) -> None:
        self.messages = _RecordingMessages(seen)


def _drive_anthropic(
    tools: List[Dict[str, Any]], **call: Any
) -> Dict[str, Any]:
    seen: Dict[str, Any] = {}
    original = anthropic_harness._client
    try:
        anthropic_harness._client = lambda _settings: _RecordingAnthropic(seen)
        step = AnthropicHarness().build_step(
            _anthropic_settings(),
            prompt="p",
            user_id="alice",
            evidence=[],
            workspace_id=WORKSPACE,
        )
        list(step([{"role": "user", "content": "hi"}], tools, "instr", **call))
    finally:
        anthropic_harness._client = original
    return seen


def test_anthropic_translates_tool_choice_into_its_own_object():
    seen = _drive_anthropic(
        [{"type": "function", "name": "search_sources", "parameters": {}}],
        tool_choice="none",
    )
    assert seen["tool_choice"] == {"type": "none"}
    assert [tool["name"] for tool in seen["tools"]] == ["search_sources"]


def test_anthropic_states_nothing_on_an_ordinary_round():
    seen = _drive_anthropic(
        [{"type": "function", "name": "search_sources", "parameters": {}}]
    )
    assert "tool_choice" not in seen


def test_anthropic_never_sends_a_tool_choice_with_no_tools():
    """The Messages API rejects it, and a turn whose registry narrowed to
    nothing is exactly the turn that would hit that."""
    seen = _drive_anthropic([], tool_choice="none")
    assert "tool_choice" not in seen
    assert "tools" not in seen


def test_anthropic_carries_no_workspace_identity_into_the_request():
    """Accepted and deliberately unused: Anthropic caches on `cache_control`
    breakpoints, not on a routing key, so there is no field for it to fill —
    and inventing a metadata entry would export a tenant id to buy nothing."""
    seen = _drive_anthropic([])
    assert seen["metadata"] == {"user_id": privacy_safe_identifier("alice")}
    assert WORKSPACE not in repr(seen)


@pytest.mark.parametrize("field", ["prompt_cache_key", "include"])
def test_the_openai_only_fields_never_reach_anthropic(field: str):
    seen = _drive_anthropic([])
    assert field not in seen
