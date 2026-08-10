"""Pausable agent loop: persisted loop state, tool decisions, approval policies.

Revision ID: 0006_agent_approvals
Revises: 0005_app_kind
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006_agent_approvals"
down_revision = "0005_app_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    run_columns = {column["name"] for column in inspector.get_columns("runs")}
    if "agent_state_json" not in run_columns:
        op.add_column("runs", sa.Column("agent_state_json", sa.Text(), nullable=True))

    call_columns = {
        column["name"] for column in inspector.get_columns("agent_tool_calls")
    }
    if "call_id" not in call_columns:
        op.add_column(
            "agent_tool_calls",
            sa.Column("call_id", sa.String(80), nullable=False, server_default=""),
        )
    if "decided_by" not in call_columns:
        op.add_column(
            "agent_tool_calls", sa.Column("decided_by", sa.String(36), nullable=True)
        )
    if "decided_at" not in call_columns:
        op.add_column(
            "agent_tool_calls", sa.Column("decided_at", sa.DateTime(), nullable=True)
        )

    if "tool_policies" not in set(inspector.get_table_names()):
        op.create_table(
            "tool_policies",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("tool_name", sa.String(120), nullable=False),
            sa.Column(
                "policy", sa.String(16), nullable=False, server_default="ask"
            ),
            sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "tool_name"),
        )
        op.create_index(
            "ix_tool_policies_workspace_id", "tool_policies", ["workspace_id"]
        )


def downgrade() -> None:
    op.drop_index("ix_tool_policies_workspace_id", table_name="tool_policies")
    op.drop_table("tool_policies")
    op.drop_column("agent_tool_calls", "decided_at")
    op.drop_column("agent_tool_calls", "decided_by")
    op.drop_column("agent_tool_calls", "call_id")
    op.drop_column("runs", "agent_state_json")
