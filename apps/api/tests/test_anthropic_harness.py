"""The Anthropic harness, driven entirely offline.

Every test fakes the SDK client at the `_client` seam — the module-level
constructor exists precisely so these tests never open a socket. What is under
test is the translation layer in both directions: loop-dialect history and
tools going in, Responses-shaped output items and usage accounting coming out.
The fakes mirror only the SDK surface the harness actually touches (a context
manager stream that yields typed events and answers `get_final_message`), so a
drifting fake fails these tests rather than silently passing them.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from app.config import Settings
from app.services import usage
from app.services.harness import anthropic as harness_anthropic
from app.services.harness.anthropic import AnthropicHarness, _shim_usage
from app.services.usage import token_counts


class _FakeStream:
    """The slice of `MessageStream` the harness uses: iterate, then final."""

    def __init__(self, events: List[Any], final: Any) -> None:
        self._events = events
        self._final = final

    def __enter__(self) -> _FakeStream:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __iter__(self) -> Iterator[Any]:
        return iter(self._events)

    def get_final_message(self) -> Any:
        return self._final


class _FakeClient:
    """Records the kwargs of the one `messages.stream(...)` call it serves."""

    def __init__(self, stream: _FakeStream) -> None:
        self.stream_kwargs: Optional[Dict[str, Any]] = None
        client = self

        class _Messages:
            def stream(self, **kwargs: Any) -> _FakeStream:
                client.stream_kwargs = kwargs
                return stream

        self.messages = _Messages()


def _settings() -> Settings:
    return Settings(
        model_provider="anthropic",
        anthropic_api_key="test-anthropic-key",
        anthropic_model="claude-sonnet-5",
        anthropic_max_output_tokens=555,
    )


def _final_message() -> Any:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="Hello"),
            SimpleNamespace(type="tool_use", id="call_1", name="search", input={"q": "x"}),
            # Round-tripped, not dropped: a tool-using turn with thinking on
            # must replay this block (with its signature) or the API refuses
            # the continuation.
            SimpleNamespace(type="thinking", thinking="private", signature="sig-1"),
        ],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=40,
            cache_read_input_tokens=25,
            cache_creation_input_tokens=5,
        ),
    )


def _events() -> List[Any]:
    return [
        SimpleNamespace(type="text", text="Hel", snapshot="Hel"),
        SimpleNamespace(type="content_block_stop"),  # non-text events are ignored
        SimpleNamespace(type="text", text="lo", snapshot="Hello"),
    ]


def _run_step(
    monkeypatch: pytest.MonkeyPatch,
    *,
    input_items: List[Any],
    tools: Optional[List[Dict[str, Any]]] = None,
    instructions: str = "Be terse.",
    model: Optional[str] = None,
    effort: Optional[str] = "high",
    final: Any = None,
    thinking: bool = False,
    stream_events: Optional[List[Any]] = None,
) -> Tuple[_FakeClient, List[Tuple[str, Any]]]:
    fake = _FakeClient(
        _FakeStream(
            _events() if stream_events is None else stream_events,
            final or _final_message(),
        )
    )
    monkeypatch.setattr(harness_anthropic, "_client", lambda settings: fake)
    step = AnthropicHarness().build_step(
        _settings(),
        prompt="unused by the step itself",
        user_id="user-1",
        evidence=[],
        model=model,
        effort=effort,
        thinking=thinking,
    )
    events = list(step(input_items, tools or [], instructions))
    return fake, events


def test_translates_history_and_tools_into_anthropic_shapes(monkeypatch):
    """The full round trip in: seed, replayed text, calls, grouped results.

    The two tool results must land in one user message — the Messages API
    rejects a parallel round whose results are split across messages — and the
    assistant's text must share a message with its tool_use blocks.
    """
    input_items = [
        {"role": "user", "content": "find x"},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Looking."}],
        },
        # Extra keys (id/status) mirror what model_dump leaves on real items.
        {
            "type": "function_call",
            "id": "fc_1",
            "status": "completed",
            "name": "search",
            "call_id": "call_1",
            "arguments": '{"q": "x"}',
        },
        {"type": "function_call", "name": "read", "call_id": "call_2", "arguments": ""},
        {"type": "function_call_output", "call_id": "call_1", "output": "result one"},
        {"type": "function_call_output", "call_id": "call_2", "output": "result two"},
        {"type": "reasoning", "summary": []},  # unmappable: skipped, no error
    ]
    tools = [
        {
            "type": "function",
            "name": "search",
            "description": "Find things",
            "parameters": {"type": "object", "properties": {}},
        },
        {"type": "web_search"},  # hosted tool: no Anthropic equivalent, dropped
    ]
    fake, _ = _run_step(monkeypatch, input_items=input_items, tools=tools)

    kwargs = fake.stream_kwargs
    assert kwargs is not None
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["max_tokens"] == 555
    assert kwargs["system"] == "Be terse."
    # The safety identifier, never the raw user id, crosses to the provider.
    assert kwargs["metadata"]["user_id"].startswith("kw_")
    assert "user-1" not in kwargs["metadata"]["user_id"]
    assert kwargs["tools"] == [
        {
            "name": "search",
            "description": "Find things",
            "input_schema": {"type": "object", "properties": {}},
        }
    ]
    assert kwargs["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "find x"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Looking."},
                {"type": "tool_use", "id": "call_1", "name": "search", "input": {"q": "x"}},
                {"type": "tool_use", "id": "call_2", "name": "read", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "result one"},
                {"type": "tool_result", "tool_use_id": "call_2", "content": "result two"},
            ],
        },
    ]


def test_step_yields_deltas_then_exactly_one_completed(monkeypatch):
    _, events = _run_step(
        monkeypatch, input_items=[{"role": "user", "content": "hi"}]
    )
    kinds = [kind for kind, _ in events]
    assert kinds == ["delta", "delta", "completed"]
    assert [value for kind, value in events if kind == "delta"] == ["Hel", "lo"]

    response = events[-1][1]
    assert response.output_text == "Hello"
    calls = [item for item in response.output if item.type == "function_call"]
    assert len(calls) == 1
    assert calls[0].call_id == "call_1"
    assert calls[0].name == "search"
    assert json.loads(calls[0].arguments) == {"q": "x"}
    # Replay order is the API's own: thinking first, then the assistant text,
    # then the tool calls — the translator folds them into one assistant
    # message in exactly this order.
    assert response.output[0].type == "anthropic_thinking"
    assert response.output[0].thinking == "private"
    assert response.output[0].signature == "sig-1"
    assert response.output[1].type == "message"
    assert response.output[1].role == "assistant"
    assert response.output[1].content == [{"type": "output_text", "text": "Hello"}]


def _thinking_stream_events() -> List[Any]:
    return [
        SimpleNamespace(type="thinking", thinking="mulling", snapshot="mulling"),
        SimpleNamespace(type="signature", signature="sig-1"),
        SimpleNamespace(type="text", text="Hello", snapshot="Hello"),
    ]


def test_show_thinking_streams_thinking_deltas(monkeypatch):
    """The loop passes `thinking=run.show_thinking` on every turn (the merge
    that added the flag to the protocol broke this harness's signature), so
    the parameter must be accepted — and with it on, thinking stream events
    surface as ("thinking", ...) ahead of the answer, mirroring the OpenAI
    path's reasoning summaries."""
    _, events = _run_step(
        monkeypatch,
        input_items=[{"role": "user", "content": "hi"}],
        thinking=True,
        stream_events=_thinking_stream_events(),
    )
    kinds = [kind for kind, _ in events]
    assert kinds == ["thinking", "delta", "completed"]
    assert events[0][1] == "mulling"


def test_thinking_off_still_replays_but_does_not_stream(monkeypatch):
    """Visibility off is not thinking off: the blocks are still captured for
    replay (the API refuses a tool continuation without them) — they just
    never reach the user's stream."""
    _, events = _run_step(
        monkeypatch,
        input_items=[{"role": "user", "content": "hi"}],
        thinking=False,
        stream_events=_thinking_stream_events(),
    )
    kinds = [kind for kind, _ in events]
    assert kinds == ["delta", "completed"]
    response = events[-1][1]
    assert response.output[0].type == "anthropic_thinking"
    assert response.output[0].thinking == "private"


def test_output_items_survive_a_replay_round_trip(monkeypatch):
    """What a completed step emits must translate back on the next step.

    This is the invariant that lets a run park mid-turn: the loop serialises
    `response.output` into history and this harness must read its own items
    back. `vars()` mirrors the loop's `_serialize_item` for plain dataclasses.
    """
    _, events = _run_step(monkeypatch, input_items=[{"role": "user", "content": "hi"}])
    response = events[-1][1]
    replayed = [dict(vars(item)) for item in response.output]
    replayed.append(
        {"type": "function_call_output", "call_id": "call_1", "output": "done"}
    )
    messages = harness_anthropic._anthropic_messages(
        [{"role": "user", "content": "hi"}, *replayed]
    )
    assert messages == [
        {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "private", "signature": "sig-1"},
                {"type": "text", "text": "Hello"},
                {"type": "tool_use", "id": "call_1", "name": "search", "input": {"q": "x"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "done"}
            ],
        },
    ]


