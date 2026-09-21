from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.services import model
from app.services.model import (
    ModelConfigurationError,
    _openai_client,
    _openai_input,
    stream_agent_response,
)
from app.services.retrieval import Evidence


def evidence() -> list[Evidence]:
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


def test_openai_provider_refuses_to_boot_without_a_key():
    """The key is a startup requirement, not a per-turn failure.

    There is no offline mode to fall through to, so an app that booted without a
    key could only 500 on every message it accepted.
    """
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(_env_file=None, model_provider="openai", openai_api_key=None)
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(_env_file=None, model_provider="openai", openai_api_key=SecretStr("  "))


def test_configured_openai_settings_select_openai_and_hide_the_key():
    settings = Settings(
        _env_file=None,
        model_provider="openai",
        openai_api_key=SecretStr("test-key"),
    )
    assert settings.active_model_provider == "openai"
    assert "test-key" not in repr(settings)


def test_client_construction_still_guards_a_keyless_settings():
    """model_copy bypasses validation, so the client keeps its own check."""
    settings = Settings(
        _env_file=None, model_provider="openai", openai_api_key=SecretStr("test-key")
    ).model_copy(update={"openai_api_key": None})
    with pytest.raises(ModelConfigurationError, match="OPENAI_API_KEY"):
        _openai_client(settings)


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


def test_agent_turn_is_grounded_and_not_stored():
    captured: dict[str, object] = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _FakeStream("Maya owns the launch. [1]")

    settings = Settings(
        _env_file=None,
        model_provider="openai",
        openai_api_key=SecretStr("test-key"),
        openai_model="test-model",
        openai_reasoning_effort="low",
    )
    events = list(
        stream_agent_response(
            SimpleNamespace(responses=FakeResponses()),  # type: ignore[arg-type]
            settings,
            user_id="user-1",
            input_items=[
                {
                    "role": "user",
                    "content": _openai_input("Who owns the launch?", evidence()),
                }
            ],
            tools=[],
            instructions=model.CHAT_INSTRUCTIONS,
        )
    )

    assert events[0] == ("delta", "Maya owns the launch. [1]")
    assert events[-1][0] == "completed"
    assert captured["model"] == "test-model"
    assert captured["store"] is False
    assert captured["stream"] is True
    assert captured["reasoning"] == {"effort": "low"}
    assert captured["text"] == {"verbosity": "low"}
    assert "Maya owns" in str(captured["input"])
    assert "user-1" not in str(captured["safety_identifier"])
    assert "helpful assistant" in str(captured["instructions"])


def test_openai_client_is_built_from_settings():
    settings = Settings(
        _env_file=None,
        model_provider="openai",
        openai_api_key=SecretStr("test-key"),
        openai_timeout_seconds=60.0,
    )
    client = _openai_client(settings)
    assert client.api_key == "test-key"
    assert client.timeout == 60.0
    assert client.max_retries == 1


def test_prompt_carries_sources_only_when_there_is_evidence():
    with_sources = _openai_input("Who owns the launch?", evidence())
    assert "Source passages from the user's library" in with_sources
    assert "brief.md, passage 1" in with_sources

    without = _openai_input("hi", [])
    assert "Question:\nhi" in without
    assert "Source passages from the user's library" not in without


def test_the_evidence_block_restates_the_citation_rule():
    """The [n] contract is stated beside the passages, not only at the top.

    The header used to read "Optional source passages", which contradicted the
    instruction the citation validator enforces. A live audit found three of
    four grounded answers restating a source almost verbatim with no marker
    anywhere, so the UI badged accurate answers "This answer cites nothing".
    The passages are optional to use; citing the ones you do use is not, and
    the prompt has to say so where the model is actually reading.
    """
    prompt = _openai_input("Who owns the launch?", evidence())
    assert "Optional source passages" not in prompt
    assert "[n]" in prompt
    # The rule travels with the evidence: everything after the header, not in
    # some earlier section the passages have scrolled away from.
    header = prompt.index("Source passages from the user's library")
    assert prompt.index("[n]", header) > header


def _openai_settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        model_provider="openai",
        openai_api_key=SecretStr("test-key"),
        openai_model="test-model",
        **overrides,
    )


