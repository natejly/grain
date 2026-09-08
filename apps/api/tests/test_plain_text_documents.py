"""Plain-text documents edit like a canvas.

A write that only touches a `kind == "text"` document applies live instead of
parking on the tool's default `ask` — the document's content is exactly what
the editor shows, every version is kept, and a wrong edit is one Restore away.

The tests that matter are the boundaries, not the happy path: the carve-out
may only ever soften the tool's OWN default prudence. Anything a person,
workspace, or conversation actually said — a standing `ask` or `deny` row,
`ask_all` (the injection escalation's mode) — still reaches a human, and a
markdown document still parks exactly as before.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any, Dict, List

from conftest import ask_before_writes

from app.database import SessionLocal
from app.models import AgentToolCall, Document, Run, ToolPolicy
from app.services.agent_loop import run_agent_turn


class _FakeResponse:
    def __init__(self, output=None, output_text=""):
        self.output = output or []
        self.output_text = output_text


def _completed(output=None, output_text=""):
    return [("completed", _FakeResponse(output=output, output_text=output_text))]


def _function_call(name: str, arguments: Dict[str, Any], call_id: str = "call-1"):
    return SimpleNamespace(
        type="function_call",
        name=name,
        call_id=call_id,
        arguments=json.dumps(arguments),
    )


def _write_step(calls: List[SimpleNamespace], answer: str = "Wrote it."):
    """First model turn proposes the calls; the next one closes the run."""

    def model_step(input_items, tools, instructions):
        if not any(
            isinstance(item, dict) and item.get("type") == "function_call_output"
            for item in input_items
        ):
            return _completed(output=calls)
        return _completed(output_text=answer)

    return model_step


def _make_run(client, prompt: str, *, mode: str = "ask_writes") -> str:
    bootstrap = client.get("/api/bootstrap").json()
    identity = bootstrap["identity"]
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "plaintext-conv-" + os.urandom(6).hex()},
        json={"title": "Plain text"},
    ).json()
    if mode == "ask_writes":
        ask_before_writes(client, conversation["id"])
    else:
        response = client.put(
            f"/api/conversations/{conversation['id']}/approval-mode",
            json={"mode": mode},
        )
        assert response.status_code == 200
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=identity["workspace_id"],
            conversation_id=conversation["id"],
            agent_id=bootstrap["default_agent_id"],
            created_by=identity["user_id"],
            status="running",
            prompt=prompt,
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _document(client, *, kind: str, content: str = "alpha\nbeta\n") -> dict:
    created = client.post(
        "/api/documents",
        json={
            "title": f"Canvas {kind} " + os.urandom(4).hex(),
            "content": content,
            "kind": kind,
        },
    )
    assert created.status_code == 201
    return created.json()


def _cleanup(db, run_id: str, *titles: str) -> None:
    db.query(ToolPolicy).delete()
    if titles:
        db.query(Document).filter(Document.title.in_(titles)).delete(
            synchronize_session=False
        )
    db.commit()


def _edit(document_id: str) -> SimpleNamespace:
    return _function_call(
        "edit_document",
        {
            "document_id": document_id,
            "find": "alpha",
            "replace": "ALPHA",
            "summary": "Shout the start",
        },
    )


def test_a_plain_text_edit_applies_without_parking(client):
    document = _document(client, kind="text")
    run_id = _make_run(client, "Shout the start.")
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        result = run_agent_turn(
            db, run, evidence=[], model_step=_write_step([_edit(document["id"])])
        )
        assert result is not None and result.answer == "Wrote it."
        record = db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).one()
        assert record.status == "succeeded"
        assert record.decided_by == "mode:plain_text_document"
        assert record.approved_by_mode == "plain_text_document"
        assert db.get(Document, document["id"]).content == "ALPHA\nbeta\n"
    finally:
        _cleanup(db, run_id)
        db.close()
        client.delete(f"/api/documents/{document['id']}")


def test_a_markdown_edit_still_parks(client):
    document = _document(client, kind="markdown")
    run_id = _make_run(client, "Shout the start.")
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert (
            run_agent_turn(
                db, run, evidence=[], model_step=_write_step([_edit(document["id"])])
            )
            is None
        )
        record = db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).one()
        assert record.status == "proposed"
        assert db.get(Run, run_id).status == "waiting_for_approval"
        assert db.get(Document, document["id"]).content == "alpha\nbeta\n"
    finally:
        _cleanup(db, run_id)
        db.close()
        client.delete(f"/api/documents/{document['id']}")


def test_a_standing_ask_row_still_parks_a_plain_text_edit(client):
    """A row is the user's or the workspace's explicit "ask me" — the carve-out
    softens only the tool's own default, never a stated instruction."""
    document = _document(client, kind="text")
    identity = client.get("/api/bootstrap").json()["identity"]
    run_id = _make_run(client, "Shout the start.")
    db = SessionLocal()
    try:
        db.add(
            ToolPolicy(
                workspace_id=identity["workspace_id"],
                tool_name="edit_document",
                policy="ask",
            )
        )
        db.commit()
        run = db.get(Run, run_id)
        assert (
            run_agent_turn(
                db, run, evidence=[], model_step=_write_step([_edit(document["id"])])
            )
            is None
        )
        record = db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).one()
        assert record.status == "proposed"
        assert db.get(Document, document["id"]).content == "alpha\nbeta\n"
    finally:
        _cleanup(db, run_id)
        db.close()
        client.delete(f"/api/documents/{document['id']}")


