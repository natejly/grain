"""Metric monitors: a threshold question asked of a dataset on a schedule.

One new table, on the Cron's shape: the same `schedule_cron` + IANA-zone pair,
the same `last_dispatched_at` conditional-UPDATE claim advanced by the shared
tick. A monitor only ever *reads* — it runs its stored `DatasetQuery`, compares
the first metric of the first row against `threshold`, and on the ok→tripped
edge (detected via `last_state`) writes one `monitor_alert` notification for
every member. `dataset_id` is a plain column so a monitor outlives a purged
dataset and skips honestly instead of dangling a foreign key.

The composite (workspace_id, enabled) index is the sweep's scan shape: every
enabled monitor of a workspace, once a minute.

Revision ID: 0056_monitors
Revises: 0055_assigned_approvals
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0056_monitors"
down_revision = "0055_assigned_approvals"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_monitors_workspace_id", ["workspace_id"]),
    ("ix_monitors_workspace_enabled", ["workspace_id", "enabled"]),
)


def upgrade() -> None:
    # Guarded like 0024-0047: a database migrated from empty already got this
    # table from `create_all`, and creating it again would fail. has_table is
    # checked BEFORE any inspection of the table, because the replay test runs
    # this chain against a deliberately partial legacy database.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "monitors" in tables:
        return
    op.create_table(
        "monitors",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(length=36),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            sa.String(length=36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("dataset_id", sa.String(length=36), nullable=False),
        sa.Column("query_json", sa.Text(), nullable=False),
        sa.Column("comparator", sa.String(length=8), nullable=False),
        sa.Column("threshold", sa.Float(), nullable=False),
        sa.Column("schedule_cron", sa.String(length=120), nullable=False),
        sa.Column("schedule_timezone", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_dispatched_at", sa.DateTime(), nullable=True),
        sa.Column("last_value_json", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "last_state", sa.String(length=16), nullable=False, server_default=""
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    for name, cols in _INDEXES:
        op.create_index(name, "monitors", cols)


def downgrade() -> None:
    # A monitor is a stored question, never data: dropping the table loses
    # watchfulness, not facts. Guarded symmetrically.
    inspector = sa.inspect(op.get_bind())
    if "monitors" not in inspector.get_table_names():
        return
    live = {index["name"] for index in inspector.get_indexes("monitors")}
    for name, _cols in _INDEXES:
        if name in live:
            op.drop_index(name, table_name="monitors")
    op.drop_table("monitors")
