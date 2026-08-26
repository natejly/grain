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

import pytest
from sqlalchemy import create_engine, inspect, select
from test_dashboards import make_dataset, unique

from app.auth import DEV_SEED_USER_ID, DEV_SEED_WORKSPACE_ID
from app.database import SessionLocal, engine
from app.models import (
    Board,
    BoardCard,
    Dashboard,
    DashboardTemplate,
    Document,
    Project,
    ProjectFile,
    Run,
    RunCheckpoint,
    RunEvent,
)
from app.services import checkpoints
from app.services.agent_loop import run_agent_turn
from app.services.artifacts import boards, documents
from app.services.llm_tools import ToolContext, ToolResult, build_registry
from app.services.projects import store as project_store

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


def test_undo_deletes_what_the_run_created(client):
    """End to end through the executor's `created_ids`: a document the run
    created is attributed and deleted by the undo."""
    suffix = os.urandom(4).hex()
    run_id = _run_writes(
        client,
        [
            _call(
                "create_document",
                {"title": f"Created by run {suffix}", "content": "fresh"},
                "created-1",
            )
        ],
    )
    response = client.post(f"/api/runs/{run_id}/undo")
    assert response.status_code == 200, response.text
    assert [item["tool_name"] for item in response.json()["reverted"]] == [
        "create_document"
    ]
    db = SessionLocal()
    try:
        assert (
            db.scalar(
                select(Document).where(Document.title == f"Created by run {suffix}")
            )
            is None
        )
    finally:
        db.close()


def test_creation_attribution_comes_from_the_executor_not_a_set_diff(client):
    """A row created concurrently — even one whose id sorts first — is never
    recorded as this run's creation, so its undo cannot delete it."""
    boot = client.get("/api/bootstrap").json()
    workspace_id = boot["identity"]["workspace_id"]
    user_id = boot["identity"]["user_id"]
    suffix = os.urandom(4).hex()
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=workspace_id,
            conversation_id="",
            agent_id=boot["default_agent_id"],
            created_by=user_id,
            status="completed",
            prompt="attribution",
        )
        db.add(run)
        db.commit()
        run_id = run.id
        context = ToolContext(
            workspace_id=workspace_id, user_id=user_id, conversation_id=""
        )
        pending = checkpoints.capture_before(
            db, context, "create_document", {"title": f"Mine {suffix}"}
        )
        # Someone else's creation lands between capture and record, with an id
        # that sorts before any uuid4 — exactly what the old set-diff picked.
        decoy = Document(
            id="00000000-0000-0000-0000-000000000000",
            workspace_id=workspace_id,
            title=f"Decoy {suffix}",
            kind="markdown",
            content="not this run's work",
            created_by=user_id,
        )
        db.add(decoy)
        db.flush()
        mine = documents.create_document(
            db, workspace_id=workspace_id, title=f"Mine {suffix}", content="mine"
        )
        checkpoints.record_checkpoint(
            db,
            run=run,
            tool_call_id="attr-1",
            name="create_document",
            pending=pending,
            result=ToolResult(content="created", created_ids=[mine.id]),
        )
        db.commit()
        mine_id = mine.id
        row = db.scalar(select(RunCheckpoint).where(RunCheckpoint.run_id == run_id))
        assert json.loads(row.before_json)["document_id"] == mine_id
    finally:
        db.close()
    assert client.post(f"/api/runs/{run_id}/undo").status_code == 200
    db = SessionLocal()
    try:
        assert db.get(Document, mine_id) is None, "the run's own creation is undone"
        decoy_row = db.get(Document, "00000000-0000-0000-0000-000000000000")
        assert decoy_row is not None, "the concurrent creation is untouched"
        db.delete(decoy_row)
        db.commit()
    finally:
        db.close()


def test_a_board_changed_after_the_run_is_skipped_not_clobbered(client):
    """The clobber guard: the board restore is delete-and-recreate, so a card
    someone added after the run refuses the restore instead of vanishing."""
    suffix = os.urandom(4).hex()
    boot = client.get("/api/bootstrap").json()
    workspace_id = boot["identity"]["workspace_id"]
    db = SessionLocal()
    try:
        board = boards.create_board(
            db, workspace_id=workspace_id, name=f"Guarded board {suffix}"
        )
        board_id = board.id
    finally:
        db.close()
    run_id = _run_writes(
        client,
        [
            _call(
                "board_add_card",
                {"board_id": board_id, "column": "Todo", "title": "From the run"},
                "guard-1",
            )
        ],
    )
    db = SessionLocal()
    try:
        board = boards.get_board(db, workspace_id=workspace_id, board_id=board_id)
        boards.add_card(
            db,
            workspace_id=workspace_id,
            board=board,
            column="Todo",
            title="Added by a human afterwards",
        )
    finally:
        db.close()
    response = client.post(f"/api/runs/{run_id}/undo")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reverted"] == []
    assert [item["tool_name"] for item in body["skipped"]] == ["board_add_card"]
    assert "changed after this run" in body["skipped"][0]["reason"]
    db = SessionLocal()
    try:
        titles = {
            card.title
            for card in db.scalars(
                select(BoardCard).where(BoardCard.board_id == board_id)
            )
        }
        assert titles == {"From the run", "Added by a human afterwards"}
    finally:
        db.close()


