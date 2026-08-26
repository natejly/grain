"""An authored agent's system prompt and tool subset reach the loop — and
survive a park.

The `ModelStep` seam makes the interesting assertion cheap: the third argument
to every scripted step *is* the system prompt the model would have been given,
so these tests read it directly instead of inferring it from behaviour. The
resume test is the one that matters most: instructions and registry are
resolved again on the resume path, from the persisted `run.agent_id`, and the
turn must come back as the same agent it parked as.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

from conftest import create_identity

from app.database import SessionLocal
from app.models import Agent, AgentToolCall, Conversation, Run, ToolPolicy
from app.services import agent_loop
from app.services.agent_loop import (
    resolve_directives,
    resume_agent_turn,
    run_agent_turn,
)
from app.services.llm_tools import ToolContext, ToolResult, ToolSpec, build_registry
from app.services.model import CHAT_INSTRUCTIONS


class FakeResponse:
    def __init__(self, output=None, output_text=""):
        self.output = output or []
        self.output_text = output_text


def _function_call(name: str, arguments: dict, call_id: str = "call-1"):
    return SimpleNamespace(
        type="function_call",
        name=name,
        call_id=call_id,
        arguments=json.dumps(arguments),
    )


def _capture_step(seen: List[Tuple[Any, str]], *, output=None, output_text="done"):
    """A one-turn scripted model that records (tools, instructions)."""

    def model_step(input_items, tools, instructions):
        seen.append((tools, instructions))
        return [("completed", FakeResponse(output=output, output_text=output_text))]

    return model_step


def _workspace_with_agent(
    *,
    instructions: str = "You are Fern. Answer as Fern would.",
    allowed_tools_json: str = "",
    enabled: bool = True,
) -> Tuple[str, str, str]:
    """(workspace_id, user_id, agent_id) — a fresh tenant plus one custom agent."""
    identity = create_identity(name="Directive owner", workspace_name="Directives")
    db = SessionLocal()
    try:
        agent = Agent(
            workspace_id=identity.workspace_id,
            name="Fern",
            instructions=instructions,
            allowed_tools_json=allowed_tools_json,
            enabled=enabled,
        )
        db.add(agent)
        db.commit()
        return identity.workspace_id, identity.user_id, agent.id
    finally:
        db.close()


def _run_as(workspace_id: str, user_id: str, agent_id: str, prompt: str = "hi") -> str:
    db = SessionLocal()
    try:
        # Pinned, not defaulted: these directives are asserted through what
        # parks, so the thread has to be one that parks.
        conversation = Conversation(
            workspace_id=workspace_id, created_by=user_id, approval_mode="ask_writes"
        )
        db.add(conversation)
        db.flush()
        run = Run(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            agent_id=agent_id,
            created_by=user_id,
            status="running",
            prompt=prompt,
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


# --------------------------------------------------------------------------
# resolve_directives: the fallback matrix
# --------------------------------------------------------------------------


def _directives_for(agent_kwargs: Dict[str, Any]) -> agent_loop.AgentDirectives:
    workspace_id, user_id, agent_id = _workspace_with_agent(**agent_kwargs)
    run_id = _run_as(workspace_id, user_id, agent_id)
    db = SessionLocal()
    try:
        return resolve_directives(db, db.get(Run, run_id))
    finally:
        db.close()


def test_an_authored_prompt_and_subset_resolve() -> None:
    directives = _directives_for(
        {
            "instructions": "Answer only in haiku.",
            "allowed_tools_json": '["search_sources", "recall_memory"]',
        }
    )
    assert directives.instructions == "Answer only in haiku."
    assert directives.allowed == frozenset({"search_sources", "recall_memory"})


def test_blank_instructions_fall_back_to_stock() -> None:
    directives = _directives_for({"instructions": "   "})
    assert directives.instructions == CHAT_INSTRUCTIONS


def test_unset_and_empty_subsets_differ() -> None:
    assert _directives_for({"allowed_tools_json": ""}).allowed is None
    assert _directives_for({"allowed_tools_json": "[]"}).allowed == frozenset()


def test_a_corrupt_allowlist_degrades_to_all_tools() -> None:
    for raw in ("{not json", '{"a": 1}', '"just a string"'):
        assert _directives_for({"allowed_tools_json": raw}).allowed is None


def test_a_disabled_agent_still_resolves_for_its_existing_runs() -> None:
    directives = _directives_for(
        {"instructions": "Speak plainly.", "enabled": False}
    )
    assert directives.instructions == "Speak plainly."


def test_a_run_with_no_agent_gets_stock_directives() -> None:
    workspace_id, user_id, agent_id = _workspace_with_agent()
    run_id = _run_as(workspace_id, user_id, agent_id)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        run.agent_id = ""
        directives = resolve_directives(db, run)
    finally:
        db.close()
    assert directives.instructions == CHAT_INSTRUCTIONS
    assert directives.allowed is None


# --------------------------------------------------------------------------
# build_registry: the intersection
# --------------------------------------------------------------------------


def test_the_subset_is_a_pure_intersection() -> None:
    workspace_id, user_id, _ = _workspace_with_agent()
    context = ToolContext(
        workspace_id=workspace_id, user_id=user_id, conversation_id=""
    )
    db = SessionLocal()
    try:
        assert set(
            build_registry(db, context, allowed={"search_sources", "no_such_tool"})
        ) == {"search_sources"}
        assert build_registry(db, context, allowed=frozenset()) == {}
        everything = build_registry(db, context)
        assert set(build_registry(db, context, allowed=set(everything))) == set(
            everything
        )
    finally:
        db.close()


# --------------------------------------------------------------------------
# The loop: instructions reach the model, the subset reaches the tool payload
# --------------------------------------------------------------------------


def test_the_agents_prompt_is_what_the_model_is_given() -> None:
    workspace_id, user_id, agent_id = _workspace_with_agent(
        instructions="Answer only in haiku.",
        allowed_tools_json='["search_sources"]',
    )
    run_id = _run_as(workspace_id, user_id, agent_id)
    seen: List[Tuple[Any, str]] = []
    db = SessionLocal()
    try:
        result = run_agent_turn(
            db, db.get(Run, run_id), evidence=[], model_step=_capture_step(seen)
        )
    finally:
        db.close()
    assert result is not None
    (tools, instructions), = seen
    assert instructions == "Answer only in haiku."
    # Local functions only: hosted tools (web search) join the payload without
    # joining the registry, and are not the subset's business.
    functions = [tool["name"] for tool in tools if tool.get("type") == "function"]
    assert functions == ["search_sources"]


def test_a_parked_turn_resumes_as_the_same_agent(monkeypatch) -> None:
    workspace_id, user_id, agent_id = _workspace_with_agent(
        instructions="You are the auditor.",
        allowed_tools_json='["probe_write"]',
    )

    probe = ToolSpec(
        name="probe_write",
        description="A write-capable probe.",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        executor=lambda db, context, args: ToolResult(content="wrote"),
        read_only=False,
    )

    def fake_registry(db, context, allowed=None):
        registry = {"probe_write": probe}
        if allowed is None:
            return registry
        return {k: v for k, v in registry.items() if k in set(allowed)}

    monkeypatch.setattr(agent_loop, "build_registry", fake_registry)

    db = SessionLocal()
    try:
        db.add(
            ToolPolicy(
                workspace_id=workspace_id, tool_name="probe_write", policy="ask"
            )
        )
        db.commit()
    finally:
        db.close()

    run_id = _run_as(workspace_id, user_id, agent_id, prompt="write it down")
    first: List[Tuple[Any, str]] = []
    db = SessionLocal()
    try:
        parked = run_agent_turn(
            db,
            db.get(Run, run_id),
            evidence=[],
            model_step=_capture_step(
                first, output=[_function_call("probe_write", {"text": "x"})]
            ),
        )
        assert parked is None  # waiting on the approval

        call = db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).one()
        second: List[Tuple[Any, str]] = []
        result = resume_agent_turn(
            db,
            db.get(Run, run_id),
            tool_call_id=call.id,
            decision="approve",
            model_step=_capture_step(second, output_text="all done"),
        )
    finally:
        db.close()

    assert result is not None and result.answer == "all done"
    # Both halves of the turn — before the park and after — ran under the same
    # authored prompt and the same one-tool registry.
    assert first[0][1] == "You are the auditor."
    assert second[0][1] == "You are the auditor."
    assert [
        tool["name"] for tool in first[0][0] if tool.get("type") == "function"
    ] == ["probe_write"]
