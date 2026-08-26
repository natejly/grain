"""Run undo: the checkpoint trail a run's writes leave, and the endpoint that
walks it backwards.

The end-to-end test drives a real agent turn through `run_agent_turn` with an
injected model step (the `_park_run` pattern from test_agent_approvals.py),
under `auto_writes` so the writes execute instead of parking — which is also
the mode whose unreviewed writes an undo button exists for.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, inspect, select

from app.database import SessionLocal, engine
from app.models import BoardCard, Document, Run, RunCheckpoint, RunEvent
from app.services import checkpoints
from app.services.agent_loop import run_agent_turn
from app.services.artifacts import boards, documents
from app.services.llm_tools import ToolContext

API_ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, output=None, output_text=""):
        self.output = output or []
        self.output_text = output_text


def _call(name: str, arguments: dict, call_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="function_call",
        name=name,
        call_id=call_id,
        arguments=json.dumps(arguments),
    )


def _steps(calls):
    """A model that issues `calls` on its first round and then wraps up."""
    rounds = iter(
        [
            [("completed", FakeResponse(output=calls))],
            [("completed", FakeResponse(output_text="All done."))],
        ]
    )

    def model_step(input_items, tools, instructions):
        return next(rounds)

    return model_step


def _run_writes(client, calls) -> str:
    """Drive one auto-approved turn that executes `calls`; return the run id.

    The run is left in a terminal state, which is what the undo endpoint
    requires — the worker normally stamps that in `_finish_run`.
    """
    boot = client.get("/api/bootstrap").json()
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "undo-conv-" + os.urandom(6).hex()},
        json={"title": "Undo"},
    ).json()
    client.put(
        f"/api/conversations/{conversation['id']}/approval-mode",
        json={"mode": "auto_writes"},
    )
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=boot["identity"]["workspace_id"],
            conversation_id=conversation["id"],
            agent_id=boot["default_agent_id"],
            created_by=boot["identity"]["user_id"],
            status="running",
            prompt="Make the changes",
        )
        db.add(run)
        db.commit()
        run_id = run.id
        result = run_agent_turn(db, run, evidence=[], model_step=_steps(calls))
        assert result is not None, "the turn should complete, not park"
        run = db.get(Run, run_id)
        run.status = "completed"
        db.commit()
        return run_id
    finally:
        db.close()


def test_every_run_checkpoint_row_is_workspace_scoped():
    columns = RunCheckpoint.__table__.columns
    assert "workspace_id" in columns
    assert not columns["workspace_id"].nullable


def test_the_migration_chain_builds_the_run_checkpoints_table():
    """`alembic upgrade head` from empty must match `create_all` — the parity
    test every new table gets, per test_workflow_schema.py."""
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'chain.db'}"
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=API_ROOT,
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "DATABASE_URL": url,
                "APP_ENV": "test",
                "MODEL_PROVIDER": "scripted",
                "SCRIPTED_MODEL_SCRIPT": "tests/scripts/agent.json",
                "PYTHONPATH": str(API_ROOT),
            },
        )
        assert result.returncode == 0, result.stderr
        migrated = inspect(create_engine(url))
        assert "run_checkpoints" in migrated.get_table_names()
        declared = inspect(engine)
        assert {c["name"] for c in migrated.get_columns("run_checkpoints")} == {
            c["name"] for c in declared.get_columns("run_checkpoints")
        }
        assert {i["name"] for i in migrated.get_indexes("run_checkpoints")} >= {
            i["name"] for i in declared.get_indexes("run_checkpoints")
        }


def test_undo_restores_the_document_and_removes_the_card(client):
    """The headline path: a turn edits a document and adds a board card; undo
    puts the document's text back — as a NEW version, never rewritten history —
    and takes the card off the board."""
    boot = client.get("/api/bootstrap").json()
    workspace_id = boot["identity"]["workspace_id"]
    suffix = os.urandom(4).hex()
    db = SessionLocal()
    try:
        document = documents.create_document(
            db,
            workspace_id=workspace_id,
            title=f"Undo brief {suffix}",
            content="The original figure is 40.",
        )
        board = boards.create_board(
            db, workspace_id=workspace_id, name=f"Undo board {suffix}"
        )
        document_id, board_id = document.id, board.id
        versions_before = len(
            documents.list_versions(
                db, workspace_id=workspace_id, document_id=document_id
            )
        )
    finally:
        db.close()

    run_id = _run_writes(
        client,
        [
            _call(
                "edit_document",
                {
                    "document_id": document_id,
                    "find": "40",
                    "replace": "45",
                },
                "undo-1",
            ),
            _call(
                "board_add_card",
                {
                    "board_id": board_id,
                    "column": "Todo",
                    "title": f"Chase the figure {suffix}",
                },
                "undo-2",
            ),
        ],
    )

    db = SessionLocal()
    try:
        assert db.get(Document, document_id).content == "The original figure is 45."
        rows = list(
            db.scalars(
                select(RunCheckpoint).where(RunCheckpoint.run_id == run_id)
            )
        )
        assert {row.kind for row in rows} == {"document", "board"}
        assert all(row.reversible for row in rows)
    finally:
        db.close()

    response = client.post(f"/api/runs/{run_id}/undo")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["skipped"] == []
    assert {item["tool_name"] for item in body["reverted"]} == {
        "edit_document",
        "board_add_card",
    }

    db = SessionLocal()
    try:
        assert db.get(Document, document_id).content == "The original figure is 40."
        versions_after = len(
            documents.list_versions(
                db, workspace_id=workspace_id, document_id=document_id
            )
        )
        # The edit snapshotted once, and the undo snapshotted the edited text
        # once more on its way out: history grew, nothing was rewritten.
        assert versions_after == versions_before + 2
        cards = list(
            db.scalars(select(BoardCard).where(BoardCard.board_id == board_id))
        )
        assert cards == []
        event = db.scalar(
            select(RunEvent).where(
                RunEvent.run_id == run_id, RunEvent.event_type == "run.reverted"
            )
        )
        assert event is not None
    finally:
        db.close()


def test_undoing_the_same_run_twice_refuses(client):
    suffix = os.urandom(4).hex()
    db = SessionLocal()
    try:
        board = boards.create_board(
            db,
            workspace_id=client.get("/api/bootstrap").json()["identity"][
                "workspace_id"
            ],
            name=f"Twice board {suffix}",
        )
        board_id = board.id
    finally:
        db.close()
    run_id = _run_writes(
        client,
        [
            _call(
                "board_add_card",
                {"board_id": board_id, "column": "Todo", "title": "Only once"},
                "twice-1",
            )
        ],
    )
    assert client.post(f"/api/runs/{run_id}/undo").status_code == 200
    second = client.post(f"/api/runs/{run_id}/undo")
    assert second.status_code == 409
    assert "already" in second.json()["detail"]


def test_an_active_run_cannot_be_undone(client):
    boot = client.get("/api/bootstrap").json()
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "undo-live-" + os.urandom(6).hex()},
        json={"title": "Live"},
    ).json()
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=boot["identity"]["workspace_id"],
            conversation_id=conversation["id"],
            agent_id=boot["default_agent_id"],
            created_by=boot["identity"]["user_id"],
            status="running",
            prompt="still going",
        )
        db.add(run)
        db.commit()
        run_id = run.id
    finally:
        db.close()
    response = client.post(f"/api/runs/{run_id}/undo")
    assert response.status_code == 409


def test_a_foreign_run_is_not_found(client, identity_client):
    """Another tenant naming my run id gets the same 404 a made-up id gets."""
    boot = client.get("/api/bootstrap").json()
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "undo-foreign-" + os.urandom(6).hex()},
        json={"title": "Mine"},
    ).json()
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=boot["identity"]["workspace_id"],
            conversation_id=conversation["id"],
            agent_id=boot["default_agent_id"],
            created_by=boot["identity"]["user_id"],
            status="completed",
            prompt="finished",
        )
        db.add(run)
        db.commit()
        run_id = run.id
    finally:
        db.close()
    other = identity_client()
    assert other.post(f"/api/runs/{run_id}/undo").status_code == 404


def test_external_writes_are_reported_skipped_not_reverted(client):
    """A checkpoint whose effects left the workspace is consumed but honest."""
    suffix = os.urandom(4).hex()
    db = SessionLocal()
    try:
        board = boards.create_board(
            db,
            workspace_id=client.get("/api/bootstrap").json()["identity"][
                "workspace_id"
            ],
            name=f"External board {suffix}",
        )
        board_id = board.id
    finally:
        db.close()
    run_id = _run_writes(
        client,
        [
            _call(
                "board_add_card",
                {"board_id": board_id, "column": "Todo", "title": "Mixed run"},
                "ext-1",
            )
        ],
    )
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        db.add(
            RunCheckpoint(
                workspace_id=run.workspace_id,
                run_id=run_id,
                tool_call_id="",
                tool_name="run_python",
                kind="external",
                reversible=False,
                before_json="",
            )
        )
        db.commit()
    finally:
        db.close()
    response = client.post(f"/api/runs/{run_id}/undo")
    assert response.status_code == 200
    body = response.json()
    assert [item["tool_name"] for item in body["skipped"]] == ["run_python"]
    assert "cannot be undone" in body["skipped"][0]["reason"]
    assert [item["tool_name"] for item in body["reverted"]] == ["board_add_card"]


def test_an_unknown_write_tool_captures_as_external():
    """The fallback: any write tool without a family capture is recorded as an
    irreversible external effect, never silently uncovered."""
    db = SessionLocal()
    try:
        context = ToolContext(workspace_id="w", user_id="u", conversation_id="c")
        pending = checkpoints.capture_before(db, context, "run_python", {"code": "1"})
        assert pending is not None
        assert (pending.kind, pending.reversible) == ("external", False)
    finally:
        db.close()


def test_a_capture_failure_never_fails_the_tool_call(client, monkeypatch):
    """The swallow-and-log guard in execute_agent_tool_call, proven."""

    def explode(*args, **kwargs):
        raise RuntimeError("capture bug")

    monkeypatch.setattr("app.services.checkpoints.capture_before", explode)
    suffix = os.urandom(4).hex()
    db = SessionLocal()
    try:
        board = boards.create_board(
            db,
            workspace_id=client.get("/api/bootstrap").json()["identity"][
                "workspace_id"
            ],
            name=f"Crash board {suffix}",
        )
        board_id = board.id
    finally:
        db.close()
    run_id = _run_writes(
        client,
        [
            _call(
                "board_add_card",
                {"board_id": board_id, "column": "Todo", "title": "Still lands"},
                "crash-1",
            )
        ],
    )
    db = SessionLocal()
    try:
        cards = list(
            db.scalars(select(BoardCard).where(BoardCard.board_id == board_id))
        )
        assert [card.title for card in cards] == ["Still lands"]
        rows = list(
            db.scalars(select(RunCheckpoint).where(RunCheckpoint.run_id == run_id))
        )
        assert rows == [], "a failed capture records nothing"
    finally:
        db.close()
