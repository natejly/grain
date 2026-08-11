"""The sandbox concurrency quota becomes a constraint instead of a count.

A workspace's limit on concurrent sandboxes was enforced by counting: insert a
claim row, count the claims that sort ahead of it, admit it if fewer than the
limit do. Counting cannot enforce it. The ordering key is stamped while the
INSERT is being built and the row becomes visible only at COMMIT, so two
overlapping creates can commit in the opposite order to the one they sort in —
and then each of them counts nothing ahead of itself and each is admitted. The
result is two live machines against a quota of one: a real billing escape, and
the escape of the mechanism that is supposed to stop a runaway workflow spawning
machines.

So concurrency is expressed as `limit` numbered slots and a row that holds one
names it. `slot_index` is unique per workspace, which makes the second claim on a
number fail its own INSERT rather than pass a check — the one serialisation
primitive SQLite and Postgres genuinely share, unlike `FOR UPDATE` (ignored by
one) and the single-writer lock (absent from the other).

NULL is "holds no slot", and NULLs do not collide in a unique index on either
engine, so a killed, errored or retired row simply writes NULL and hands the
number back. That is also why the index is over the whole table rather than a
partial index over live statuses: it needs no dialect-specific predicate and no
list of statuses duplicated in SQL.

Existing live rows are numbered per workspace, oldest first, so the constraint
starts out describing the sessions that are already running. A workspace holding
more live sessions than the current limit keeps them — this migration does not
kill anyone's machine — and simply admits no new ones until it is back under.

Revision ID: 0024_sandbox_slot_index
Revises: 0023_spend_limits
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0024_sandbox_slot_index"
down_revision = "0023_spend_limits"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_sandbox_sessions_workspace_slot"

#: The statuses that still hold a slot, mirroring `LIVE_STATUSES` plus the
#: `starting` claim in sandbox/session.py. Spelled out rather than imported:
#: a migration has to keep describing the schema as it was when it ran.
HOLDING_STATUSES = ("running", "paused", "starting")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("sandbox_sessions")}

    if "slot_index" not in columns:
        op.add_column(
            "sandbox_sessions", sa.Column("slot_index", sa.Integer(), nullable=True)
        )

    # Numbered before the index exists, so a database that somehow already holds
    # duplicates fails on the CREATE INDEX with the rows still visible to whoever
    # has to look at them, rather than half-numbered.
    holding = sa.bindparam("statuses", expanding=True)
    rows = bind.execute(
        sa.text(
            "SELECT id, workspace_id FROM sandbox_sessions "
            "WHERE status IN :statuses AND slot_index IS NULL "
            "ORDER BY workspace_id, created_at, id"
        ).bindparams(holding),
        {"statuses": list(HOLDING_STATUSES)},
    ).fetchall()

    next_slot: dict[str, int] = {}
    for row_id, workspace_id in rows:
        index = next_slot.get(workspace_id, 0)
        next_slot[workspace_id] = index + 1
        bind.execute(
            sa.text("UPDATE sandbox_sessions SET slot_index = :slot WHERE id = :id"),
            {"slot": index, "id": row_id},
        )

    existing = {index["name"] for index in inspector.get_indexes("sandbox_sessions")}
    if INDEX_NAME not in existing:
        op.create_index(
            INDEX_NAME,
            "sandbox_sessions",
            ["workspace_id", "slot_index"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="sandbox_sessions")
    op.drop_column("sandbox_sessions", "slot_index")
