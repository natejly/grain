from __future__ import annotations

import json

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.services.harness import HARNESSES, Harness, resolve_harness
from app.services.harness.openai import OpenAIHarness
from app.services.harness.scripted import ScriptedHarness
from app.services.retrieval import Evidence
from app.services.scripted_model import scripted_model_step


def _openai_settings() -> Settings:
    return Settings(
        _env_file=None,
        model_provider="openai",
        openai_api_key=SecretStr("test-key"),
    )


@pytest.fixture
def scripted_settings(tmp_path):
    path = tmp_path / "script.json"
    path.write_text(json.dumps([{"match": "nothing here", "steps": [{"text": "hi"}]}]))
    return Settings(
        _env_file=None,
        app_env="test",
        model_provider="scripted",
        scripted_model_script=path,
    )


def _evidence() -> list[Evidence]:
    return [
        Evidence(
            chunk_id="c1",
            source_id="s1",
            filename="brief.md",
            ordinal=0,
            excerpt="Maya owns the launch.",
            score=1.0,
        )
    ]


def test_registry_resolves_the_configured_provider_to_its_harness(scripted_settings):
    """The loop asks for a backend by the validated provider name, nothing else.

    A registry that resolved to the wrong class — or a stray second instance
    instead of the shared singleton — would let a turn run on a backend the
    config never chose, the exact drift the seam exists to rule out.
    """
    openai = resolve_harness(_openai_settings())
    scripted = resolve_harness(scripted_settings)

    assert isinstance(openai, OpenAIHarness)
    assert openai.name == "openai"
    assert isinstance(scripted, ScriptedHarness)
    assert scripted.name == "scripted"

    # Resolution returns the registered singletons, not fresh instances.
    assert openai is HARNESSES["openai"]
    assert scripted is HARNESSES["scripted"]


def test_both_backends_conform_to_the_harness_interface():
    """Every registry entry must satisfy the one contract the loop depends on.

    A third backend is meant to drop in as a new class plus a registry entry; if
    an implementation could register without conforming, the loop would discover
    the missing method mid-turn instead of here.
    """
    assert set(HARNESSES) == {"openai", "scripted"}
    for name, harness in HARNESSES.items():
        assert isinstance(harness, Harness)
        assert harness.name == name
        assert callable(harness.build_step)


def test_scripted_harness_round_trips_a_turn_unchanged(scripted_settings):
    """The harness is a thin layer, so its step must match the double it wraps.

    An unmatched prompt still has to answer from the evidence; the harness-built
    step and a direct `scripted_model_step` must produce byte-identical events,
    proving the wrapper adds nothing and drops nothing.
    """
    evidence = _evidence()
    prompt = "something no entry covers"

    via_harness = resolve_harness(scripted_settings).build_step(
        scripted_settings, prompt=prompt, user_id="u", evidence=evidence
    )
    direct = scripted_model_step(scripted_settings, prompt=prompt, evidence=evidence)

    assert list(via_harness([], [], "")) == list(direct([], [], ""))
    text = "".join(
        str(value)
        for kind, value in resolve_harness(scripted_settings)
        .build_step(scripted_settings, prompt=prompt, user_id="u", evidence=evidence)([], [], "")
        if kind == "delta"
    )
    assert text == "- Maya owns the launch. [1]"


def test_resolve_harness_names_an_unregistered_provider():
    """A provider that widened the Literal and added its config guard but forgot a
    HARNESSES entry must fail by name, not as a bare KeyError on the first turn.
    """
    from types import SimpleNamespace

    with pytest.raises(ValueError, match="No harness is registered"):
        resolve_harness(SimpleNamespace(active_model_provider="anthropic"))


def test_a_wrong_signature_build_step_is_rejected_by_invocation():
    """isinstance() against a runtime-checkable Protocol only proves `build_step`
    exists — not its shape. The contract is the signature, so the check that
    matters is invoking it with the loop's exact keyword arguments; a drop-in that
    took no settings, or dropped `evidence`, would pass isinstance and fail here.
    """

    class Broken:
        name = "broken"

        def build_step(self):  # the wrong shape a real third backend might ship
            return lambda items, tools, instructions: iter(())

    assert isinstance(Broken(), Harness)  # the weak check is fooled...
    with pytest.raises(TypeError):  # ...invocation is not.
        Broken().build_step(_openai_settings(), prompt="p", user_id="u", evidence=[])


def test_openai_harness_forwards_the_turn_arguments_to_the_stream(monkeypatch):
    """The one production backend and the template a third copies: prove it threads
    client, user_id, tools and instructions straight through. A mis-wire — a
    swapped user_id feeds `privacy_safe_identifier`, a swapped tools/instructions
    changes the request — would otherwise ship undetected, since every other
    OpenAI test calls `stream_agent_response` directly and skips this wiring.
    """
    from app.services.harness import openai as openai_harness

    captured: dict = {}

    def spy(client, settings, *, user_id, input_items, tools, instructions, operation=""):
        captured.update(
            client=client,
            user_id=user_id,
            input_items=input_items,
            tools=tools,
            instructions=instructions,
        )
        yield ("completed", object())

    monkeypatch.setattr(openai_harness, "_openai_client", lambda settings: "CLIENT")
    monkeypatch.setattr(openai_harness, "stream_agent_response", spy)

    step = OpenAIHarness().build_step(
        _openai_settings(), prompt="p", user_id="alice", evidence=[]
    )
    events = list(step([{"role": "user"}], [{"type": "function"}], "instr"))

    assert captured == {
        "client": "CLIENT",
        "user_id": "alice",
        "input_items": [{"role": "user"}],
        "tools": [{"type": "function"}],
        "instructions": "instr",
    }
    assert events[-1][0] == "completed"


def test_registry_keys_only_on_providers_settings_would_boot(scripted_settings):
    """Every registry key is a provider the config guard already accepts.

    The registry is not a second gate: it keys on `active_model_provider`, so a
    provider Settings refused to boot can never be resolved. Guarding the two
    validators here keeps that assumption honest.
    """
    assert set(HARNESSES) <= {"openai", "scripted"}
    assert _openai_settings().active_model_provider == "openai"
    assert scripted_settings.active_model_provider == "scripted"

    # scripted is fatal outside a development/test app_env...
    with pytest.raises(ValidationError, match="APP_ENV"):
        Settings(
            _env_file=None,
            app_env="production",
            model_provider="scripted",
            scripted_model_script="script.json",
        )
    # ...and openai is fatal without a key.
    with pytest.raises(ValidationError, match="OPENAI_API_KEY"):
        Settings(_env_file=None, model_provider="openai", openai_api_key=None)
