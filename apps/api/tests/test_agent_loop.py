from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.models import AgentToolCall, Run, RunEvent, ToolPolicy
from app.services.agent_loop import resume_agent_turn, run_agent_turn


class FakeResponse:
    def __init__(self, output=None, output_text=""):
        self.output = output or []
        self.output_text = output_text


def _completed(output=None, output_text=""):
    """One scripted model turn: no deltas, then the terminal response."""
    return [("completed", FakeResponse(output=output, output_text=output_text))]


def _function_call(name: str, arguments: dict, call_id: str = "call-1"):
    return SimpleNamespace(
        type="function_call",
        name=name,
        call_id=call_id,
        arguments=json.dumps(arguments),
    )


def _make_run(client) -> str:
    identity = client.get("/api/bootstrap").json()["identity"]
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "agent-loop-conversation-" + identity["user_id"][:4]},
        json={"title": "Agent loop"},
    ).json()
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=identity["workspace_id"],
            conversation_id=conversation["id"],
            agent_id=client.get("/api/bootstrap").json()["default_agent_id"],
            created_by=identity["user_id"],
            status="running",
            prompt="What is in the sources about Atlas?",
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _set_policy(db, workspace_id: str, tool_name: str, policy: str) -> None:
    existing = (
        db.query(ToolPolicy)
        .filter(
            ToolPolicy.workspace_id == workspace_id,
            ToolPolicy.tool_name == tool_name,
        )
        .one_or_none()
    )
    if existing is None:
        db.add(
            ToolPolicy(
                workspace_id=workspace_id, tool_name=tool_name, policy=policy
            )
        )
    else:
        existing.policy = policy
    db.commit()


def _events(db, run_id: str) -> list[str]:
    return [
        event.event_type
        for event in db.query(RunEvent).filter(RunEvent.run_id == run_id).all()
    ]


def test_agent_loop_executes_tools_and_returns_answer(client):
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        steps = []

        def model_step(input_items, tools, instructions):
            steps.append(len(input_items))
            if len(steps) == 1:
                assert any(tool["name"] == "search_sources" for tool in tools)
                return _completed(
                    output=[_function_call("search_sources", {"query": "Atlas"})]
                )
            if len(steps) == 2:
                return _completed(
                    output=[_function_call("list_datasets", {}, call_id="call-2")]
                )
            return _completed(output_text="Atlas is covered in the sources.")

        result = run_agent_turn(db, run, evidence=[], model_step=model_step)
        assert result is not None
        assert result.answer == "Atlas is covered in the sources."
        assert len(steps) == 3

        calls = list(
            db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).all()
        )
        assert {call.name for call in calls} == {"search_sources", "list_datasets"}
        assert all(call.status == "succeeded" for call in calls)

        events = _events(db, run_id)
        assert events.count("tool.started") == 2
        assert events.count("tool.completed") == 2
    finally:
        db.close()


def test_agent_loop_surfaces_unknown_tool_as_result(client):
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        seen_outputs = []

        def model_step(input_items, tools, instructions):
            for item in input_items:
                if isinstance(item, dict) and item.get("type") == "function_call_output":
                    seen_outputs.append(item["output"])
            if not seen_outputs:
                return _completed(output=[_function_call("bogus_tool", {})])
            return _completed(output_text="Recovered gracefully.")

        result = run_agent_turn(db, run, evidence=[], model_step=model_step)
        assert result is not None
        assert result.answer == "Recovered gracefully."
        assert any("unknown tool" in output for output in seen_outputs)
        call = db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).one()
        assert call.status == "failed"
    finally:
        db.close()


def test_agent_loop_iteration_budget(client):
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)

        def model_step(input_items, tools, instructions):
            return _completed(
                output=[_function_call("list_datasets", {}, call_id="loop")]
            )

        with pytest.raises(RuntimeError):
            run_agent_turn(db, run, evidence=[], model_step=model_step)
    finally:
        db.close()


def test_agent_loop_streams_text_deltas(client):
    """Deltas reach the event log as they arrive and compose the final answer."""
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)

        def model_step(input_items, tools, instructions):
            return [
                ("delta", "Atlas is "),
                ("delta", "covered."),
                ("completed", FakeResponse(output_text="ignored fallback")),
            ]

        result = run_agent_turn(db, run, evidence=[], model_step=model_step)
        assert result is not None
        assert result.answer == "Atlas is covered."

        deltas = [
            json.loads(event.payload_json)["delta"]
            for event in db.query(RunEvent)
            .filter(RunEvent.run_id == run_id, RunEvent.event_type == "message.delta")
            .order_by(RunEvent.sequence.asc())
            .all()
        ]
        assert "".join(deltas) == "Atlas is covered."
    finally:
        db.close()


