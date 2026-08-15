"""0040 against a genuinely pre-0040 database.

Worth its own module because the ordinary chain proves nothing about it. 0001
builds the schema with `Base.metadata.create_all`, so a database migrated from
empty already carries `owner_id` by the time 0040 runs and both of its rebuild
branches are skipped — `alembic upgrade head` on a fresh file exercises the
guards and not the migration. The only way to run the code that actually moves
customer data is to construct the old schema, which is what this does.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]

#: The shape of the two tables immediately before 0040 — memory_items as 0003
#: created it plus 0014's `superseded_by`, tool_policies as 0020 rebuilt it.
LEGACY_SCHEMA = """
CREATE TABLE workspaces (id VARCHAR(36) PRIMARY KEY, name VARCHAR(120));
CREATE TABLE conversations (id VARCHAR(36) PRIMARY KEY);
CREATE TABLE memory_items (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id),
    conversation_id VARCHAR(36) REFERENCES conversations(id),
    run_id VARCHAR(36) NOT NULL DEFAULT '',
    kind VARCHAR(24) NOT NULL DEFAULT 'fact',
    content TEXT NOT NULL,
    normalized_key VARCHAR(200) NOT NULL,
    entity_names_json TEXT NOT NULL DEFAULT '[]',
    message_ids_json TEXT NOT NULL DEFAULT '[]',
    importance INTEGER NOT NULL DEFAULT 1,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    superseded_by VARCHAR(36),
    embedding BLOB,
    embedding_model VARCHAR(64) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE (workspace_id, kind, normalized_key)
);
CREATE INDEX ix_memory_items_workspace_id ON memory_items (workspace_id);
CREATE INDEX ix_memory_items_conversation_id ON memory_items (conversation_id);
CREATE INDEX ix_memory_items_workspace_status ON memory_items (workspace_id, status);
CREATE INDEX ix_memory_items_ws_status_updated
    ON memory_items (workspace_id, status, updated_at);
CREATE TABLE tool_policies (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id),
    tool_name VARCHAR(120) NOT NULL,
    policy VARCHAR(16) NOT NULL DEFAULT 'ask',
    scope VARCHAR(16) NOT NULL DEFAULT 'chat',
    created_by VARCHAR(36) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    UNIQUE (workspace_id, tool_name, scope)
);
CREATE INDEX ix_tool_policies_workspace_id ON tool_policies (workspace_id);
CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
INSERT INTO alembic_version VALUES ('0039_invite_revocation');
INSERT INTO workspaces VALUES ('ws1', 'Legacy');
"""

VECTOR = b"\x01\x02\x03not-a-real-embedding"


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    db.executescript(LEGACY_SCHEMA)
    db.execute(
        "INSERT INTO memory_items (id, workspace_id, conversation_id, run_id, kind,"
        " content, normalized_key, entity_names_json, message_ids_json, importance,"
        " status, superseded_by, embedding, embedding_model, created_at, updated_at)"
        " VALUES ('m1','ws1',NULL,'r1','fact','the api deploys on railway',"
        "'api|deploy_host','[\"api\"]','[\"msg1\"]',4,'active',NULL,?,'text-3',"
        "'2026-01-01 00:00:00','2026-01-02 00:00:00')",
        (VECTOR,),
    )
    db.executemany(
        "INSERT INTO tool_policies VALUES (?,?,?,?,?,?,'2026-01-01','2026-01-01')",
        [
            ("p1", "ws1", "send_email", "allow", "chat", "user-nate"),
            ("p2", "ws1", "send_email", "allow", "workflow", "user-nate"),
            # Written before anyone was attributed. "" is already the sentinel
            # for shared, so this row needs no special case in the backfill.
            ("p3", "ws1", "run_python", "deny", "chat", ""),
        ],
    )
    db.commit()
    db.close()
    return path


def _alembic(path: Path, *args: str) -> None:
    """Run alembic against `path`, in a subprocess, as an operator would.

    Out of process because `alembic/env.py` overrides `sqlalchemy.url` with
    `get_settings().database_url` unconditionally — so an in-process
    `command.upgrade` with a url set on the Config silently migrates *the test
    suite's own database* instead of the temporary one, which is both a useless
    measurement and a destructive one. The env var is the only supported way to
    point it somewhere else, and it has to be set before the process starts.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        env={
            **os.environ,
            "DATABASE_URL": f"sqlite:///{path}",
            "PYTHONPATH": str(API_ROOT),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-3000:]


def test_0040_gives_memory_no_owner_and_policies_the_one_they_recorded(legacy_db: Path):
    """The two backfills differ, on purpose.

    Memory has no honest owner to recover — `importance` counts how many runs
    re-touched a row — and narrowing it would take memories out of members'
    recall, which is data-loss-shaped. Policies do: `created_by` is recorded
    fact, and narrowing a grant only means being asked again.
    """
    _alembic(legacy_db, "upgrade", "head")

    db = sqlite3.connect(legacy_db)
    try:
        memory = db.execute(
            "SELECT owner_id, content, importance, embedding, message_ids_json"
            " FROM memory_items"
        ).fetchall()
        assert memory == [
            ("", "the api deploys on railway", 4, VECTOR, '["msg1"]')
        ]
        policies = dict(db.execute("SELECT id, owner_id FROM tool_policies"))
        assert policies == {"p1": "user-nate", "p2": "user-nate", "p3": ""}
    finally:
        db.close()


def test_0040_leaves_the_widened_key_enforcing_on_both_sides(legacy_db: Path):
    """The property the rebuild exists for, and the one it must not break.

    A personal row and the workspace's row coexist on one claim key — that is
    supersession staying inside a scope. Two *shared* rows on one claim key are
    still refused, which is the half a nullable `owner_id` would have silently
    dropped: NULLs are distinct inside a unique index on both engines, so the
    constraint would have stopped constraining exactly the rows it protects.
    """
    _alembic(legacy_db, "upgrade", "head")
    db = sqlite3.connect(legacy_db)
    try:
        insert = (
            "INSERT INTO memory_items (id, workspace_id, owner_id, conversation_id,"
            " run_id, kind, content, normalized_key, entity_names_json,"
            " message_ids_json, importance, status, superseded_by, embedding,"
            " embedding_model, created_at, updated_at) VALUES (?,'ws1',?,NULL,'r2',"
            "'fact',?,'api|deploy_host','[]','[]',1,'active',NULL,NULL,'',"
            "'2026-01-03','2026-01-03')"
        )
        db.execute(insert, ("m2", "user-nate", "the api deploys on fly"))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(insert, ("m3", "", "the api deploys somewhere else"))
    finally:
        db.close()


def test_0040_downgrades_by_keeping_what_the_workspace_holds(legacy_db: Path):
    """Collapsing the owner out of the key means choosing which row survives.

    Both answers are "the shared one". For memory that is every row the upgrade
    produced, so nothing is lost. For policies it drops the personal grants,
    which fails closed — a dropped grant asks again.
    """
    _alembic(legacy_db, "upgrade", "head")
    _alembic(legacy_db, "downgrade", "0039_invite_revocation")

    db = sqlite3.connect(legacy_db)
    try:
        assert [row[0] for row in db.execute("SELECT id FROM memory_items")] == ["m1"]
        assert [row[0] for row in db.execute("SELECT id FROM tool_policies")] == ["p3"]
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(memory_items)")
        }
        assert "owner_id" not in columns
        # And nothing left half-renamed behind it.
        leftovers = [
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
                " AND (name LIKE '%_pre_owner' OR name LIKE '%_owned')"
            )
        ]
        assert leftovers == []
    finally:
        db.close()