def test_usage_shim_parses_into_correct_token_counts():
    """`token_counts` must see Anthropic's split counts as one whole prompt.

    Anthropic excludes cache reads/writes from `input_tokens`; the OpenAI
    schema `token_counts` speaks includes them. 100 uncached + 25 read + 5
    written = 130 input, of which 25 are billed at the cached rate.
    """
    shim = _shim_usage(
        SimpleNamespace(
            input_tokens=100,
            output_tokens=40,
            cache_read_input_tokens=25,
            cache_creation_input_tokens=5,
        )
    )
    counts = token_counts(shim)
    assert counts is not None
    assert counts.input_tokens == 130
    assert counts.cached_input_tokens == 25
    assert counts.uncached_input_tokens == 105
    assert counts.output_tokens == 40
    assert counts.total_tokens == 170


def test_usage_shim_without_cache_fields():
    shim = _shim_usage(SimpleNamespace(input_tokens=10, output_tokens=3))
    counts = token_counts(shim)
    assert counts is not None
    assert counts.input_tokens == 10
    assert counts.cached_input_tokens == 0
    assert counts.total_tokens == 13
    assert _shim_usage(None) is None


def test_usage_recorded_before_completed_is_yielded(monkeypatch):
    """The ledger write must not depend on the consumer draining the stream."""
    recorded: List[Dict[str, Any]] = []

    def fake_record(**kwargs: Any) -> None:
        recorded.append(kwargs)

    monkeypatch.setattr(usage, "record_model_usage", fake_record)
    fake = _FakeClient(_FakeStream(_events(), _final_message()))
    monkeypatch.setattr(harness_anthropic, "_client", lambda settings: fake)
    step = AnthropicHarness().build_step(
        _settings(),
        prompt="p",
        user_id="user-1",
        evidence=[],
        model="claude-opus-4-6",  # per-turn override must key the usage row
    )
    for kind, _value in step([{"role": "user", "content": "hi"}], [], ""):
        if kind == "completed":
            assert recorded, "usage was not recorded before the terminal yield"
    assert len(recorded) == 1
    row = recorded[0]
    assert row["provider"] == "anthropic"
    assert row["model"] == "claude-opus-4-6"
    assert row["operation"] == usage.CHAT
    counts = token_counts(row["usage"])
    assert counts is not None and counts.total_tokens == 170
    # The override also reaches the request itself.
    assert fake.stream_kwargs is not None
    assert fake.stream_kwargs["model"] == "claude-opus-4-6"


