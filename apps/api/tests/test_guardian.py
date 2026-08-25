"""`services/guardian.review`: the fail-closed auto-approval triage.

The guardian's one power is converting "ask a human" into "execute now", so the
tests are organized around the one property that makes it safe to hold that
power: every path that is not an exact, well-formed approval defers. The model
call is monkeypatched at the module seam (`guardian._call_model`) — no network,
no OpenAI client, no database; run in isolation with `--noconftest`.

What is pinned here:

- only ``{"decision": "approve"}`` approves — an explicit defer, malformed
  JSON, wrong keys, wrong casing, non-object replies all defer;
- inputs are clipped to the module bounds before they reach the model;
- the scripted double defers without ever touching the model seam;
- `review` never raises, even when the seam itself explodes — the exception
  becomes a defer with the error clipped into the reason.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from app.config import Settings
from app.services import guardian
from app.services.guardian import (
    GUARDIAN_OPERATION,
    MAX_GUARDIAN_APPROVALS_PER_TURN,
    MAX_GUARDIAN_ARGUMENTS_CHARS,
    MAX_GUARDIAN_PREVIEW_CHARS,
    MAX_GUARDIAN_PROMPT_CHARS,
    MAX_GUARDIAN_REASON_CHARS,
    GuardianVerdict,
    review,
)


def _settings(provider: str = "openai") -> Settings:
    """Only the attribute `review` reads before the seam.

    A real `Settings` would drag in env and boot validators the guardian never
    touches; past the seam the settings object is opaque to these tests.
    """
    return cast(Settings, SimpleNamespace(active_model_provider=provider))


def _scripted_model(monkeypatch: pytest.MonkeyPatch, reply: str) -> list[str]:
    """Replace the model seam with a canned reply; returns captured payloads."""
    payloads: list[str] = []

    def fake(*, payload: str, settings: Settings) -> str:
        payloads.append(payload)
        return reply

    monkeypatch.setattr(guardian, "_call_model", fake)
    return payloads


def _review(**overrides: str) -> GuardianVerdict:
    kwargs = dict(
        name="todos_write",
        arguments_json='{"text": "buy milk"}',
        preview="Add todo: buy milk",
        prompt="add buy milk to my todos",
        settings=_settings(),
    )
    kwargs.update(overrides)
    return review(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Verdict parsing: only an exact approval approves
# --------------------------------------------------------------------------


def test_clean_approval_approves(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted_model(
        monkeypatch, '{"decision": "approve", "reason": "directly requested"}'
    )
    verdict = _review()
    assert verdict == GuardianVerdict(True, "directly requested")


def test_explicit_defer_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted_model(monkeypatch, '{"decision": "defer", "reason": "mass deletion"}')
    verdict = _review()
    assert verdict.approve is False
    assert verdict.reason == "mass deletion"


def test_fenced_json_is_still_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """A code-fenced approval is the model following the contract verbosely."""
    _scripted_model(
        monkeypatch, '```json\n{"decision": "approve", "reason": "ok"}\n```'
    )
    assert _review().approve is True


def test_malformed_json_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted_model(monkeypatch, "approve")
    verdict = _review()
    assert verdict.approve is False
    assert "unparseable" in verdict.reason


def test_wrong_keys_defer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The right word under the wrong key is not an approval."""
    _scripted_model(monkeypatch, '{"verdict": "approve", "reason": "sure"}')
    verdict = _review()
    assert verdict.approve is False
    assert "unrecognized" in verdict.reason


@pytest.mark.parametrize(
    "decision", ["Approve", "approved", "APPROVE", " approve", "yes", True, 1, None]
)
def test_anything_but_the_exact_string_defers(
    monkeypatch: pytest.MonkeyPatch, decision: object
) -> None:
    import json

    _scripted_model(
        monkeypatch, json.dumps({"decision": decision, "reason": "close enough"})
    )
    assert _review().approve is False


def test_non_object_reply_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted_model(monkeypatch, '["approve"]')
    verdict = _review()
    assert verdict.approve is False
    assert "non-object" in verdict.reason


