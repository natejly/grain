"""`DEV_UNRESTRICTED_AGENT`: every tool, nothing parked — and only in development.

The gate is the feature. This flag removes the containment prompt injection has
to get past: it drops the per-subject registry scoping and lets every write
execute without a human seeing it. So the first and most important test here is
not what it does when it is on, but that it *cannot be switched on* outside
development — a deployment that sets the variable fails to boot with a legible
message rather than coming up quietly unguarded.

That posture is not theoretical caution. `app_env` itself defaults to
"production" in this codebase because a deployment that merely forgot to set it
once came up with the doors open.

Two things the flag deliberately does NOT do, each asserted below:

- a `tool_policies` row of `deny` still denies. "No restrictions" means "stop
  asking me", not "ignore what I told you", and a prohibition has never been a
  grant anywhere else here either;
- it never reaches workflow scope. An unattended scheduled run parks on writes
  in development exactly as it does in deployment, because that is the behaviour
  being developed against.
"""
from __future__ import annotations

import uuid
from typing import Any, List, Tuple

import pytest
from conftest import create_identity
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models import Agent, AgentToolCall, Conversation, Run, ToolPolicy, WorkflowRun
from app.services import agent_loop, subjects
from app.services.agent_loop import ASK_WRITES, AUTO_WRITES, approval_mode_for_run
from app.services.llm_tools import ToolResult, ToolSpec


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        model_provider="openai",
        openai_api_key=SecretStr("test-key"),
        **overrides,
    )


# --------------------------------------------------------------------------
# The gate


@pytest.mark.parametrize("environment", ["production", "staging", ""])
def test_the_flag_refuses_to_construct_outside_development(environment: str) -> None:
    """The whole point. Not "defaults to off" — cannot be switched on."""
    with pytest.raises(ValidationError) as caught:
        _settings(app_env=environment, dev_unrestricted_agent=True)
    assert "development or test" in str(caught.value)
    assert "DEV_UNRESTRICTED_AGENT" in str(caught.value)


@pytest.mark.parametrize("environment", ["development", "test"])
def test_the_flag_does_construct_in_development(environment: str) -> None:
    """The positive control: a guard that refused everything would pass above."""
    assert _settings(app_env=environment, dev_unrestricted_agent=True).dev_unrestricted_agent


def test_the_default_is_off_even_in_development() -> None:
    assert _settings(app_env="development").dev_unrestricted_agent is False
    assert _settings(app_env="test").dev_unrestricted_agent is False


# --------------------------------------------------------------------------
# What it does when it is on


class _Response:
    def __init__(self, output: Any = None, output_text: str = "done") -> None:
        self.output = output or []
        self.output_text = output_text


def _call(name: str, arguments: str = "{}", call_id: str = "call-1") -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        type="function_call", name=name, call_id=call_id, arguments=arguments
    )


def _probe() -> ToolSpec:
    return ToolSpec(
        name="probe_write",
        description="A write-capable probe.",
        parameters={"type": "object", "properties": {}},
        executor=lambda db, context, args: ToolResult(content="wrote"),
        read_only=False,
    )


@pytest.fixture
def scoped_thread():
    """A workspace, an agent, and a conversation scoped to a project subject."""
    identity = create_identity(workspace_name="Unrestricted")
    db = SessionLocal()
    try:
        agent_id = db.scalar(
            select(Agent.id).where(Agent.workspace_id == identity.workspace_id)
        )
        conversation = Conversation(
            workspace_id=identity.workspace_id,
            created_by=identity.user_id,
            title="Scoped",
            subject_kind=subjects.PROJECT,
            # A subject id that resolves to nothing: this fixture is about the
            # registry and the park, and a real project would only add rows.
            subject_id=uuid.uuid4().hex,
        )
        db.add(conversation)
        db.flush()
        run = Run(
            workspace_id=identity.workspace_id,
            conversation_id=conversation.id,
            agent_id=agent_id,
            created_by=identity.user_id,
            status="running",
            prompt="write it down",
        )
        db.add(run)
        db.commit()
        return identity, conversation.id, run.id
    finally:
        db.close()


def _turn(run_id: str, *, output: Any = None) -> Tuple[List[Any], Any]:
    """Run one turn with the probe registry, returning (tools offered, result)."""
    seen: List[Tuple[Any, str]] = []

    def model_step(input_items, tools, instructions):
        # The call is offered once. A step that re-offered it every iteration
        # would spin to the loop's budget instead of finishing, which is a
        # property of the double rather than of the code under test.
        first = not seen
        seen.append((tools, instructions))
        return [("completed", _Response(output=output if first else None))]

    db = SessionLocal()
    try:
        result = agent_loop.run_agent_turn(
            db, db.get(Run, run_id), evidence=[], model_step=model_step
        )
    finally:
        db.close()
    return seen[0][0], result


def test_on_the_subject_scoping_is_bypassed(monkeypatch, scoped_thread) -> None:
    """A project thread is offered the document tools too — every tool, as asked."""
    _identity, _conversation_id, run_id = scoped_thread
    monkeypatch.setattr(
        agent_loop, "get_settings", lambda: _settings(
            app_env="development", dev_unrestricted_agent=True
        )
    )
    tools, _result = _turn(run_id)
    offered = {tool["name"] for tool in tools if tool.get("type") == "function"}
    assert {"fs_write", "edit_document", "create_dashboard"} <= offered


