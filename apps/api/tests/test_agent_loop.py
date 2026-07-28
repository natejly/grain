from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.models import AgentToolCall, Run, RunEvent
from app.services.agent_loop import run_agent_turn


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
                return FakeResponse(
                    output=[_function_call("search_sources", {"query": "Atlas"})]
                )
            if len(steps) == 2:
                return FakeResponse(
                    output=[_function_call("list_datasets", {}, call_id="call-2")]
                )
            return FakeResponse(output_text="Atlas is covered in the sources.")

        result = run_agent_turn(
            db,
            run,
            evidence=[],
            model_step=model_step,
        )
        assert result.answer == "Atlas is covered in the sources."
        assert len(steps) == 3

        calls = list(
            db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).all()
        )
        assert {call.name for call in calls} == {"search_sources", "list_datasets"}
        assert all(call.status == "succeeded" for call in calls)

        events = [
            event.event_type
            for event in db.query(RunEvent).filter(RunEvent.run_id == run_id).all()
        ]
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
                return FakeResponse(output=[_function_call("bogus_tool", {})])
            return FakeResponse(output_text="Recovered gracefully.")

        result = run_agent_turn(db, run, evidence=[], model_step=model_step)
        assert result.answer == "Recovered gracefully."
        assert any("unknown tool" in output for output in seen_outputs)
        call = (
            db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).one()
        )
        assert call.status == "failed"
    finally:
        db.close()


def test_agent_loop_iteration_budget(client):
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)

        def model_step(input_items, tools, instructions):
            return FakeResponse(
                output=[_function_call("list_datasets", {}, call_id="loop")]
            )

        with pytest.raises(RuntimeError):
            run_agent_turn(db, run, evidence=[], model_step=model_step)
    finally:
        db.close()