def test_empty_reply_defers(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted_model(monkeypatch, "")
    assert _review().approve is False


def test_missing_reason_gets_a_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _scripted_model(monkeypatch, '{"decision": "approve"}')
    verdict = _review()
    assert verdict.approve is True
    assert verdict.reason  # never an empty audit reason


def test_rambling_reason_is_clipped(monkeypatch: pytest.MonkeyPatch) -> None:
    long_reason = "x" * 5_000
    _scripted_model(
        monkeypatch, '{"decision": "defer", "reason": "' + long_reason + '"}'
    )
    assert len(_review().reason) == MAX_GUARDIAN_REASON_CHARS


# --------------------------------------------------------------------------
# Input clipping: bounded before the model sees it
# --------------------------------------------------------------------------


def test_inputs_are_clipped_to_the_module_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = _scripted_model(monkeypatch, '{"decision": "defer", "reason": "r"}')
    _review(
        prompt="p" * (MAX_GUARDIAN_PROMPT_CHARS + 1_000),
        arguments_json="a" * (MAX_GUARDIAN_ARGUMENTS_CHARS + 1_000),
        preview="v" * (MAX_GUARDIAN_PREVIEW_CHARS + 1_000),
    )
    payload = payloads[0]
    assert "p" * MAX_GUARDIAN_PROMPT_CHARS in payload
    assert "p" * (MAX_GUARDIAN_PROMPT_CHARS + 1) not in payload
    assert "a" * MAX_GUARDIAN_ARGUMENTS_CHARS in payload
    assert "a" * (MAX_GUARDIAN_ARGUMENTS_CHARS + 1) not in payload
    assert "v" * MAX_GUARDIAN_PREVIEW_CHARS in payload
    assert "v" * (MAX_GUARDIAN_PREVIEW_CHARS + 1) not in payload


def test_payload_carries_all_four_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = _scripted_model(monkeypatch, '{"decision": "defer", "reason": "r"}')
    _review()
    payload = payloads[0]
    assert "add buy milk to my todos" in payload
    assert "todos_write" in payload
    assert '{"text": "buy milk"}' in payload
    assert "Add todo: buy milk" in payload


# --------------------------------------------------------------------------
# Provider gate and fail-closed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["scripted"])
def test_the_scripted_double_defers_without_calling_the_model(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    payloads = _scripted_model(monkeypatch, '{"decision": "approve", "reason": "!"}')
    verdict = _review(settings=_settings(provider))  # type: ignore[arg-type]
    assert verdict == GuardianVerdict(False, "guardian requires a real model provider")
    assert payloads == []  # the seam was never reached


def test_an_exploding_seam_defers_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*, payload: str, settings: Settings) -> str:
        raise RuntimeError("provider is on fire")

    monkeypatch.setattr(guardian, "_call_model", boom)
    verdict = _review()  # must not raise
    assert verdict.approve is False
    assert verdict.reason.startswith("guardian_error: ")
    assert "provider is on fire" in verdict.reason


def test_an_exploding_seam_reason_is_clipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*, payload: str, settings: Settings) -> str:
        raise RuntimeError("x" * 10_000)

    monkeypatch.setattr(guardian, "_call_model", boom)
    verdict = _review()
    assert verdict.approve is False
    assert len(verdict.reason) == MAX_GUARDIAN_REASON_CHARS


def test_even_settings_access_failures_defer() -> None:
    """The provider gate itself is inside the try: a broken settings object
    (no `active_model_provider`) still yields a defer, not an AttributeError."""
    verdict = _review(settings=cast(Settings, SimpleNamespace()))  # type: ignore[arg-type]
    assert verdict.approve is False
    assert verdict.reason.startswith("guardian_error: ")


# --------------------------------------------------------------------------
# Exported constants the loop wires against
# --------------------------------------------------------------------------


def test_exported_constants() -> None:
    assert GUARDIAN_OPERATION == "guardian"
    assert MAX_GUARDIAN_APPROVALS_PER_TURN == 5


def test_the_anthropic_provider_reaches_the_seam_and_may_approve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anthropic is a real provider now: the gate lets it through to the same
    seam, and an exact approval approves exactly as it does on OpenAI."""
    payloads = _scripted_model(
        monkeypatch, '{"decision": "approve", "reason": "plainly asked for"}'
    )
    verdict = _review(settings=_settings("anthropic"))  # type: ignore[arg-type]
    assert verdict == GuardianVerdict(True, "plainly asked for")
    assert len(payloads) == 1


def test_the_anthropic_leg_bills_and_extracts_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_call_model_anthropic`: one bounded Messages call, thinking disabled,
    text blocks joined, usage recorded under the guardian operation with
    provider=anthropic."""
    from app.services import usage as usage_module
    from app.services.harness import anthropic as harness_anthropic

    created: dict = {}

    class _FakeMessages:
        def create(self, **kwargs):
            created.update(kwargs)
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="thinking", thinking="hmm", signature="s"),
                    SimpleNamespace(type="text", text='{"decision": "defer", '),
                    SimpleNamespace(type="text", text='"reason": "odd"}'),
                ],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            )

    monkeypatch.setattr(
        harness_anthropic,
        "_client",
        lambda settings: SimpleNamespace(messages=_FakeMessages()),
    )
    recorded: dict = {}

    def fake_record(**kwargs):
        recorded.update(kwargs)
        return None

    monkeypatch.setattr(usage_module, "record_model_usage", fake_record)
    settings = cast(
        Settings,
        SimpleNamespace(
            active_model_provider="anthropic",
            anthropic_context_model="claude-haiku-4-5-20251001",
            anthropic_api_key=SimpleNamespace(get_secret_value=lambda: "k"),
            anthropic_timeout_seconds=5.0,
        ),
    )
    text = guardian._call_model_anthropic(payload="judge this", settings=settings)
    assert text == '{"decision": "defer", "reason": "odd"}'
    assert created["model"] == "claude-haiku-4-5-20251001"
    assert created["max_tokens"] == 200
    assert created["thinking"] == {"type": "disabled"}
    assert created["system"] == guardian.GUARDIAN_INSTRUCTIONS
    assert created["messages"] == [{"role": "user", "content": "judge this"}]
    assert recorded["operation"] == GUARDIAN_OPERATION
    assert recorded["provider"] == "anthropic"
    assert recorded["usage"] is not None
