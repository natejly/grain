"""Loop-level coverage for mid-turn steering absorption.

`POST /api/runs/{id}/steer` appends `run.steer` events; `_absorb_steering`
folds them into the transcript at the head of each iteration, and the cursor on
`LoopState.steered_sequence` guarantees a park/resume neither replays nor
drops one.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

from app.database import SessionLocal
from app.models import AgentToolCall, Conversation, Run, RunEvent, ToolPolicy
from app.services.agent_loop import (
    STEER_REQUESTED,
    LoopState,
    _absorb_steering,
    resume_agent_turn,
    run_agent_turn,
)
from app.services.events import append_event

STEER_MARKER = "[The user adds, mid-task]"


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


def _make_run(client) -> tuple[str, str, str]:
    """A running chat Run; returns (run_id, conversation_id, workspace_id)."""
    identity = client.get("/api/bootstrap").json()["identity"]
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "steer-conv-" + os.urandom(6).hex()},
        json={"title": "Steering"},
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
        return run.id, conversation["id"], identity["workspace_id"]
    finally:
        db.close()


def _steer(run_id: str, workspace_id: str, content: str) -> int:
    """Append a steer.requested event from its own session, as the endpoint does."""
    session = SessionLocal()
    try:
        event = append_event(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            event_type=STEER_REQUESTED,
            payload={"content": content},
        )
        session.commit()
        return event.sequence
    finally:
        session.close()


def _steer_items(input_items) -> list:
    return [
        item
        for item in input_items
        if isinstance(item, dict)
        and item.get("role") == "user"
        and STEER_MARKER in str(item.get("content") or "")
    ]


def _cleanup(run_id: str, conversation_id: str) -> None:
    """The test workspace is shared; leave no rows behind."""
    db = SessionLocal()
    try:
        db.query(ToolPolicy).delete()
        db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).delete()
        db.query(RunEvent).filter(RunEvent.run_id == run_id).delete()
        db.query(Run).filter(Run.id == run_id).delete()
        db.query(Conversation).filter(Conversation.id == conversation_id).delete()
        db.commit()
    finally:
        db.close()


def test_a_steer_sent_before_the_turn_reaches_the_first_model_call(client):
    run_id, conversation_id, workspace_id = _make_run(client)
    _steer(run_id, workspace_id, "Also check the dates")
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        seen: list = []

        def model_step(input_items, tools, instructions):
            seen.append(_steer_items(input_items))
            return _completed(output_text="Checked, dates included.")

        result = run_agent_turn(db, run, evidence=[], model_step=model_step)
        assert result is not None
        assert len(seen) == 1
        assert len(seen[0]) == 1
        assert (
            seen[0][0]["content"] == "[The user adds, mid-task]: Also check the dates"
        )
    finally:
        db.close()
        _cleanup(run_id, conversation_id)


def test_a_steer_arriving_mid_turn_reaches_the_very_next_model_call(client):
    """A steer written between iterations lands before the next step, not the
    next turn."""
    run_id, conversation_id, workspace_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        steps: list = []

        def model_step(input_items, tools, instructions):
            steps.append(_steer_items(input_items))
            if len(steps) == 1:
                assert steps[0] == []  # nothing steered yet
                # The user types mid-task, while the model's first tool call is
                # still queued — the event is written by its own session, as the
                # steer endpoint's request would be.
                _steer(run_id, workspace_id, "Prefer the Q3 numbers")
                return _completed(
                    output=[_function_call("list_datasets", {}, call_id="steer-1")]
                )
            return _completed(output_text="Using the Q3 numbers.")

        result = run_agent_turn(db, run, evidence=[], model_step=model_step)
        assert result is not None
        assert len(steps) == 2
        assert len(steps[1]) == 1
        assert "Prefer the Q3 numbers" in steps[1][0]["content"]
    finally:
        db.close()
        _cleanup(run_id, conversation_id)


def test_a_steer_arriving_while_parked_is_absorbed_exactly_once_on_resume(client):
    """The cursor round-trips through the persisted LoopState: a steer that
    arrived during the park is read once, and never re-injected."""
    run_id, conversation_id, workspace_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        db.add(
            ToolPolicy(
                workspace_id=workspace_id, tool_name="list_datasets", policy="ask"
            )
        )
        db.commit()
        steps: list = []

        def model_step(input_items, tools, instructions):
            steps.append(_steer_items(input_items))
            if len(steps) == 1:
                return _completed(
                    output=[_function_call("list_datasets", {}, call_id="park-1")]
                )
            if len(steps) == 2:
                # search_sources is allow by default, so the loop iterates again
                # without parking — the second head re-runs _absorb_steering.
                return _completed(
                    output=[
                        _function_call(
                            "search_sources", {"query": "Atlas"}, call_id="park-2"
                        )
                    ]
                )
            return _completed(output_text="Done, dates checked.")

        assert run_agent_turn(db, run, evidence=[], model_step=model_step) is None
        db.refresh(run)
        assert run.status == "waiting_for_approval"

        _steer(run_id, workspace_id, "Also check the dates")

        proposed = (
            db.query(AgentToolCall)
            .filter(AgentToolCall.run_id == run_id, AgentToolCall.status == "proposed")
            .one()
        )
        result = resume_agent_turn(
            db,
            run,
            tool_call_id=proposed.id,
            decision="approved",
            model_step=model_step,
        )
        assert result is not None
        assert result.answer == "Done, dates checked."
        assert len(steps) == 3
        # The resumed turn's next model call saw the steer exactly once...
        assert len(steps[1]) == 1
        assert "Also check the dates" in steps[1][0]["content"]
        # ...and the following iteration did not duplicate it: the cursor
        # advanced, so the same event is never folded in twice.
        assert len(steps[2]) == 1
    finally:
        db.close()
        _cleanup(run_id, conversation_id)


def test_blank_steer_content_is_skipped_but_still_advances_the_cursor(client):
    run_id, conversation_id, workspace_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        blank_sequence = _steer(run_id, workspace_id, "   ")

        state = LoopState()
        _absorb_steering(db, run, state)
        assert state.input_items == []
        assert state.steered_sequence == blank_sequence

        # A later, real steer still lands — the blank one consumed nothing but
        # its own sequence number.
        real_sequence = _steer(run_id, workspace_id, "Now the real request")
        _absorb_steering(db, run, state)
        assert len(_steer_items(state.input_items)) == 1
        assert "Now the real request" in state.input_items[0]["content"]
        assert state.steered_sequence == real_sequence

        # Absorbing again re-injects nothing: the cursor already passed it.
        _absorb_steering(db, run, state)
        assert len(_steer_items(state.input_items)) == 1
    finally:
        db.close()
        _cleanup(run_id, conversation_id)
