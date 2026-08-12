"""Personal crons: a recurring job an external cron fires unattended.

One new table, `crons`, and one new `runs` column, `cron_id`. A cron is grounded
on the workflow-schedule machinery — the same `last_dispatched_at` conditional
claim, the same POST /api/workflows/tick — but carries no authority of its own: a
`kind="task"` fire runs at WORKFLOW policy scope, which `policy_scope_for_run`
derives from `runs.cron_id`, so the first write-capable tool parks for a human.
`cron_id` is empty-string-for-unset and deliberately not a foreign key — a run is
a historical record that must outlive the cron's deletion, exactly as `skill_id`
outlives a skill — so a run written before this feature is byte-for-byte today's
behaviour.

Revision ID: 0035_crons
Revises: 0034_skills
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0035_crons"
down_revision = "0034_skills"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _add(table: str, column: sa.Column) -> None:
    """Idempotent add, matching 0029–0034: these databases have been through
    `create_all` as well as alembic, so a column can already be there and the
    migration still has to run to completion."""
    if column.name not in _columns(table):
        op.add_column(table, column)


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "crons" not in tables:
        op.create_table(
            "crons",
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
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="task"),
            sa.Column(
                "schedule_cron", sa.String(length=120), nullable=False, server_default=""
            ),
            sa.Column(
                "schedule_timezone",
                sa.String(length=64),
                nullable=False,
                server_default="UTC",
            ),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "target_conversation_id",
                sa.String(length=36),
                nullable=False,
                server_default="",
            ),
            sa.Column("last_dispatched_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_crons_workspace_id", "crons", ["workspace_id"])
        op.create_index(
            "ix_crons_workspace_enabled", "crons", ["workspace_id", "enabled"]
        )

    _add("runs", sa.Column("cron_id", sa.String(length=36), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("runs", "cron_id")
    op.drop_index("ix_crons_workspace_enabled", table_name="crons")
    op.drop_index("ix_crons_workspace_id", table_name="crons")
    op.drop_table("crons")