def _capturing_client(response) -> tuple[SimpleNamespace, dict[str, object]]:
    captured: dict[str, object] = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return response

    return SimpleNamespace(responses=FakeResponses()), captured


def test_the_grounded_answer_and_repair_calls_carry_a_hashed_identifier(monkeypatch):
    """Every site in this file that RECEIVES a user id passes it on, hashed.

    These two were the first that had one available and dropped it — and they
    carry the most caller-controlled text on the surface: a 4,000-character
    question plus an arbitrary JSON Schema, through a bearer door any workspace
    token can drive. Without the identifier a policy signal attributes to the
    whole account instead of the member whose token drove it, which is the
    posture the standing no-live-provider-abuse rule exists to keep.
    """
    settings = _openai_settings()

    client, captured = _capturing_client(
        SimpleNamespace(output_text="Maya owns the launch [1].", status="completed")
    )
    monkeypatch.setattr(model, "_openai_client", lambda _settings: client)

    text, schema_error = model.answer_from_passages(
        "Who owns the launch?", evidence(), None, user_id="user-1", settings=settings
    )
    assert text and not schema_error
    assert captured["safety_identifier"] == model.privacy_safe_identifier("user-1")
    assert "user-1" not in str(captured["safety_identifier"])

    repair_client, repair_captured = _capturing_client(
        SimpleNamespace(output_text="Maya owns the launch [1].", status="completed")
    )
    monkeypatch.setattr(model, "_openai_client", lambda _settings: repair_client)
    assert model.regenerate_unsupported(
        "Ravi owns the launch [1].",
        evidence(),
        ("Ravi owns the launch [1].",),
        user_id="user-1",
        settings=settings,
    )
    assert repair_captured["safety_identifier"] == model.privacy_safe_identifier(
        "user-1"
    )


def test_a_repair_that_came_back_incomplete_is_discarded(monkeypatch):
    """A partial rewrite is not a rewrite.

    The Responses API carries the text it managed to produce on an `incomplete`
    response, and returning it hands the caller a fragment that grades BETTER
    than the full answer — fewer sentences, so fewer unsupported ones. Nothing
    downstream could tell that apart from a genuine correction.
    """
    settings = _openai_settings()
    client, _captured = _capturing_client(
        SimpleNamespace(
            output_text="Maya owns the launch [1]. And then the answer stops mid-",
            status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        )
    )
    monkeypatch.setattr(model, "_openai_client", lambda _settings: client)

    assert (
        model.regenerate_unsupported(
            "Ravi owns the launch [1].",
            evidence(),
            ("Ravi owns the launch [1].",),
            user_id="user-1",
            settings=settings,
        )
        == ""
    )


def test_the_repair_budget_follows_the_answer_and_refuses_an_unrepairable_one(
    monkeypatch,
):
    """The fixed 2,000-token ceiling could not return the answer it was handed.

    The instruction is "return the complete corrected answer", so any answer
    over roughly 8,000 characters came back cut off — on exactly the long
    research answers this pass was built for.
    """
    settings = _openai_settings()
    client, captured = _capturing_client(
        SimpleNamespace(output_text="rewritten [1].", status="completed")
    )
    monkeypatch.setattr(model, "_openai_client", lambda _settings: client)

    long_answer = "Maya owns the October launch [1]. " * 400
    model.regenerate_unsupported(
        long_answer,
        evidence(),
        ("Maya owns the October launch [1].",),
        user_id="user-1",
        settings=settings,
    )
    assert captured["max_output_tokens"] >= len(long_answer) // 4

    # Past the ceiling the stage declines rather than asking for a rewrite it
    # cannot receive; the caller keeps the original answer.
    captured.clear()
    unrepairable = "Maya owns the October launch [1]. " * 4000
    assert (
        model.regenerate_unsupported(
            unrepairable,
            evidence(),
            ("Maya owns the October launch [1].",),
            user_id="user-1",
            settings=settings,
        )
        == ""
    )
    assert captured == {}


def test_local_web_origin_accepts_both_loopback_names():
    settings = Settings(
        _env_file=None,
        web_origin="http://localhost:3000",
        openai_api_key=SecretStr("test-key"),
    )
    assert settings.allowed_web_origins == [
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ]
