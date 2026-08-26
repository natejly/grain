"""0064 against a database that already holds the duplicates it exists for.

The ordinary chain proves nothing about the dedup: 0001 builds the schema with
`Base.metadata.create_all`, so a database migrated from empty already carries
the partial unique index by the time 0064 runs and the guard returns before the
UPDATE. The only way to exercise the code that touches customer rows is to
build the pre-0064 table and plant the race's leftovers in it, which is what
this does — the same shape as test_personal_scope_migration.py.

What is asserted is *which* row survives, not merely that one did: the losing
rows are somebody's acknowledgement history, and picking the wrong survivor
leaves the monitor's inbox describing a crossing it is no longer in.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]

#: `notifications` as 0043 created it and 0063 left it — every column 0064
#: reads or writes, and nothing else.
LEGACY_SCHEMA = """
CREATE TABLE notifications (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    workspace_id VARCHAR(36) NOT NULL,
    target_user_id VARCHAR(36) NOT NULL DEFAULT '',
    kind VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'open',
    title VARCHAR(300) NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    conversation_id VARCHAR(36) NOT NULL DEFAULT '',
    document_id VARCHAR(36) NOT NULL DEFAULT '',
    dashboard_id VARCHAR(36) NOT NULL DEFAULT '',
    comment_id VARCHAR(36) NOT NULL DEFAULT '',
    monitor_id VARCHAR(36) NOT NULL DEFAULT '',
    agent_id VARCHAR(36) NOT NULL DEFAULT '',
    created_by VARCHAR(36) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL,
    resolved_at DATETIME,
    resolved_by VARCHAR(36) NOT NULL DEFAULT ''
);
CREATE INDEX ix_notifications_workspace_status_created
    ON notifications (workspace_id, status, created_at);
CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
INSERT INTO alembic_version VALUES ('0063_digests');
"""

#: uuid4s chosen so lexicographic order disagrees with chronological order:
#: the OLDEST row sorts last, which is exactly what the old `MAX(id)` kept.
OLD_ID = "ffffffff-1111-4111-8111-111111111111"
NEW_ID = "00000000-2222-4222-8222-222222222222"
#: Same `created_at` as each other, to pin the tie-break.
TIE_LOW = "11111111-3333-4333-8333-333333333333"
TIE_HIGH = "99999999-3333-4333-8333-333333333333"

ROWS = [
    # monitor-a: a real race, two open rows minutes apart.
    (OLD_ID, "monitor_alert", "open", "monitor-a", "2026-08-01 09:00:00.000000"),
    (NEW_ID, "monitor_alert", "open", "monitor-a", "2026-08-01 09:05:00.000000"),
    # monitor-b: the duplicate landed in the same instant.
    (TIE_LOW, "monitor_alert", "open", "monitor-b", "2026-08-01 10:00:00.000000"),
    (TIE_HIGH, "monitor_alert", "open", "monitor-b", "2026-08-01 10:00:00.000000"),
    # Untouched neighbours: another monitor's single open row, an already
    # resolved one, and the ''-monitor_id kinds the index is scoped away from.
    ("solo", "monitor_alert", "open", "monitor-c", "2026-08-01 11:00:00.000000"),
    ("acked", "monitor_alert", "resolved", "monitor-c", "2026-07-01 11:00:00.000000"),
    ("mention-1", "mention", "open", "", "2026-08-01 12:00:00.000000"),
    ("mention-2", "mention", "open", "", "2026-08-01 12:00:01.000000"),
]


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    path = tmp_path / "alerts.db"
    db = sqlite3.connect(path)
    db.executescript(LEGACY_SCHEMA)
    db.executemany(
        "INSERT INTO notifications (id, workspace_id, kind, status, title,"
        " monitor_id, created_at) VALUES (?, 'ws1', ?, ?, ?, ?, ?)",
        [
            (row_id, kind, status, f"crossing {row_id}", monitor_id, created_at)
            for row_id, kind, status, monitor_id, created_at in ROWS
        ],
    )
    db.commit()
    db.close()
    return path


def _alembic(path: Path, *args: str) -> None:
    """Run alembic against `path`, out of process — `alembic/env.py` overrides
    `sqlalchemy.url` from the settings, so the env var is the only way to point
    it at the temporary database instead of the suite's own."""
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


def _rows(path: Path) -> dict[str, tuple]:
    db = sqlite3.connect(path)
    try:
        return {
            row[0]: row[1:]
            for row in db.execute(
                "SELECT id, status, resolved_at, resolved_by FROM notifications"
            )
        }
    finally:
        db.close()


def test_0064_keeps_the_most_recent_open_alert_not_the_largest_id(legacy_db: Path):
    """The survivor is the newest crossing, and the id it beats sorts higher —
    so a `MAX(id)` over uuid4s would have kept the stale one."""
    _alembic(legacy_db, "upgrade", "0064_open_alert_unique")

    rows = _rows(legacy_db)
    assert rows[NEW_ID][0] == "open"
    assert rows[OLD_ID][0] == "resolved"
    # On identical timestamps the id decides, so two replicas of one database
    # dedup to the same row rather than to whichever one they scanned first.
    assert rows[TIE_HIGH][0] == "open"
    assert rows[TIE_LOW][0] == "resolved"


def test_0064_resolves_the_rows_it_closes_honestly(legacy_db: Path):
    """A closed loser is a resolved notification, not a row in a third state:
    `resolved_at` is stamped, and `resolved_by` stays '' because no member
    acknowledged it."""
    _alembic(legacy_db, "upgrade", "0064_open_alert_unique")

    rows = _rows(legacy_db)
    for closed in (OLD_ID, TIE_LOW):
        status, resolved_at, resolved_by = rows[closed]
        assert status == "resolved"
        assert resolved_at, f"{closed} was closed without a resolved_at"
        assert resolved_by == ""
    # The survivors keep the untouched shape of an open row.
    assert rows[NEW_ID] == ("open", None, "")


def test_0064_leaves_every_row_the_index_does_not_cover_alone(legacy_db: Path):
    _alembic(legacy_db, "upgrade", "0064_open_alert_unique")

    rows = _rows(legacy_db)
    assert rows["solo"][0] == "open"
    assert rows["acked"] == ("resolved", None, "")
    assert rows["mention-1"][0] == "open"
    assert rows["mention-2"][0] == "open"


def test_0064_goes_both_ways_and_can_be_replayed(legacy_db: Path):
    """Guarded in both directions: down drops the index, up rebuilds it, and
    the second upgrade finds nothing left to dedup."""
    _alembic(legacy_db, "upgrade", "0064_open_alert_unique")
    after_first = _rows(legacy_db)

    _alembic(legacy_db, "downgrade", "0063_digests")
    db = sqlite3.connect(legacy_db)
    try:
        indexes = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        assert "uq_notifications_open_monitor_alert" not in indexes
    finally:
        db.close()

    _alembic(legacy_db, "upgrade", "0064_open_alert_unique")
    assert _rows(legacy_db) == after_first