def test_undoing_create_project_refuses_once_it_gained_a_file(client):
    """Undo of `create_project` deletes the whole project — so a file added
    after the run means the delete is refused, not applied."""
    suffix = os.urandom(4).hex()
    boot = client.get("/api/bootstrap").json()
    workspace_id = boot["identity"]["workspace_id"]
    run_id = _run_writes(
        client,
        [
            _call(
                "create_project",
                {"name": f"Guarded project {suffix}", "kind": "web"},
                "proj-1",
            )
        ],
    )
    db = SessionLocal()
    try:
        project = db.scalar(
            select(Project).where(
                Project.workspace_id == workspace_id,
                Project.name == f"Guarded project {suffix}",
            )
        )
        assert project is not None
        project_id = project.id
        db.add(
            ProjectFile(
                workspace_id=workspace_id,
                project_id=project_id,
                path="added-later.txt",
                content="work the undo must not destroy",
            )
        )
        db.commit()
    finally:
        db.close()
    response = client.post(f"/api/runs/{run_id}/undo")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["tool_name"] for item in body["skipped"]] == ["create_project"]
    assert "changed after this run" in body["skipped"][0]["reason"]
    db = SessionLocal()
    try:
        assert db.get(Project, project_id) is not None
    finally:
        db.close()


def test_an_interrupted_undo_leaves_no_stranded_rows(client):
    """Consumption is per-row: rows an earlier (crashed) undo never stamped
    are picked up by a retry instead of 409ing forever, and only once all rows
    are consumed does the endpoint refuse.

    The crash is simulated faithfully — the newest row is stamped *and* its
    restore applied — because a retry that only reports a number proves
    nothing. What must hold is that the remaining row is really restored:
    the board comes back to the state before the run touched it.
    """
    suffix = os.urandom(4).hex()
    boot = client.get("/api/bootstrap").json()
    workspace_id = boot["identity"]["workspace_id"]
    db = SessionLocal()
    try:
        board = boards.create_board(
            db, workspace_id=workspace_id, name=f"Retry board {suffix}"
        )
        board_id = board.id
    finally:
        db.close()
    run_id = _run_writes(
        client,
        [
            _call(
                "board_add_card",
                {"board_id": board_id, "column": "Todo", "title": "First write"},
                "retry-1",
            ),
            _call(
                "board_add_card",
                {"board_id": board_id, "column": "Todo", "title": "Second write"},
                "retry-2",
            ),
        ],
    )
    db = SessionLocal()
    try:
        rows = list(
            db.scalars(
                select(RunCheckpoint)
                .where(RunCheckpoint.run_id == run_id)
                .order_by(RunCheckpoint.created_at.desc(), RunCheckpoint.id.desc())
            )
        )
        assert len(rows) == 2
        # Simulate a crash that consumed the newest row *and* applied it: its
        # restore removed the second card, which is the state the older row's
        # own guard snapshot expects to find.
        newest = rows[0]
        newest.reverted_at = newest.created_at
        db.commit()
        stamped_id = newest.id
        board = boards.get_board(db, workspace_id=workspace_id, board_id=board_id)
        boards.delete_card(db, board=board, card="Second write")
    finally:
        db.close()
    retry = client.post(f"/api/runs/{run_id}/undo")
    assert retry.status_code == 200, retry.text
    body = retry.json()
    assert body["skipped"] == []
    assert [item["tool_name"] for item in body["reverted"]] == ["board_add_card"], (
        "only the unconsumed row is processed — and it is really restored"
    )
    db = SessionLocal()
    try:
        cards = list(
            db.scalars(select(BoardCard).where(BoardCard.board_id == board_id))
        )
        assert cards == [], "the retry put the board back, not just a number"
        rows = list(
            db.scalars(select(RunCheckpoint).where(RunCheckpoint.run_id == run_id))
        )
        assert all(row.reverted_at is not None for row in rows)
        assert stamped_id in {row.id for row in rows}
    finally:
        db.close()
    assert client.post(f"/api/runs/{run_id}/undo").status_code == 409


