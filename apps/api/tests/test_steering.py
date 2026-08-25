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
from app.models import Message, Run, RunEvent
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
