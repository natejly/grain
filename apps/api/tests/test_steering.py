"""Steering a live turn from the same text box, and streams that end early.

Two claims, proven rather than asserted:

- A note typed while a run is working becomes part of THAT turn: it lands in
  the transcript as an ordinary user message under the run, and the loop folds
  it into the very next model call. The `run.steer` event's per-run sequence
  is the cursor, persisted in LoopState, so a park/resume neither replays a
  note nor drops one sent while parked.
- A stream the provider cuts off (`response.incomplete`, usually the
  output-token ceiling) finishes the turn with everything that streamed —
  plus an honest cut-short note — instead of erasing it with an error. Only
  a stream that produced nothing at all still fails.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models import AgentToolCall, Message, Run, RunEvent
from app.services import events as events_service
from app.services.agent_loop import LoopState, _absorb_steering, run_agent_turn
from app.services.events import append_event

ANSWER = "Two sentences, as asked. Nothing more."


def _settings(**overrides: Any) -> Settings:
    return get_settings().model_copy(update=overrides)


def _key() -> dict[str, str]:
    return {"Idempotency-Key": f"steer-{uuid.uuid4().hex}"}


def _response(text: str = ANSWER) -> SimpleNamespace:
    return SimpleNamespace(
        output=[
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(type="output_text", text=text, annotations=[])
                ],
            )
        ],
        output_text=text,
    )


def _incomplete(text: str, reason: str = "max_output_tokens") -> SimpleNamespace:
    response = _response(text)
    response.incomplete_details = SimpleNamespace(reason=reason)
    return response


def _make_run(client) -> str:
    identity = client.get("/api/bootstrap").json()["identity"]
    conversation = client.post(
        "/api/conversations",
        headers=_key(),
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
            prompt="Summarize the thread.",
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


# --- the steer route --------------------------------------------------------


def test_steering_lands_in_the_transcript_and_the_event_log(client):
    run_id = _make_run(client)
    answer = client.post(
        f"/api/runs/{run_id}/steer",
        json={"content": "Shorter, please."},
        headers=_key(),
    )
    assert answer.status_code == 202, answer.text
    message = answer.json()["message"]
    assert message["role"] == "user"
    assert message["run_id"] == run_id
    assert answer.json()["run"] is None  # no new turn started
    db = SessionLocal()
    try:
        events = (
            db.query(RunEvent)
            .filter_by(run_id=run_id, event_type="run.steer")
            .all()
        )
        assert len(events) == 1
        assert json.loads(events[0].payload_json)["content"] == "Shorter, please."
        row = db.get(Message, message["id"])
        assert row is not None
        assert row.conversation_id != ""
    finally:
        db.close()


def test_a_finished_run_refuses_steering_with_a_conflict(client):
    """409, not 404: the composer uses the difference to fall back to an
    ordinary send rather than telling the user the run does not exist."""
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        run.status = "completed"
        db.commit()
    finally:
        db.close()
    refused = client.post(
        f"/api/runs/{run_id}/steer", json={"content": "Too late."}, headers=_key()
    )
    assert refused.status_code == 409
    assert "finished" in refused.text


# --- absorption -------------------------------------------------------------


def test_absorption_folds_notes_once_in_order_and_survives_a_resume(client):
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        for content in ("First note.", "Second note."):
            append_event(
                db,
                workspace_id=run.workspace_id,
                run_id=run_id,
                event_type="run.steer",
                payload={"content": content},
            )
        db.commit()
        state = LoopState(input_items=[{"role": "user", "content": "prompt"}])
        _absorb_steering(db, run, state)
        assert [item["content"] for item in state.input_items[1:]] == [
            "First note.",
            "Second note.",
        ]
        cursor = state.steered_sequence
        # Idempotent: the cursor, not the query, decides what is new.
        _absorb_steering(db, run, state)
        assert len(state.input_items) == 3
        assert state.steered_sequence == cursor
        # The cursor rides the serialized state, so a park/resume in another
        # process replays nothing…
        revived = LoopState.from_json(state.to_json())
        assert revived.steered_sequence == cursor
        _absorb_steering(db, run, revived)
        assert len(revived.input_items) == 3
        # …and a note sent while parked is picked up on resume.
        append_event(
            db,
            workspace_id=run.workspace_id,
            run_id=run_id,
            event_type="run.steer",
            payload={"content": "Sent while parked."},
        )
        db.commit()
        _absorb_steering(db, run, revived)
        assert revived.input_items[-1]["content"] == "Sent while parked."
    finally:
        db.close()


def test_a_note_reaches_the_very_next_model_call(client):
    run_id = _make_run(client)
    seen: list[list[Any]] = []
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        append_event(
            db,
            workspace_id=run.workspace_id,
            run_id=run_id,
            event_type="run.steer",
            payload={"content": "Keep it to one sentence."},
        )
        db.commit()

        def model_step(input_items, tools, instructions):
            seen.append(list(input_items))
            return [("delta", ANSWER), ("completed", _response())]

        result = run_agent_turn(
            db, run, evidence=[], settings=_settings(), model_step=model_step
        )
    finally:
        db.close()
    assert result is not None
    assert any(
        isinstance(item, dict)
        and item.get("role") == "user"
        and item.get("content") == "Keep it to one sentence."
        for item in seen[0]
    )


# --- incomplete streams -----------------------------------------------------


def test_a_cut_short_stream_keeps_what_streamed(client):
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None

        def model_step(input_items, tools, instructions):
            return [
                ("delta", "Half an answer, mid-"),
                ("incomplete", _incomplete("Half an answer, mid-")),
            ]

        result = run_agent_turn(
            db, run, evidence=[], settings=_settings(), model_step=model_step
        )
    finally:
        db.close()
    assert result is not None
    assert result.answer.startswith("Half an answer, mid-")
    assert "cut short" in result.answer
    assert "output limit" in result.answer


def test_a_note_during_the_final_model_call_still_lands_this_turn(client):
    """The finish-time check: a steer that arrives while the LAST model call
    is streaming (route says 202 — the run is still running) must not be
    silently dropped. The loop absorbs it at finish and answers it in the
    same turn rather than filing it away for a turn nobody started."""
    run_id = _make_run(client)
    prompts_seen: list[int] = []
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        workspace_id = run.workspace_id

        def model_step(input_items, tools, instructions):
            prompts_seen.append(len(input_items))
            if len(prompts_seen) == 1:
                # The steer lands WHILE this call streams: after the loop's
                # absorb checkpoint, before the response completes.
                append_event(
                    db,
                    workspace_id=workspace_id,
                    run_id=run_id,
                    event_type="run.steer",
                    payload={"content": "Also name the owner."},
                )
                db.flush()
                return [
                    ("delta", "The rollout is Monday."),
                    ("completed", _response("The rollout is Monday.")),
                ]
            return [
                ("delta", "The owner is the platform team."),
                ("completed", _response("The owner is the platform team.")),
            ]

        result = run_agent_turn(
            db, run, evidence=[], settings=_settings(), model_step=model_step
        )
    finally:
        db.close()
    assert result is not None
    # Two model calls: the second exists only because the late note demanded it.
    assert len(prompts_seen) == 2
    assert "The rollout is Monday." in result.answer
    assert "The owner is the platform team." in result.answer


def test_a_truncated_streams_function_calls_are_dropped_not_executed(client):
    """A cut-off stream can carry half-formed function calls; executing an
    argument list the provider never finished would act on garbage. They are
    dropped — the streamed text stands, and no tool call row exists."""
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        partial = _incomplete("Half an answer")
        partial.output.append(
            SimpleNamespace(
                type="function_call",
                call_id="call-cut",
                name="search_sources",
                arguments='{"query": "unfini',
            )
        )

        def model_step(input_items, tools, instructions):
            return [("delta", "Half an answer"), ("incomplete", partial)]

        result = run_agent_turn(
            db, run, evidence=[], settings=_settings(), model_step=model_step
        )
        calls = db.query(AgentToolCall).filter_by(run_id=run_id).count()
    finally:
        db.close()
    assert result is not None
    assert result.answer.startswith("Half an answer")
    assert calls == 0


def test_append_event_survives_losing_the_sequence_race(client):
    """Two writers per run is steering's designed common case (the worker's
    delta flush and the steer route). A lost race on UNIQUE(run_id, sequence)
    retries against the fresh maximum instead of raising — in the worker that
    raise failed the very run being steered."""
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        real = events_service._next_sequence
        stale_reads = {"count": 0}

        def contended(session, target_run_id):
            fresh = real(session, target_run_id)
            if stale_reads["count"] == 0:
                stale_reads["count"] += 1
                # Simulate the race: another appender claims `fresh` between
                # our read and our insert.
                session.add(
                    RunEvent(
                        workspace_id=run.workspace_id,
                        run_id=target_run_id,
                        sequence=fresh,
                        event_type="message.delta",
                        payload_json="{}",
                    )
                )
                session.flush()
                return fresh  # now stale: the insert above owns it
            return real(session, target_run_id)

        events_service._next_sequence = contended
        try:
            event = append_event(
                db,
                workspace_id=run.workspace_id,
                run_id=run_id,
                event_type="run.steer",
                payload={"content": "survive"},
            )
        finally:
            events_service._next_sequence = real
        db.commit()
        assert stale_reads["count"] == 1
        rows = (
            db.query(RunEvent)
            .filter_by(run_id=run_id, event_type="run.steer")
            .all()
        )
        assert [row.sequence for row in rows] == [event.sequence]
    finally:
        db.close()


def test_thinking_deltas_stream_as_their_own_event_lane(client):
    """("thinking", …) yields become `thinking.delta` run events — a separate
    lane from the answer, which carries none of the narration."""
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None

        def model_step(input_items, tools, instructions):
            return [
                ("thinking", "First, gather the passages."),
                ("delta", ANSWER),
                ("completed", _response()),
            ]

        result = run_agent_turn(
            db, run, evidence=[], settings=_settings(), model_step=model_step
        )
    finally:
        db.close()
    assert result is not None
    assert result.answer == ANSWER
    db = SessionLocal()
    try:
        events = (
            db.query(RunEvent)
            .filter_by(run_id=run_id, event_type="thinking.delta")
            .all()
        )
        assert len(events) == 1
        assert (
            json.loads(events[0].payload_json)["delta"]
            == "First, gather the passages."
        )
    finally:
        db.close()


def test_the_thinking_toggle_rides_the_send_and_lands_on_the_run(client):
    conversation = client.post(
        "/api/conversations", headers=_key(), json={"title": "Trail"}
    ).json()
    sent = client.post(
        f"/api/conversations/{conversation['id']}/messages",
        json={"content": "hello", "thinking": True},
        headers=_key(),
    )
    assert sent.status_code == 202, sent.text
    run_id = sent.json()["run"]["id"]
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        assert run.show_thinking is True
    finally:
        db.close()


def test_the_provider_incomplete_event_yields_the_partial_and_bills_it(
    client, monkeypatch
):
    """The model-layer half of the salvage, tested at its own seam: a
    `response.incomplete` terminal event yields ("incomplete", partial)
    instead of raising, and its usage is recorded exactly once —
    a turn that was cut off still spent the tokens it streamed."""
    from app.services import model as model_service
    from app.services import usage as usage_service

    partial = SimpleNamespace(
        usage={"input_tokens": 7, "output_tokens": 3},
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )
    events = [
        SimpleNamespace(type="response.output_text.delta", delta="Half an ans"),
        SimpleNamespace(type="response.incomplete", response=partial),
    ]
    fake_client = SimpleNamespace(
        responses=SimpleNamespace(create=lambda **kwargs: iter(events))
    )
    recorded: list[Any] = []
    monkeypatch.setattr(
        usage_service,
        "record_model_usage",
        lambda **kwargs: recorded.append(kwargs),
    )
    monkeypatch.setattr(
        model_service.usage,
        "record_model_usage",
        lambda **kwargs: recorded.append(kwargs),
    )

    yielded = list(
        model_service.stream_agent_response(
            fake_client,  # type: ignore[arg-type]
            _settings(),
            user_id="user-1",
            input_items=[],
            tools=[],
            instructions="",
        )
    )
    assert yielded == [("delta", "Half an ans"), ("incomplete", partial)]
    assert len(recorded) == 1
    assert recorded[0]["usage"] == {"input_tokens": 7, "output_tokens": 3}


def test_a_stream_that_produced_nothing_still_fails(client):
    run_id = _make_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None

        def model_step(input_items, tools, instructions):
            return [("incomplete", _incomplete(""))]

        with pytest.raises(RuntimeError, match="ended early"):
            run_agent_turn(
                db, run, evidence=[], settings=_settings(), model_step=model_step
            )
    finally:
        db.close()