def test_a_protective_skip_is_retryable_once_the_later_edits_are_settled(client):
    """A skip the clobber guard made is not a verdict on the checkpoint, only
    on the moment — so it releases the row instead of consuming it. Undo the
    same run again after clearing the later edit and it finishes the job."""
    suffix = os.urandom(4).hex()
    boot = client.get("/api/bootstrap").json()
    workspace_id = boot["identity"]["workspace_id"]
    db = SessionLocal()
    try:
        board = boards.create_board(
            db, workspace_id=workspace_id, name=f"Retryable board {suffix}"
        )
        board_id = board.id
    finally:
        db.close()
    run_id = _run_writes(
        client,
        [
            _call(
                "board_add_card",
                {"board_id": board_id, "column": "Todo", "title": "From the run"},
                "protect-1",
            )
        ],
    )
    db = SessionLocal()
    try:
        board = boards.get_board(db, workspace_id=workspace_id, board_id=board_id)
        boards.add_card(
            db,
            workspace_id=workspace_id,
            board=board,
            column="Todo",
            title="A human's later card",
        )
    finally:
        db.close()

    first = client.post(f"/api/runs/{run_id}/undo")
    assert first.status_code == 200, first.text
    skipped = first.json()["skipped"]
    assert [item["outcome"] for item in skipped] == ["protected"]
    assert "changed after this run" in skipped[0]["reason"]
    db = SessionLocal()
    try:
        rows = list(
            db.scalars(select(RunCheckpoint).where(RunCheckpoint.run_id == run_id))
        )
        assert [row.reverted_at for row in rows] == [None], (
            "a protected row is released, not consumed"
        )
        # The user settles the later edit the guard was protecting.
        board = boards.get_board(db, workspace_id=workspace_id, board_id=board_id)
        boards.delete_card(db, board=board, card="A human's later card")
    finally:
        db.close()

    second = client.post(f"/api/runs/{run_id}/undo")
    assert second.status_code == 200, second.text
    assert [item["tool_name"] for item in second.json()["reverted"]] == [
        "board_add_card"
    ]
    db = SessionLocal()
    try:
        cards = list(
            db.scalars(select(BoardCard).where(BoardCard.board_id == board_id))
        )
        assert cards == []
    finally:
        db.close()
    # Consumed this time: the retry that worked is still a one-shot undo.
    assert client.post(f"/api/runs/{run_id}/undo").status_code == 409


def test_two_concurrent_undos_cannot_both_apply_one_checkpoint(client):
    """The conditional UPDATE on `reverted_at IS NULL`, exercised directly.

    Two workers holding the same rows is what the endpoint's 409 cannot
    prevent — it reads before it writes. The loser must see rowcount 0, report
    the row as already consumed, and restore *nothing*, or an undo that raced
    would overwrite whatever was typed between the two attempts.
    """
    boot = client.get("/api/bootstrap").json()
    workspace_id = boot["identity"]["workspace_id"]
    user_id = boot["identity"]["user_id"]
    suffix = os.urandom(4).hex()
    db = SessionLocal()
    try:
        document = documents.create_document(
            db,
            workspace_id=workspace_id,
            title=f"Raced brief {suffix}",
            content="The original sentence.",
        )
        document_id = document.id
    finally:
        db.close()
    run_id = _run_writes(
        client,
        [
            _call(
                "edit_document",
                {"document_id": document_id, "find": "original", "replace": "edited"},
                "race-1",
            )
        ],
    )
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        rows = list(
            db.scalars(
                select(RunCheckpoint).where(RunCheckpoint.run_id == run_id)
            )
        )
        assert len(rows) == 1
        reverted, skipped = checkpoints.revert_run(
            db, run=run, actor_id=user_id, rows=rows
        )
        db.commit()
        assert [item["tool_name"] for item in reverted] == ["edit_document"]
        assert skipped == []
        # Between the two undos, someone writes. The second undo must not
        # reach past the consumed marker and clobber it.
        documents.replace_content(
            db,
            workspace_id=workspace_id,
            document_id=document_id,
            content="Words typed after the undo.",
            summary="Later work",
            created_by=user_id,
        )
        db.commit()
        loser_reverted, loser_skipped = checkpoints.revert_run(
            db, run=run, actor_id=user_id, rows=rows
        )
        db.commit()
        assert loser_reverted == []
        assert [item["outcome"] for item in loser_skipped] == ["concurrent"]
        assert db.get(Document, document_id).content == "Words typed after the undo."
    finally:
        db.close()