def test_malformed_tool_items_fail_loudly():
    """Skipping is only for unmappable *types*; broken calls must raise.

    A silently dropped function_call desynchronises the tool_use/tool_result
    pairing for the rest of the turn, which the Messages API then rejects with
    an error pointing nowhere near the cause.
    """
    with pytest.raises(ValueError, match="call_id"):
        harness_anthropic._anthropic_messages(
            [{"type": "function_call", "name": "x", "arguments": "{}"}]
        )
    with pytest.raises(ValueError, match="call_id"):
        harness_anthropic._anthropic_messages(
            [{"type": "function_call_output", "output": "y"}]
        )
    with pytest.raises(ValueError, match="non-JSON"):
        harness_anthropic._anthropic_messages(
            [
                {
                    "type": "function_call",
                    "name": "x",
                    "call_id": "c1",
                    "arguments": "not json",
                }
            ]
        )


def test_empty_instructions_and_tools_are_omitted(monkeypatch):
    fake, _ = _run_step(
        monkeypatch,
        input_items=[{"role": "user", "content": "hi"}],
        tools=[{"type": "web_search"}],  # translates to nothing
        instructions="",
    )
    assert fake.stream_kwargs is not None
    assert "system" not in fake.stream_kwargs
    assert "tools" not in fake.stream_kwargs