def test_agent_loop_parks_on_ask_policy_then_resumes(client):
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        _set_policy(db, run.workspace_id, "list_datasets", "ask")

        def model_step(input_items, tools, instructions):
            if not any(
                isinstance(item, dict) and item.get("type") == "function_call_output"
                for item in input_items
            ):
                return _completed(
                    output=[_function_call("list_datasets", {}, call_id="ask-1")]
                )
            return _completed(output_text="Here are your datasets.")

        assert run_agent_turn(db, run, evidence=[], model_step=model_step) is None

        db.refresh(run)
        assert run.status == "waiting_for_approval"
        assert run.agent_state_json  # state survives for another process
        proposed = (
            db.query(AgentToolCall)
            .filter(AgentToolCall.run_id == run_id, AgentToolCall.status == "proposed")
            .one()
        )
        assert proposed.name == "list_datasets"
        assert proposed.call_id == "ask-1"
        assert "tool.proposed" in _events(db, run_id)

        result = resume_agent_turn(
            db,
            run,
            tool_call_id=proposed.id,
            decision="approved",
            model_step=model_step,
        )
        assert result is not None
        assert result.answer == "Here are your datasets."
        db.refresh(proposed)
        assert proposed.status == "succeeded"
        assert proposed.decided_at is not None
        db.refresh(run)
        assert run.agent_state_json is None
    finally:
        _cleanup_policy(run_id)
        db.close()


def test_agent_loop_denied_call_lets_the_model_answer(client):
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        _set_policy(db, run.workspace_id, "list_datasets", "ask")
        seen_outputs = []

        def model_step(input_items, tools, instructions):
            for item in input_items:
                if isinstance(item, dict) and item.get("type") == "function_call_output":
                    seen_outputs.append(item["output"])
            if not seen_outputs:
                return _completed(
                    output=[_function_call("list_datasets", {}, call_id="deny-1")]
                )
            return _completed(output_text="I could not list your datasets.")

        assert run_agent_turn(db, run, evidence=[], model_step=model_step) is None
        proposed = (
            db.query(AgentToolCall)
            .filter(AgentToolCall.run_id == run_id, AgentToolCall.status == "proposed")
            .one()
        )
        result = resume_agent_turn(
            db,
            run,
            tool_call_id=proposed.id,
            decision="denied",
            model_step=model_step,
        )
        assert result is not None
        assert result.answer == "I could not list your datasets."
        assert any("denied this tool call" in output for output in seen_outputs)
        db.refresh(proposed)
        assert proposed.status == "denied"
    finally:
        _cleanup_policy(run_id)
        db.close()


def test_agent_loop_deny_policy_never_executes(client):
    """policy=deny short-circuits without ever parking the run."""
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        _set_policy(db, run.workspace_id, "list_datasets", "deny")
        seen_outputs = []

        def model_step(input_items, tools, instructions):
            for item in input_items:
                if isinstance(item, dict) and item.get("type") == "function_call_output":
                    seen_outputs.append(item["output"])
            if not seen_outputs:
                return _completed(
                    output=[_function_call("list_datasets", {}, call_id="never-1")]
                )
            return _completed(output_text="Blocked by policy.")

        result = run_agent_turn(db, run, evidence=[], model_step=model_step)
        assert result is not None
        assert result.answer == "Blocked by policy."
        db.refresh(run)
        assert run.status != "waiting_for_approval"
        assert any("denied this tool call" in output for output in seen_outputs)
    finally:
        _cleanup_policy(run_id)
        db.close()


def test_agent_loop_honours_cancel_between_iterations(client):
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        run.cancel_requested = True
        db.commit()

        def model_step(input_items, tools, instructions):
            raise AssertionError("the model must not be called after cancellation")

        assert run_agent_turn(db, run, evidence=[], model_step=model_step) is None
        db.refresh(run)
        assert run.status == "cancelled"
        assert "run.cancelled" in _events(db, run_id)
    finally:
        db.close()


def _cleanup_policy(run_id: str) -> None:
    """Policies are workspace-scoped and the test workspace is shared."""
    db = SessionLocal()
    try:
        db.query(ToolPolicy).delete()
        db.commit()
    finally:
        db.close()