def test_the_guard_snapshot_does_not_halve_the_reversible_ceiling(client):
    """`MAX_BEFORE_CHARS` is the before-state's budget, not a budget the guard
    shares with it: a file that was reversible before the after-state existed
    is still reversible, because the guard is stored as a fingerprint."""
    boot = client.get("/api/bootstrap").json()
    workspace_id = boot["identity"]["workspace_id"]
    user_id = boot["identity"]["user_id"]
    suffix = os.urandom(4).hex()
    # Comfortably over half the ceiling, so before + a second full copy would
    # not fit — which is exactly what a shared budget would have measured.
    body = "x" * (checkpoints.MAX_BEFORE_CHARS - 2_000)
    db = SessionLocal()
    try:
        project = project_store.create_project(
            db,
            workspace_id=workspace_id,
            name=f"Ceiling project {suffix}",
            created_by=user_id,
            entry_path="big.txt",
            files={"big.txt": body},
        )
        run = Run(
            workspace_id=workspace_id,
            conversation_id="",
            agent_id=boot["default_agent_id"],
            created_by=user_id,
            status="completed",
            prompt="rewrite the big file",
        )
        db.add(run)
        db.commit()
        context = ToolContext(
            workspace_id=workspace_id, user_id=user_id, conversation_id=""
        )
        args = {"project_id": project.id, "path": "big.txt", "content": "small"}
        pending = checkpoints.capture_before(db, context, "fs_write", args)
        assert pending is not None
        checkpoints.record_checkpoint(
            db,
            run=run,
            tool_call_id="ceiling-1",
            name="fs_write",
            pending=pending,
            result=ToolResult(content="written"),
        )
        db.commit()
        row = db.scalar(select(RunCheckpoint).where(RunCheckpoint.run_id == run.id))
        assert row.reversible is True, "a file under the ceiling stays reversible"
        recorded = json.loads(row.before_json)
        assert recorded["files"]["big.txt"] == body
        # And the guard is still there, at constant cost.
        assert recorded["after"]["files"]["big.txt"]
        assert len(row.before_json) < checkpoints.MAX_BEFORE_CHARS + 1_000
    finally:
        db.close()


def test_a_reserialized_dashboard_spec_does_not_trip_the_guard(client):
    """The guard asks whether the dashboard still holds what the run left, not
    whether its JSON was re-typed the same way. A spec re-serialized with its
    keys in another order is the same dashboard, and the undo must apply."""
    dataset = make_dataset(client)
    boot = client.get("/api/bootstrap").json()
    workspace_id = boot["identity"]["workspace_id"]
    user_id = boot["identity"]["user_id"]
    name = unique("Reserialized")
    db = SessionLocal()
    try:
        context = ToolContext(
            workspace_id=workspace_id, user_id=user_id, conversation_id=""
        )
        registry = build_registry(db, context)
        created = registry["create_dashboard"].executor(
            db,
            context,
            {
                "name": name,
                "dataset_id": dataset["id"],
                "visualization": "bar",
                "query": {
                    "group_by": "territory",
                    "metrics": [
                        {"field": "revenue", "operation": "sum", "label": "total"}
                    ],
                },
                "x_field": "territory",
                "y_fields": ["total"],
            },
        )
        db.commit()
        assert created.created_ids, created.content
        dashboard_id = created.created_ids[0]
        run = Run(
            workspace_id=workspace_id,
            conversation_id="",
            agent_id=boot["default_agent_id"],
            created_by=user_id,
            status="completed",
            prompt="rename it",
        )
        db.add(run)
        db.commit()
        pending = checkpoints.capture_before(
            db,
            context,
            "update_dashboard",
            {"dashboard_id": dashboard_id, "name": name},
        )
        assert pending is not None
        dashboard = db.get(Dashboard, dashboard_id)
        dashboard.name = f"{name} renamed"
        db.flush()
        checkpoints.record_checkpoint(
            db,
            run=run,
            tool_call_id="reserialize-1",
            name="update_dashboard",
            pending=pending,
            result=ToolResult(content="updated"),
        )
        db.commit()
        # A benign round trip: identical spec, different key order and spacing.
        dashboard = db.get(Dashboard, dashboard_id)
        spec = json.loads(dashboard.spec_json)
        dashboard.spec_json = json.dumps(
            dict(sorted(spec.items(), reverse=True)), indent=2
        )
        db.commit()
        assert dashboard.spec_json != json.dumps(spec)

        rows = list(
            db.scalars(select(RunCheckpoint).where(RunCheckpoint.run_id == run.id))
        )
        assert json.loads(rows[0].before_json)["after"], (
            "the row is guarded — otherwise this proves nothing"
        )
        reverted, skipped = checkpoints.revert_run(
            db, run=db.get(Run, run.id), actor_id=user_id, rows=rows
        )
        db.commit()
        assert skipped == [], skipped
        assert [item["tool_name"] for item in reverted] == ["update_dashboard"]
        assert db.get(Dashboard, dashboard_id).name == name
    finally:
        db.close()


