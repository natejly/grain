from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.services import model
from app.services.model import ModelConfigurationError, generate_grounded_answer
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


def test_auto_provider_uses_openai_only_when_key_exists():
    offline = Settings(_env_file=None, model_provider="auto", openai_api_key=None)
    online = Settings(
        _env_file=None,
        model_provider="auto",
        openai_api_key=SecretStr("test-key"),
    )
    assert offline.active_model_provider == "deterministic"
    assert online.active_model_provider == "openai"
    assert "test-key" not in repr(online)


def test_explicit_openai_requires_a_key():
    settings = Settings(_env_file=None, model_provider="openai", openai_api_key=None)
    with pytest.raises(ModelConfigurationError, match="OPENAI_API_KEY"):
        generate_grounded_answer(
            "Who owns the launch?",
            evidence(),
            user_id="user-1",
            settings=settings,
        )


def test_openai_response_is_grounded_and_not_stored(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_text="Maya owns the launch. [1]")

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.responses = FakeResponses()

    monkeypatch.setattr(model, "OpenAI", FakeOpenAI)
    settings = Settings(
        _env_file=None,
        model_provider="openai",
        openai_api_key=SecretStr("test-key"),
        openai_model="test-model",
        openai_reasoning_effort="low",
    )
    answer = generate_grounded_answer(
        "Who owns the launch?",
        evidence(),
        user_id="user-1",
        settings=settings,
    )

    assert answer == "Maya owns the launch. [1]"
    assert captured["model"] == "test-model"
    assert captured["store"] is False
    assert captured["reasoning"] == {"effort": "low"}
    assert captured["text"] == {"verbosity": "low"}
    assert "Maya owns" in str(captured["input"])
    assert "user-1" not in str(captured["safety_identifier"])
    assert "helpful assistant" in str(captured["instructions"])
    assert captured["client"] == {
        "api_key": "test-key",
        "timeout": 60.0,
        "max_retries": 1,
    }


def test_openai_answers_without_sources(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(output_text="Hello! How can I help?")

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    monkeypatch.setattr(model, "OpenAI", FakeOpenAI)
    settings = Settings(
        _env_file=None,
        model_provider="openai",
        openai_api_key=SecretStr("test-key"),
        openai_model="test-model",
    )
    answer = generate_grounded_answer(
        "hi",
        [],
        user_id="user-1",
        settings=settings,
    )

    assert answer == "Hello! How can I help?"
    assert "Question:\nhi" in str(captured["input"])
    assert "Optional source passages" not in str(captured["input"])


def test_deterministic_without_sources_explains_offline_mode():
    settings = Settings(_env_file=None, model_provider="deterministic")
    answer = generate_grounded_answer(
        "hi",
        [],
        user_id="user-1",
        settings=settings,
    )
    assert "offline mode" in answer
    assert "OPENAI_API_KEY" in answer


def test_local_web_origin_accepts_both_loopback_names():
    settings = Settings(_env_file=None, web_origin="http://localhost:3000")
    assert settings.allowed_web_origins == [
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ]
