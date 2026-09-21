"""Who writes the council's run event — the parallel batch's stated invariant.

`agent_loop._delegate_parallel_batch` says it outright: "`run_events` is unique
on (run_id, sequence), so worker threads write **no events** — every row and
event is written serially on the parent session." `delegation`'s own module
docstring says the same thing from the other side, and `_screen_excerpt` pays a
real cost to honour it (a screen hit rides home inside the ToolResult rather
than writing a row).

`_record_council` broke it. It called `append_event` + `db.commit()` from inside
`_delegate`, which in a parallel batch runs on a ThreadPoolExecutor worker
holding its own Session — so two councils in one model round raced
`_next_sequence`. `append_event` retries, so the usual outcome was contention
rather than a crash; the defect was that a documented concurrency invariant had
become false, and the next person adding an event to the child path would have
had no retry contract to hide behind.

The test asserts both halves, because they are two different facts: the event
is written on the coordinator thread, AND `delegation` no longer imports
`append_event` at all — there is no name in there to write one with.

THIS TEST IS THE INVARIANT. The write is now marshalled back to the
coordinator: `_council_events` returns the record as data, `_delegate` puts it
on `ToolResult.deferred_events`, and `agent_loop._append_deferred` appends it on
the parent session beside `tool.completed` — in queue order, so the event
stream of a parallel batch is byte-identical to a serial one. It was an xfail
while that was still to do; it is a live test now, and it fails again the
moment anything writes a run event from a worker.
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from typing import Any, Dict, List

from app.database import SessionLocal
from app.models import Agent, Chunk, Run, Source, new_id
from app.services import agent_loop, delegation
from app.services.agent_loop import run_agent_turn


class FakeResponse:
    def __init__(self, output=None, output_text=""):
        self.output = output or []
        self.output_text = output_text


def _completed(output=None, output_text=""):
    return [("completed", FakeResponse(output=output, output_text=output_text))]


def _function_call(name: str, arguments: Dict[str, Any], call_id: str):
    return SimpleNamespace(
        type="function_call",
        name=name,
        call_id=call_id,
        arguments=json.dumps(arguments),
    )


def _make_run(client, prompt: str) -> str:
    identity = client.get("/api/bootstrap").json()["identity"]
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "council-conv-" + prompt[:12]},
        json={"title": "Council"},
    ).json()
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=identity["workspace_id"],
            conversation_id=conversation["id"],
            agent_id=client.get("/api/bootstrap").json()["default_agent_id"],
            created_by=identity["user_id"],
            status="running",
            prompt=prompt,
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _corpus(db, workspace_id: str, *, text: str) -> None:
    """A passage the council's one retrieval can freeze.

    Without frozen evidence `_delegate` degrades to independent attempts and
    never scores a council at all, so the corpus is what makes this test about
    the thing it claims to be about.
    """
    source = Source(
        workspace_id=workspace_id,
        created_by="",
        filename="council-" + new_id() + ".md",
        media_type="text/markdown",
        object_key="/x/council.md",
        byte_size=len(text),
        status="ready",
    )
    db.add(source)
    db.flush()
    db.add(
        Chunk(
            workspace_id=workspace_id,
            source_id=source.id,
            ordinal=0,
            content=text,
            char_start=0,
            char_end=len(text),
            token_count=len(text.split()),
        )
    )
    db.commit()


def _agent_name(db, workspace_id: str) -> str:
    agent = (
        db.query(Agent)
        .filter(Agent.workspace_id == workspace_id, Agent.enabled.is_(True))
        .order_by(Agent.created_at)
        .first()
    )
    assert agent is not None
    return agent.name


def test_every_council_event_is_written_on_the_coordinator_thread(
    client, monkeypatch
) -> None:
    # The delegate module does not import `append_event` at all any more, which
    # is the structural half of the invariant: there is no name in there to
    # write an event WITH, from a worker thread or anywhere else.
    assert not hasattr(delegation, "append_event")

    threads: List[str] = []
    real_append = agent_loop.append_event

    def recording_append(db, **kwargs: Any):
        if kwargs.get("event_type") == "council.scored":
            threads.append(threading.current_thread().name)
        return real_append(db, **kwargs)

    monkeypatch.setattr(agent_loop, "append_event", recording_append)

    def factory(settings, *, prompt, user_id, model, effort, workspace_id="", run_id=""):
        def step(input_items, tools, instructions):
            return _completed(output_text=f"answer for: {prompt[:30]}")

        return step

    monkeypatch.setattr(delegation, "_child_step", factory)
    run_id = _make_run(client, prompt="Two councils in one round.")
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        name = _agent_name(db, run.workspace_id)
        _corpus(
            db,
            run.workspace_id,
            text=(
                "The first task is the retention window, and the second task "
                "is the billing exception."
            ),
        )

        def model_step(input_items, tools, instructions):
            outputs = [
                item
                for item in input_items
                if isinstance(item, dict) and item.get("type") == "function_call_output"
            ]
            if not outputs:
                return _completed(
                    output=[
                        _function_call(
                            "delegate",
                            {"agent": name, "prompt": "first task", "attempts": 2},
                            "call-a",
                        ),
                        _function_call(
                            "delegate",
                            {"agent": name, "prompt": "second task", "attempts": 2},
                            "call-b",
                        ),
                    ]
                )
            return _completed(output_text="Done.")

        assert run_agent_turn(db, run, evidence=[], model_step=model_step) is not None
        assert threads, "no council was scored, so this proves nothing"
        assert set(threads) == {threading.main_thread().name}, threads
        # Both councils' events, and both written by the coordinator rather
        # than by the module that computed them.
        assert len(threads) == 2, threads
    finally:
        db.close()
