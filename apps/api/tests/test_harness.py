from __future__ import annotations

import ast
import inspect
import json
import textwrap
from typing import Any, List, Optional, Tuple

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.models import Run
from app.services import agent_loop
from app.services.harness import HARNESSES, Harness, ModelStep, resolve_harness
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
    assert set(HARNESSES) == {"openai", "anthropic", "scripted"}
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
        resolve_harness(SimpleNamespace(active_model_provider="mistral"))


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

    def spy(
        client,
        settings,
        *,
        user_id,
        input_items,
        tools,
        instructions,
        model=None,
        effort=None,
        thinking=False,
        operation="",
    ):
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
    assert set(HARNESSES) <= {"openai", "anthropic", "scripted"}
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
    # ...and so is anthropic.
    with pytest.raises(ValidationError, match="ANTHROPIC_API_KEY"):
        Settings(_env_file=None, model_provider="anthropic", anthropic_api_key=None)


# --- The call site, not the callee -------------------------------------------
#
# A harness that accepts `thinking=` proves nothing on its own: the incident
# fixed in 948fd97 was a TypeError on *every* Anthropic run because the loop
# passed a keyword the harness had never grown, and a test that only exercised
# the harness directly stayed green throughout it. What has to be pinned is the
# join — the arguments `agent_loop` actually writes, the contract `Harness`
# declares, and the signatures the registered backends ship — so that drift in
# any one of the three fails here instead of on a user's turn.


def _shape(signature: inspect.Signature) -> List[Tuple[str, Any, Any]]:
    """(name, kind, default) per parameter — the part that decides whether a
    call binds. Annotations are deliberately excluded: they are strings under
    `from __future__ import annotations`, and a cosmetic retype must not fail a
    drift test."""
    return [
        (name, parameter.kind, parameter.default)
        for name, parameter in signature.parameters.items()
    ]


def _call_site_keywords() -> set[str]:
    """The keyword names `_default_model_step` literally passes to `build_step`.

    Read out of the source rather than restated here, on purpose: a restated
    list is a second copy that drifts silently, which is the very failure mode
    under test.
    """
    tree = ast.parse(
        textwrap.dedent(inspect.getsource(agent_loop._default_model_step))
    )
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "build_step"
    ]
    assert len(calls) == 1, "the loop should resolve one harness and call it once"
    return {keyword.arg for keyword in calls[0].keywords if keyword.arg}


def test_the_loops_call_site_binds_against_the_harness_contract():
    """Every keyword the loop passes is one the `Harness` protocol accepts.

    This is the direction the incident travelled: the protocol grew `thinking`
    alongside the loop, and a backend written before it kept a narrower
    signature.
    """
    keywords = _call_site_keywords()
    assert "thinking" in keywords, "the loop still forwards the show-thinking flag"
    inspect.signature(Harness.build_step).bind(
        object(), object(), **{name: object() for name in keywords}
    )


def test_every_registered_harness_has_the_contracts_build_step_shape():
    """A backend whose `build_step` drifts from the protocol fails here.

    `isinstance` against a runtime-checkable Protocol cannot see this, and
    neither can a test that calls one backend with arguments it happens to
    accept: the check has to be that *each registered* harness would bind the
    same call the loop makes of whichever one is resolved.
    """
    contract = _shape(inspect.signature(Harness.build_step))
    keywords = _call_site_keywords()
    for name, harness in HARNESSES.items():
        assert _shape(inspect.signature(type(harness).build_step)) == contract, (
            f"the {name} harness's build_step has drifted from the Harness protocol"
        )
        inspect.signature(harness.build_step).bind(
            object(), **{keyword: object() for keyword in keywords}
        )


def test_a_real_turn_forwards_show_thinking_through_the_loops_call_site(monkeypatch):
    """Drive the actual call path, not a reconstruction of it.

    The recorder is held to the protocol's own signature first, so it cannot
    quietly absorb a keyword a real backend would reject — then the run's
    `show_thinking` is asserted to arrive as `thinking`, which is the wiring
    the merge broke.
    """
    seen: dict = {}

    class Recorder:
        name = "recorder"

        def build_step(
            self,
            settings: Settings,
            *,
            prompt: str,
            user_id: str,
            evidence: List[Evidence],
            model: Optional[str] = None,
            effort: Optional[str] = None,
            thinking: bool = False,
        ) -> ModelStep:
            seen.update(
                prompt=prompt,
                user_id=user_id,
                model=model,
                effort=effort,
                thinking=thinking,
            )
            return lambda items, tools, instructions: iter(())

    assert _shape(inspect.signature(Recorder.build_step)) == _shape(
        inspect.signature(Harness.build_step)
    )
    monkeypatch.setattr(agent_loop, "resolve_harness", lambda settings: Recorder())

    run = Run(
        prompt="who owns the launch?",
        created_by="alice",
        requested_model="",
        requested_effort="",
        show_thinking=True,
    )
    step = agent_loop._default_model_step(_openai_settings(), run, _evidence())

    assert callable(step)
    assert seen == {
        "prompt": "who owns the launch?",
        "user_id": "alice",
        # "" is the unset convention; the harness sees None and falls back.
        "model": None,
        "effort": None,
        "thinking": True,
    }
