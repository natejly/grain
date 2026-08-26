from __future__ import annotations

import json
import os

import pytest
from conftest import ask_before_writes
from pydantic import ValidationError

from app.config import Settings
from app.database import SessionLocal
from app.models import AgentToolCall, Run
from app.services.agent_loop import resume_agent_turn, run_agent_turn
from app.services.artifacts import documents
from app.services.retrieval import Evidence
from app.services.scripted_model import ScriptError, load_script, scripted_model_step

SCRIPT = [
    {
        "match": "write the checklist",
        "steps": [
            {
                "call": {
                    "name": "create_document",
                    "arguments": {
                        "title": "Checklist",
                        "content": "One.\nTwo.\n",
                        "kind": "markdown",
                    },
                }
            },
            {"text": "Wrote the checklist.", "denied_text": "Left it alone."},
        ],
    },
    {
        "match": "dana reyes leads atlas",
        "memories": [
            {"kind": "fact", "content": "Dana Reyes leads Atlas.", "entities": ["Atlas"]}
        ],
    },
]


@pytest.fixture
def script_path(tmp_path):
    path = tmp_path / "script.json"
    path.write_text(json.dumps(SCRIPT))
    return path


@pytest.fixture
def scripted_settings(script_path):
    return Settings(
        _env_file=None,
        app_env="test",
        model_provider="scripted",
        scripted_model_script=script_path,
    )


def _run(client, prompt: str):
    """A live run row plus the session that owns it."""
    identity = client.get("/api/bootstrap").json()
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "scripted-" + os.urandom(6).hex()},
        json={"title": "Scripted"},
    ).json()
    ask_before_writes(client, conversation["id"])
    db = SessionLocal()
    run = Run(
        workspace_id=identity["identity"]["workspace_id"],
        conversation_id=conversation["id"],
        agent_id=identity["default_agent_id"],
        created_by=identity["identity"]["user_id"],
        status="running",
        prompt=prompt,
    )
    db.add(run)
    db.commit()
    return db, run


def test_scripted_provider_is_refused_outside_development():
    with pytest.raises(ValidationError, match="APP_ENV"):
        Settings(
            _env_file=None,
            app_env="production",
            model_provider="scripted",
            scripted_model_script="script.json",
        )


def test_scripted_provider_requires_a_script():
    # Explicit None: the suite itself exports SCRIPTED_MODEL_SCRIPT, and env vars
    # would otherwise supply the very value this asserts is required.
    with pytest.raises(ValidationError, match="SCRIPTED_MODEL_SCRIPT"):
        Settings(
            _env_file=None,
            app_env="test",
            model_provider="scripted",
            scripted_model_script=None,
        )


def test_scripted_settings_select_the_scripted_provider(scripted_settings):
    assert scripted_settings.active_model_provider == "scripted"


def test_malformed_scripts_are_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([{"match": "hi"}]))
    with pytest.raises(ScriptError, match="scripts nothing"):
        load_script(path)
    path.write_text(json.dumps([{"match": "hi", "steps": []}]))
    with pytest.raises(ScriptError, match="no steps"):
        load_script(path)
    path.write_text(json.dumps([{"steps": [{"text": "hi"}]}]))
    with pytest.raises(ScriptError, match="no match"):
        load_script(path)


def test_unmatched_prompts_fall_back_to_quoting_the_evidence(scripted_settings):
    evidence = [
        Evidence(
            chunk_id="c1",
            source_id="s1",
            filename="brief.md",
            ordinal=0,
            excerpt="Maya owns the launch.",
            score=1.0,
        )
    ]
    step = scripted_model_step(
        scripted_settings, prompt="something else entirely", evidence=evidence
    )
    text = "".join(
        str(value) for kind, value in step([], [], "") if kind == "delta"
    )
    assert text == "- Maya owns the launch. [1]"


def test_memory_extraction_is_scripted_per_prompt(scripted_settings):
    """extract_memories is a model call, so the double has to stand in for it too."""
    from app.services.model import extract_memories

    extracted = extract_memories(
        "Note that Dana Reyes leads Atlas now.",
        "Noted.",
        user_id="u",
        settings=scripted_settings,
    )
    assert extracted == [
        {"kind": "fact", "content": "Dana Reyes leads Atlas.", "entities": ["Atlas"]}
    ]
    # An entry that only scripts a chat turn extracts nothing, and neither does a
    # prompt no entry covers: silence is a legitimate extraction result.
    assert (
        extract_memories(
            "Please write the checklist", "Done.", user_id="u", settings=scripted_settings
        )
        == []
    )
    assert (
        extract_memories("Unrelated.", "Ok.", user_id="u", settings=scripted_settings)
        == []
    )


def test_approved_call_parks_then_applies_the_write(client, scripted_settings):
    db, run = _run(client, "Please write the checklist for me")
    try:
        assert (
            run_agent_turn(db, run, evidence=[], settings=scripted_settings) is None
        )
        assert run.status == "waiting_for_approval"
        call = (
            db.query(AgentToolCall)
            .filter(AgentToolCall.run_id == run.id, AgentToolCall.status == "proposed")
            .one()
        )
        assert call.name == "create_document"
        # The card shows the real diff, produced by the tool's own preview.
        assert "+One." in call.proposal_preview

        result = resume_agent_turn(
            db,
            run,
            tool_call_id=call.id,
            decision="approved",
            settings=scripted_settings,
        )
        assert result is not None
        assert result.answer == "Wrote the checklist."
        assert (
            documents.find_by_title(
                db, workspace_id=run.workspace_id, title="Checklist"
            )
            is not None
        )
    finally:
        db.close()


def test_denied_call_answers_around_the_refusal_without_writing(
    client, scripted_settings
):
    db, run = _run(client, "Now write the checklist again")
    try:
        run_agent_turn(db, run, evidence=[], settings=scripted_settings)
        call = (
            db.query(AgentToolCall)
            .filter(AgentToolCall.run_id == run.id, AgentToolCall.status == "proposed")
            .one()
        )
        before = documents.list_documents(db, workspace_id=run.workspace_id)
        result = resume_agent_turn(
            db,
            run,
            tool_call_id=call.id,
            decision="denied",
            settings=scripted_settings,
        )
        assert result is not None
        assert result.answer == "Left it alone."
        after = documents.list_documents(db, workspace_id=run.workspace_id)
        assert len(after) == len(before)
    finally:
        db.close()
