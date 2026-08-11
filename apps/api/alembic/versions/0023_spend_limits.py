"""A spend ceiling: an owner-settable limit, and a reason a run is parked.

0022 made spend measurable. This makes it enforceable, and the schema carries
two decisions from ADR 0008.

*The ceiling is a row, not only an environment variable.* A limit that can only
be raised by a redeploy cannot be raised by the person watching a parked run at
3am, and a run parked on budget with no way to release it is a worse outage than
the overspend it prevented. `workspace_budgets` holds at most one row per
workspace; absent, the deployment-wide setting applies.

*The park is the existing park.* A run stopped by the ceiling is
`waiting_for_approval` with `paused_reason='budget'`, on the same durable,
resumable, audited path an approval uses. A second status would have meant
editing every guard, sweep and filter that already reads `waiting_for_approval`
as "waiting on a person" — each of them correct about a budget park already, and
each of them a place to forget. So the distinction is a column, and it is a
column on both tables because the workflow surface reads `workflow_runs` and
must not label a spend stop as an approval nobody can give.

Both columns are NOT NULL with a "" server default, so every row that predates
this migration reads as "not parked for any special reason", which is what it is.

Revision ID: 0023_spend_limits
Revises: 0022_model_usage
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0023_spend_limits"
down_revision = "0022_model_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "workspace_budgets" not in tables:
        op.create_table(
            "workspace_budgets",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
                unique=True,
            ),
            sa.Column(
                "window_hours", sa.Integer(), nullable=False, server_default="24"
            ),
            # Null is "no limit of this kind", the same answer an unset setting
            # gives. There is no sentinel meaning "inherit": the row is the whole
            # ceiling, so reading one is never a question about something else.
            sa.Column("usd_per_window", sa.Float(), nullable=True),
            sa.Column("tokens_per_window", sa.Integer(), nullable=True),
            sa.Column(
                "updated_by", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_workspace_budgets_workspace_id",
            "workspace_budgets",
            ["workspace_id"],
        )

    for table in ("runs", "workflow_runs"):
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "paused_reason" not in columns:
            op.add_column(
                table,
                sa.Column(
                    "paused_reason",
                    sa.String(length=16),
                    nullable=False,
                    server_default="",
                ),
            )


def downgrade() -> None:
    op.drop_column("workflow_runs", "paused_reason")
    op.drop_column("runs", "paused_reason")
    op.drop_index(
        "ix_workspace_budgets_workspace_id", table_name="workspace_budgets"
    )
    op.drop_table("workspace_budgets")
