"""One OPEN monitor alert per monitor, enforced by the database.

`monitors._open_alert_exists` narrowed the duplicate-alert race (a claim-free
run-now evaluating the same crossing concurrently with the tick) but is a
check-then-insert. This partial unique index on `notifications.monitor_id`
WHERE the row is an open monitor_alert closes it: the loser's commit raises,
and `monitors.evaluate` already turns any failure into a skip. Scoped to real
monitor ids, so every other notification kind (monitor_id '') is untouched.

Before creating the index, any duplicates the race already produced are
resolved down to one surviving open row per monitor — otherwise the CREATE
UNIQUE INDEX itself would fail on exactly the databases that need it.

Revision ID: 0064_open_alert_unique
Revises: 0063_digests
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0064_open_alert_unique"
down_revision = "0063_digests"
branch_labels = None
depends_on = None

INDEX = "uq_notifications_open_monitor_alert"
WHERE = "kind = 'monitor_alert' AND status = 'open' AND monitor_id != ''"


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    # Table-existence first (the house guard template): the replay test runs this
    # chain against a deliberately partial legacy database, and inspecting an
    # absent table raises rather than answering "none". A database built from
    # empty already has the index from `create_all`, which the guard no-ops.
    if not sa.inspect(op.get_bind()).has_table("notifications"):
        return
    if INDEX in _indexes("notifications"):
        return
    # Duplicates the check-then-insert race already landed: keep one open row
    # per monitor, resolve the rest so the unique index can be built.
    op.execute(
        sa.text(
            "UPDATE notifications SET status = 'resolved' "
            f"WHERE {WHERE} AND id NOT IN ("
            "SELECT keep FROM (SELECT MAX(id) AS keep FROM notifications "
            f"WHERE {WHERE} GROUP BY monitor_id) AS survivors)"
        )
    )
    op.create_index(
        INDEX,
        "notifications",
        ["monitor_id"],
        unique=True,
        sqlite_where=sa.text(WHERE),
        postgresql_where=sa.text(WHERE),
    )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("notifications"):
        return
    if INDEX in _indexes("notifications"):
        op.drop_index(INDEX, table_name="notifications")