def _created_document(db, context, registry, client):
    title = unique("Attributed doc")
    result = registry["create_document"].executor(
        db, context, {"title": title, "content": "fresh"}
    )
    return result, db.scalar(
        select(Document).where(
            Document.workspace_id == context.workspace_id, Document.title == title
        )
    )


def _created_board(db, context, registry, client):
    name = unique("Attributed board")
    result = registry["create_board"].executor(db, context, {"name": name})
    return result, db.scalar(
        select(Board).where(
            Board.workspace_id == context.workspace_id, Board.name == name
        )
    )


def _created_project(db, context, registry, client):
    name = unique("Attributed project")
    result = registry["create_project"].executor(
        db, context, {"name": name, "kind": "web"}
    )
    return result, db.scalar(
        select(Project).where(
            Project.workspace_id == context.workspace_id, Project.name == name
        )
    )


def _dashboard_args(name: str, dataset_id: str) -> dict:
    return {
        "name": name,
        "dataset_id": dataset_id,
        "visualization": "bar",
        "query": {
            "group_by": "territory",
            "metrics": [{"field": "revenue", "operation": "sum", "label": "total"}],
        },
        "x_field": "territory",
        "y_fields": ["total"],
    }


def _created_dashboard(db, context, registry, client):
    dataset = make_dataset(client)
    name = unique("Attributed dashboard")
    result = registry["create_dashboard"].executor(
        db, context, _dashboard_args(name, dataset["id"])
    )
    return result, db.scalar(
        select(Dashboard).where(
            Dashboard.workspace_id == context.workspace_id, Dashboard.name == name
        )
    )


TEMPLATE_ARGS = {
    "required_columns": [
        {"name": "region", "type": "string"},
        {"name": "amount", "type": "number"},
    ],
    "visualization": "bar",
    "query": {
        "group_by": "region",
        "metrics": [{"field": "amount", "operation": "sum", "label": "total"}],
    },
    "x_field": "region",
    "y_fields": ["total"],
}


def _created_template(db, context, registry, client):
    name = unique("Attributed template")
    result = registry["create_dashboard_template"].executor(
        db, context, {**TEMPLATE_ARGS, "name": name}
    )
    return result, db.scalar(
        select(DashboardTemplate).where(
            DashboardTemplate.workspace_id == context.workspace_id,
            DashboardTemplate.name == name,
        )
    )


def _bound_template(db, context, registry, client):
    dataset = make_dataset(client)
    template_name = unique("Attributed definition")
    registry["create_dashboard_template"].executor(
        db, context, {**TEMPLATE_ARGS, "name": template_name}
    )
    db.commit()
    name = unique("Attributed binding")
    result = registry["bind_dashboard_template"].executor(
        db,
        context,
        {
            "template": template_name,
            "dataset_id": dataset["id"],
            "name": name,
            "column_bindings": {"region": "territory", "amount": "revenue"},
        },
    )
    return result, db.scalar(
        select(Dashboard).where(
            Dashboard.workspace_id == context.workspace_id, Dashboard.name == name
        )
    )


@pytest.mark.parametrize(
    "make",
    [
        _created_document,
        _created_board,
        _created_project,
        _created_dashboard,
        _created_template,
        _bound_template,
    ],
    ids=[
        "create_document",
        "create_board",
        "create_project",
        "create_dashboard",
        "create_dashboard_template",
        "bind_dashboard_template",
    ],
)
def test_every_creating_executor_reports_the_row_it_created(client, make):
    """`created_ids` is the only honest attribution channel a creation has.

    The capture for a create runs *before* the executor and has no id to
    record; it learns one from the result. An executor that forgets to report
    makes its creation invisible to the undo (`finalize` returns None and no
    checkpoint is written) — and the alternative it replaced, set-diffing the
    workspace, attributed whatever else landed in the meantime. So every
    creating executor owes this, not just the one that was tested.
    """
    db = SessionLocal()
    try:
        context = ToolContext(
            workspace_id=DEV_SEED_WORKSPACE_ID,
            user_id=DEV_SEED_USER_ID,
            conversation_id="",
        )
        result, row = make(db, context, build_registry(db, context), client)
        db.commit()
        assert row is not None, result.content
        assert result.created_ids == [row.id], result.content
    finally:
        db.close()