def test_client_guard_requires_api_key():
    settings = Settings(model_provider="openai", openai_api_key="k")
    with pytest.raises(Exception, match="ANTHROPIC_API_KEY"):
        harness_anthropic._client(settings)


def test_effort_maps_onto_anthropics_own_ladder(monkeypatch):
    """The five shared strings steer adaptive thinking; "none" disables it.

    Not a guessed equivalence — `output_config.effort` in the Messages API is
    literally the same {"low","medium","high","xhigh","max"} ladder this
    deployment's `ReasoningEffort` uses.
    """
    fake, _ = _run_step(
        monkeypatch, input_items=[{"role": "user", "content": "hi"}], effort="high"
    )
    assert fake.stream_kwargs is not None
    assert fake.stream_kwargs["thinking"] == {"type": "adaptive"}
    assert fake.stream_kwargs["output_config"] == {"effort": "high"}

    fake, _ = _run_step(
        monkeypatch, input_items=[{"role": "user", "content": "hi"}], effort="none"
    )
    assert fake.stream_kwargs is not None
    assert fake.stream_kwargs["thinking"] == {"type": "disabled"}
    assert "output_config" not in fake.stream_kwargs


def test_an_unset_effort_falls_back_to_the_deployment_default(monkeypatch):
    """`openai_reasoning_effort` predates the second harness; despite its name
    it is the deployment's reasoning default, and an unset per-turn effort
    lands on it here exactly as it does on the OpenAI path."""
    fake, _ = _run_step(
        monkeypatch, input_items=[{"role": "user", "content": "hi"}], effort=None
    )
    assert fake.stream_kwargs is not None
    # The test Settings leave openai_reasoning_effort at its default, "low".
    assert fake.stream_kwargs["thinking"] == {"type": "adaptive"}
    assert fake.stream_kwargs["output_config"] == {"effort": "low"}


def test_a_redacted_thinking_block_round_trips_opaquely(monkeypatch):
    final = SimpleNamespace(
        content=[
            SimpleNamespace(type="redacted_thinking", data="opaque-bytes"),
            SimpleNamespace(type="text", text="Hello"),
        ],
        usage=None,
    )
    _, events = _run_step(
        monkeypatch, input_items=[{"role": "user", "content": "hi"}], final=final
    )
    response = events[-1][1]
    assert response.output[0].type == "anthropic_redacted_thinking"
    replayed = [dict(vars(item)) for item in response.output]
    messages = harness_anthropic._anthropic_messages(
        [{"role": "user", "content": "hi"}, *replayed]
    )
    assert messages[1]["content"][0] == {
        "type": "redacted_thinking",
        "data": "opaque-bytes",
    }