def test_off_the_same_thread_is_scoped(scoped_thread) -> None:
    """The control for the test above, on the same fixture and the same run."""
    _identity, _conversation_id, run_id = scoped_thread
    tools, _result = _turn(run_id)
    offered = {tool["name"] for tool in tools if tool.get("type") == "function"}
    assert "fs_write" in offered
    assert "edit_document" not in offered and "create_dashboard" not in offered


def test_on_a_write_executes_without_parking(monkeypatch, scoped_thread) -> None:
    """Nothing parks. The run finishes rather than waiting on an approval."""
    identity, _conversation_id, run_id = scoped_thread
    monkeypatch.setattr(
        agent_loop, "get_settings", lambda: _settings(
            app_env="development", dev_unrestricted_agent=True
        )
    )
    monkeypatch.setattr(
        agent_loop,
        "build_registry",
        lambda db, context, allowed=None: {"probe_write": _probe()},
    )
    _tools, result = _turn(run_id, output=[_call("probe_write")])
    assert result is not None and result.answer == "done"
    db = SessionLocal()
    try:
        call = db.scalar(select(AgentToolCall).where(AgentToolCall.run_id == run_id))
        assert call.status == "succeeded"
        # And the trail says what actually authorised it. A row naming a *user*
        # as the decider of a write nobody looked at would be worse than none.
        assert call.approved_by_mode == AUTO_WRITES
        assert db.get(Run, run_id).status != "waiting_for_approval"
    finally:
        db.close()
    assert identity  # fixture sanity


def test_off_the_same_write_parks(scoped_thread) -> None:
    """The control. Without the flag this exact call waits for a person."""
    _identity, _conversation_id, run_id = scoped_thread
    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(
            agent_loop,
            "build_registry",
            lambda db, context, allowed=None: {"probe_write": _probe()},
        )
        _tools, result = _turn(run_id, output=[_call("probe_write")])
    finally:
        monkeypatch.undo()
    assert result is None  # parked
    db = SessionLocal()
    try:
        assert db.get(Run, run_id).status == "waiting_for_approval"
    finally:
        db.close()


def test_on_a_deny_row_still_denies(monkeypatch, scoped_thread) -> None:
    """A prohibition is not a grant, in this mode as in every other."""
    identity, _conversation_id, run_id = scoped_thread
    db = SessionLocal()
    try:
        db.add(
            ToolPolicy(
                workspace_id=identity.workspace_id,
                tool_name="probe_write",
                policy="deny",
                scope="chat",
            )
        )
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(
        agent_loop, "get_settings", lambda: _settings(
            app_env="development", dev_unrestricted_agent=True
        )
    )
    monkeypatch.setattr(
        agent_loop,
        "build_registry",
        lambda db, context, allowed=None: {"probe_write": _probe()},
    )
    _tools, result = _turn(run_id, output=[_call("probe_write")])
    assert result is not None
    db = SessionLocal()
    try:
        assert db.scalar(
            select(AgentToolCall).where(AgentToolCall.run_id == run_id)
        ).status == "denied"
    finally:
        db.close()


def test_on_a_workflow_run_still_parks_on_writes(monkeypatch, scoped_thread) -> None:
    """The flag must not leak into unattended execution.

    There are two independent locks — `approval_mode_for_run` returns
    `ask_writes` for anything that is not a chat run, and `evaluate_policy`
    ignores the mode unless the scope is chat. This asserts the composed result
    on a run the executor owns, which is the shape a 3am schedule actually has.
    """
    _identity, _conversation_id, run_id = scoped_thread
    monkeypatch.setattr(
        agent_loop, "get_settings", lambda: _settings(
            app_env="development", dev_unrestricted_agent=True
        )
    )
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        # A cron-dispatched run: `policy_scope_for_run` reads this as workflow
        # scope, which is the door the flag must not come through.
        run.cron_id = uuid.uuid4().hex
        db.commit()
        settings = _settings(app_env="development", dev_unrestricted_agent=True)
        scope = agent_loop.policy_scope_for_run(db, run)
        assert scope == agent_loop.WORKFLOW_SCOPE
        assert approval_mode_for_run(db, run, scope=scope, settings=settings) == ASK_WRITES
        verdict = agent_loop.evaluate_policy(
            db,
            workspace_id=run.workspace_id,
            spec=_probe(),
            scope=scope,
            # Even handed the bypass explicitly, which is the second lock.
            mode=AUTO_WRITES,
        )
        assert verdict.policy == "ask"
    finally:
        db.close()


def test_the_flag_is_read_per_call_not_per_process(scoped_thread) -> None:
    """`get_settings` is cached, so the flag is a boot-time fact — but the mode
    is still resolved per tool call, which is what lets the *conversation's* own
    mode keep working underneath it when the flag is off."""
    _identity, conversation_id, run_id = scoped_thread
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        conversation = db.get(Conversation, conversation_id)
        conversation.approval_mode = AUTO_WRITES
        db.commit()
        assert approval_mode_for_run(
            db, run, scope=agent_loop.CHAT_SCOPE, settings=get_settings()
        ) == AUTO_WRITES
    finally:
        db.close()
    assert WorkflowRun  # imported for the workflow-scope reasoning above
