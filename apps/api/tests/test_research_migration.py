"""0073 against both shapes of database it will ever meet.

Worth its own module because neither shape proves the other. 0001 builds the
schema with `Base.metadata.create_all`, so a database migrated from empty
already carries every 0073 column by the time 0073 runs and every guard skips —
`alembic upgrade head` on a fresh file exercises the guards and not the adds.
The only way to run the adds is to construct a database that genuinely lacks
them, which is what `legacy_db` does: the pre-0073 tables by hand, stamped at
0072, upgraded the way a deploy does it.

Both shapes matter in production. A long-lived deployment is the first; a
freshly provisioned one is the second, and it is exactly the database an
unguarded `drop_column` in `downgrade()` would fail on.

Run out of process because `alembic/env.py` overrides `sqlalchemy.url` from the
settings, so the environment variable is the only way to point it at a
temporary database rather than the suite's own.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app.database import engine

API_ROOT = Path(__file__).resolve().parents[1]

HEAD = "0073_research_surfaces"
PREVIOUS = "0072_identity_styles_feedback"

#: The tables 0073 adds columns to, in the shape they had before it — enough of
#: each for `add_column` to land, and nothing more. Hand-written because the
#: chain cannot produce this state: 0001's `create_all` builds today's models.
LEGACY_SCHEMA = """
CREATE TABLE workspaces (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    name VARCHAR(120) NOT NULL DEFAULT ''
);
CREATE TABLE conversations (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id),
    title VARCHAR(200) NOT NULL DEFAULT ''
);
CREATE TABLE runs (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id),
    status VARCHAR(32) NOT NULL DEFAULT 'queued'
);
CREATE TABLE messages (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id),
    content TEXT NOT NULL DEFAULT '',
    citation_report_json TEXT NOT NULL DEFAULT ''
);
CREATE TABLE notifications (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id),
    kind VARCHAR(32) NOT NULL DEFAULT 'mention',
    status VARCHAR(16) NOT NULL DEFAULT 'open'
);
CREATE TABLE agent_tool_calls (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id),
    name VARCHAR(80) NOT NULL DEFAULT '',
    status VARCHAR(24) NOT NULL DEFAULT 'succeeded',
    assigned_to VARCHAR(36) NOT NULL DEFAULT ''
);
"""

#: Everything 0073 creates, and the column it adds to each existing table.
NEW_TABLES = (
    "pages",
    "page_citations",
    "coverage_ledgers",
    "coverage_entries",
    "deliverable_manifests",
    "manifest_files",
    "watches",
    "watch_observations",
    "grounded_receipts",
)
NEW_COLUMNS = {
    "messages": {"followups_json"},
    "notifications": {"page_id", "watch_id"},
    "runs": {"preset", "retrieval_budget", "step_plan", "prompt_fingerprint"},
    "conversations": {"default_preset"},
    "agent_tool_calls": {"gate_reason"},
    "workspaces": {"taint_gating"},
}


def _alembic(path: Path, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        env={
            **os.environ,
            "DATABASE_URL": f"sqlite:///{path}",
            "APP_ENV": "test",
            "PYTHONPATH": str(API_ROOT),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return result.stdout


def _names(path: Path, sql: str) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {row[0] for row in db.execute(sql)}
    finally:
        db.close()


def _tables(path: Path) -> set[str]:
    return _names(path, "SELECT name FROM sqlite_master WHERE type='table'")


def _columns(path: Path, table: str) -> set[str]:
    db = sqlite3.connect(path)
    try:
        return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    finally:
        db.close()


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    """A database that genuinely predates 0073, stamped at 0072."""
    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    try:
        db.executescript(LEGACY_SCHEMA)
        db.commit()
    finally:
        db.close()
    _alembic(path, "stamp", PREVIOUS)
    return path


def test_0073_adds_everything_on_a_database_that_lacks_it(legacy_db: Path) -> None:
    """The adds themselves — the half a fresh database never runs."""
    for table, columns in NEW_COLUMNS.items():
        assert not (_columns(legacy_db, table) & columns), table

    _alembic(legacy_db, "upgrade", HEAD)

    assert set(NEW_TABLES) <= _tables(legacy_db)
    for table, columns in NEW_COLUMNS.items():
        assert columns <= _columns(legacy_db, table), table


def test_0073_server_defaults_are_the_historical_truth(legacy_db: Path) -> None:
    """A row written before the migration reads as "unset", not as a claim.

    No backfill is the whole design: nothing was ever computed, published or
    fingerprinted, no call was raised by the taint gate, and every workspace
    followed the deployment default. A row inserted before 0073 must say so.
    """
    db = sqlite3.connect(legacy_db)
    try:
        db.execute("INSERT INTO workspaces (id, name) VALUES ('w', 'old')")
        db.execute(
            "INSERT INTO messages (id, workspace_id, content) VALUES ('m', 'w', 'hi')"
        )
        db.execute("INSERT INTO runs (id, workspace_id) VALUES ('r', 'w')")
        db.execute(
            "INSERT INTO conversations (id, workspace_id) VALUES ('c', 'w')"
        )
        db.execute(
            "INSERT INTO agent_tool_calls (id, workspace_id, name)"
            " VALUES ('a', 'w', 'search_sources')"
        )
        db.commit()
    finally:
        db.close()

    _alembic(legacy_db, "upgrade", HEAD)

    db = sqlite3.connect(legacy_db)
    try:
        assert db.execute("SELECT followups_json FROM messages").fetchone() == ("",)
        assert db.execute(
            "SELECT preset, retrieval_budget, step_plan, prompt_fingerprint FROM runs"
        ).fetchone() == ("", "", 0, "")
        assert db.execute(
            "SELECT default_preset FROM conversations"
        ).fetchone() == ("",)
        assert db.execute(
            "SELECT gate_reason FROM agent_tool_calls"
        ).fetchone() == ("",)
        assert db.execute("SELECT taint_gating FROM workspaces").fetchone() == ("",)
    finally:
        db.close()


def test_0073_replays_on_a_legacy_database(legacy_db: Path) -> None:
    """Upgrade, downgrade, upgrade — the guards hold in both directions."""
    _alembic(legacy_db, "upgrade", HEAD)
    _alembic(legacy_db, "downgrade", PREVIOUS)

    assert not (set(NEW_TABLES) & _tables(legacy_db))
    for table, columns in NEW_COLUMNS.items():
        assert not (_columns(legacy_db, table) & columns), table

    _alembic(legacy_db, "upgrade", HEAD)
    assert set(NEW_TABLES) <= _tables(legacy_db)


def test_0073_replays_on_a_create_all_database(tmp_path: Path) -> None:
    """The freshly provisioned shape, where every guard skips on the way up.

    This is the database the downgrade guards exist for: 0001 builds today's
    models, so `drop_table`/`drop_column` meet objects the migration never
    created, and an unguarded drop would fail on exactly the databases built
    cleanly from scratch.
    """
    path = tmp_path / "fresh.db"
    _alembic(path, "upgrade", "head")
    assert set(NEW_TABLES) <= _tables(path)

    _alembic(path, "downgrade", PREVIOUS)
    _alembic(path, "upgrade", "head")

    assert set(NEW_TABLES) <= _tables(path)
    for table, columns in NEW_COLUMNS.items():
        assert columns <= _columns(path, table), table


def test_the_chain_is_linear_and_0073_is_its_head(tmp_path: Path) -> None:
    """One head, and it is this one — the thing a renumbered parallel cycle
    breaks, and the reason the build-time re-check is in the file's docstring."""
    heads = _alembic(tmp_path / "unused.db", "heads")
    assert HEAD in heads
    assert heads.count("(head)") == 1, heads


def test_the_migration_builds_what_the_orm_declares(tmp_path: Path) -> None:
    """`alembic upgrade head` from empty must match `create_all` — production
    gets the alembic schema, development the metadata one, and a difference is
    a bug that only ever appears in production.

    `messages`, `runs`, `conversations`, `agent_tool_calls` and `workspaces`
    ride along because 0073 edits them too: a new table whose columns match
    while the column it added to an existing table does not would pass a
    tables-only check and still be wrong.
    """
    path = tmp_path / "chain.db"
    _alembic(path, "upgrade", "head")

    migrated = inspect(create_engine(f"sqlite:///{path}"))
    declared = inspect(engine)
    for table in (*NEW_TABLES, *NEW_COLUMNS):
        assert {column["name"] for column in migrated.get_columns(table)} == {
            column["name"] for column in declared.get_columns(table)
        }, table
        assert {index["name"] for index in migrated.get_indexes(table)} >= {
            index["name"] for index in declared.get_indexes(table)
        }, table