def test_a_deny_row_still_denies_a_plain_text_edit(client):
    """A prohibition is not a grant, and nothing about text documents loosens one."""
    document = _document(client, kind="text")
    identity = client.get("/api/bootstrap").json()["identity"]
    run_id = _make_run(client, "Shout the start.")
    db = SessionLocal()
    try:
        db.add(
            ToolPolicy(
                workspace_id=identity["workspace_id"],
                tool_name="edit_document",
                policy="deny",
            )
        )
        db.commit()
        run = db.get(Run, run_id)
        result = run_agent_turn(
            db, run, evidence=[], model_step=_write_step([_edit(document["id"])])
        )
        assert result is not None
        record = db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).one()
        assert record.status == "denied"
        assert db.get(Document, document["id"]).content == "alpha\nbeta\n"
    finally:
        _cleanup(db, run_id)
        db.close()
        client.delete(f"/api/documents/{document['id']}")


def test_ask_all_still_parks_a_plain_text_edit(client):
    """`ask_all` is a person (or the injection screen) saying "everything", and
    everything includes the canvas."""
    document = _document(client, kind="text")
    run_id = _make_run(client, "Shout the start.", mode="ask_all")
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert (
            run_agent_turn(
                db, run, evidence=[], model_step=_write_step([_edit(document["id"])])
            )
            is None
        )
        record = db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).one()
        assert record.status == "proposed"
        assert db.get(Document, document["id"]).content == "alpha\nbeta\n"
    finally:
        _cleanup(db, run_id)
        db.close()
        client.delete(f"/api/documents/{document['id']}")


def test_a_plain_text_create_applies_and_a_markdown_create_parks(client):
    text_title = "Canvas create text " + os.urandom(4).hex()
    md_title = "Canvas create md " + os.urandom(4).hex()

    run_id = _make_run(client, "Create the text file.")
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        result = run_agent_turn(
            db,
            run,
            evidence=[],
            model_step=_write_step(
                [
                    _function_call(
                        "create_document",
                        {"title": text_title, "content": "notes", "kind": "text"},
                    )
                ]
            ),
        )
        assert result is not None and result.answer == "Wrote it."
        record = db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).one()
        assert record.status == "succeeded"
        assert record.approved_by_mode == "plain_text_document"
        assert (
            db.query(Document).filter(Document.title == text_title).count() == 1
        )
    finally:
        _cleanup(db, run_id, text_title)
        db.close()

    # The tool's default kind is markdown, so a create that does not say
    # `kind: "text"` is not plain text and parks like any other write.
    run_id = _make_run(client, "Create the markdown file.")
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert (
            run_agent_turn(
                db,
                run,
                evidence=[],
                model_step=_write_step(
                    [
                        _function_call(
                            "create_document",
                            {"title": md_title, "content": "notes"},
                        )
                    ]
                ),
            )
            is None
        )
        record = db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).one()
        assert record.status == "proposed"
        assert db.query(Document).filter(Document.title == md_title).count() == 0
    finally:
        _cleanup(db, run_id, md_title)
        db.close()


def test_an_unresolvable_target_parks(client):
    """Fail closed: an edit whose target cannot be resolved to a text document
    here and now is not one the carve-out may wave through."""
    run_id = _make_run(client, "Shout the start.")
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert (
            run_agent_turn(
                db, run, evidence=[], model_step=_write_step([_edit("no-such-doc")])
            )
            is None
        )
        record = db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).one()
        assert record.status == "proposed"
    finally:
        _cleanup(db, run_id)
        db.close()
