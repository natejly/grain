"""Run checkpoints: the recorded before-state that makes a run undoable.

One new table. Every write-capable agent tool call gets a row captured just
before its executor runs (services/checkpoints): the prior document content,
the board's full snapshot, the project file's bytes, the dashboard's spec —
typed state the undo endpoint can restore, deliberately independent of
`agent_tool_calls.arguments_json` and its 4000-character truncation. Writes
whose effects left the workspace (MCP, sandbox execution, SQL against a
connected database) are recorded `reversible=False` so an undo reports them
honestly as skipped. `reverted_at` marks a checkpoint consumed by an undo, so
undoing the same run twice refuses instead of double-applying.

The composite (workspace_id, run_id, created_at) index is the undo endpoint's
scan shape: one run's checkpoints, newest first.

Revision ID: 0049_run_checkpoints
Revises: 0048_monitors
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0049_run_checkpoints"
down_revision = "0048_monitors"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_run_checkpoints_workspace_id", ["workspace_id"]),
    (
        "ix_run_checkpoints_workspace_run_created",
        ["workspace_id", "run_id", "created_at"],
    ),
)


def upgrade() -> None:
    # Guarded like 0024-0048: a database migrated from empty already got this
    # table from `create_all`, and creating it again would fail. has_table is
    # checked BEFORE any inspection of the table, because the replay test runs
    # this chain against a deliberately partial legacy database.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "run_checkpoints" in tables:
        return
    op.create_table(
        "run_checkpoints",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(length=36),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("tool_call_id", sa.String(length=36), nullable=False),
        sa.Column("tool_name", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("reversible", sa.Boolean(), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=False),
        sa.Column("reverted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for name, cols in _INDEXES:
        op.create_index(name, "run_checkpoints", cols)


def downgrade() -> None:
    # Checkpoints are recovery data, not primary records: dropping the table
    # loses the ability to undo past runs, nothing else. Guarded symmetrically.
    inspector = sa.inspect(op.get_bind())
    if "run_checkpoints" not in inspector.get_table_names():
        return
    live = {index["name"] for index in inspector.get_indexes("run_checkpoints")}
    for name, _cols in _INDEXES:
        if name in live:
            op.drop_index(name, table_name="run_checkpoints")
    op.drop_table("run_checkpoints")
