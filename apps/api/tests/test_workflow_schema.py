"""The workflow tables, and the promises their shape is supposed to make.

Two things are being proved here. First, that a graph can be stored and its runs
tracked per node — the ordinary round trip. Second, and more interesting, that
the migration and the ORM agree: development and test schemas come from
`create_all` and production comes from alembic, so a column that exists in only
one of them is a bug nobody sees until deploy.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal, engine
from app.models import Workflow, WorkflowNodeRun, WorkflowRun

WORKFLOW_TABLES = ("workflows", "workflow_runs", "workflow_node_runs")
API_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workspace(client):
    identity = client.get("/api/bootstrap").json()["identity"]
    return identity["workspace_id"], identity["user_id"]


def _graph() -> str:
    return json.dumps(
        {
            "name": "Monday PR digest",
            "nodes": [{"id": "gather", "kind": "tool", "tool": "search_sources"}],
            "edges": [],
        }
    )


def test_a_workflow_and_its_run_round_trip(workspace):
    workspace_id, user_id = workspace
    db = SessionLocal()
    try:
        workflow = Workflow(
            workspace_id=workspace_id,
            created_by=user_id,
            name="Monday PR digest",
            source_prompt="every Monday, summarise the open PRs",
            graph_json=_graph(),
            trigger_kind="schedule",
            schedule_cron="0 9 * * 1",
        )
        db.add(workflow)
        db.flush()
        run = WorkflowRun(
            workspace_id=workspace_id,
            workflow_id=workflow.id,
            created_by=user_id,
            workflow_version=workflow.version,
            graph_json=workflow.graph_json,
            trigger="schedule",
        )
        db.add(run)
        db.flush()
        db.add(
            WorkflowNodeRun(
                workspace_id=workspace_id,
                workflow_run_id=run.id,
                node_key="gather",
                tool_name="search_sources",
                status="succeeded",
                policy="allow",
            )
        )
        db.commit()

        stored = db.get(WorkflowRun, run.id)
        assert stored is not None
        assert stored.status == "queued"
        assert stored.run_id is None  # no chat run needed until a node wants one
        node = db.query(WorkflowNodeRun).filter_by(workflow_run_id=run.id).one()
        assert (node.node_key, node.policy) == ("gather", "allow")
    finally:
        db.close()


def test_a_node_cannot_be_recorded_twice_in_one_run(workspace):
    """The constraint that makes "skip completed nodes" a fact rather than a habit.

    A resumed executor decides what to re-run by reading these rows. If the same
    node could be inserted twice, a resume after a crash could execute a
    succeeded node again — and for a node that already sent an email, "at least
    once" is the wrong number.
    """
    workspace_id, user_id = workspace
    db = SessionLocal()
    try:
        workflow = Workflow(
            workspace_id=workspace_id,
            created_by=user_id,
            name="dup",
            graph_json=_graph(),
        )
        db.add(workflow)
        db.flush()
        run = WorkflowRun(
            workspace_id=workspace_id, workflow_id=workflow.id, created_by=user_id
        )
        db.add(run)
        db.flush()
        for _ in range(2):
            db.add(
                WorkflowNodeRun(
                    workspace_id=workspace_id, workflow_run_id=run.id, node_key="gather"
                )
            )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_every_workflow_table_is_workspace_scoped():
    """No row in this feature exists outside a workspace.

    The isolation suite proves queries filter; this proves there is a column to
    filter on in the first place, which is the part a new table gets wrong.
    """
    for model in (Workflow, WorkflowRun, WorkflowNodeRun):
        columns = model.__table__.columns
        assert "workspace_id" in columns, model.__tablename__
        assert not columns["workspace_id"].nullable, model.__tablename__


def test_the_migration_chain_builds_the_tables_the_orm_declares():
    """`alembic upgrade head` from an empty database must match `create_all`.

    Not just "0019 runs" — the whole chain, on a scratch database, because a
    migration that only works when applied on top of a schema someone already had
    is a migration that fails on the first new deployment. Then the columns and
    indexes are compared against the ORM: production gets the alembic schema and
    development gets the metadata schema, and a difference between them is a bug
    that only ever appears in production.
    """
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
        assert set(WORKFLOW_TABLES) <= set(migrated.get_table_names())
        declared = inspect(engine)
        for table in WORKFLOW_TABLES:
            assert {column["name"] for column in migrated.get_columns(table)} == {
                column["name"] for column in declared.get_columns(table)
            }, table
            assert {index["name"] for index in migrated.get_indexes(table)} >= {
                index["name"] for index in declared.get_indexes(table)
            }, table
