"""Per-agent cost attribution, and the claim table system sweeps share.

Two small things. `model_usage` gains an `agent_id` column — a plain
'' -unset string like its `run_id`, frozen at write time so the ledger
outlives the agent it bills; every pre-existing row means what it always
meant ("no agent recorded"). And a new `sweep_claims` table (name PK,
last_dispatched_at) gives tick-riding sweeps with no row of their own — the
hourly spend watch now, the daily digest later — the same conditional-UPDATE
at-most-once claim the workflow/cron/monitor rows carry themselves. It is
deliberately workspace-less: ticker infrastructure, like alembic_version,
holding a name and a timestamp and nothing anyone owns.

Revision ID: 0050_usage_agent
Revises: 0049_run_checkpoints
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0050_usage_agent"
down_revision = "0049_run_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Guarded like 0024-0049: a database migrated from empty already got both
    # the column and the table from `create_all`. has_table BEFORE get_columns,
    # because the replay test runs this chain against a deliberately partial
    # legacy database.
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "model_usage" in tables:
        columns = {column["name"] for column in inspector.get_columns("model_usage")}
        if "agent_id" not in columns:
            op.add_column(
                "model_usage",
                sa.Column(
                    "agent_id", sa.String(36), nullable=False, server_default=""
                ),
            )
    if "sweep_claims" not in tables:
        op.create_table(
            "sweep_claims",
            sa.Column("name", sa.String(length=64), primary_key=True),
            sa.Column("last_dispatched_at", sa.DateTime(), nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "model_usage" in tables:
        columns = {column["name"] for column in inspector.get_columns("model_usage")}
        if "agent_id" in columns:
            op.drop_column("model_usage", "agent_id")
    if "sweep_claims" in tables:
        op.drop_table("sweep_claims")
