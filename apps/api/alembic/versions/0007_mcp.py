"""MCP servers and their cached tool discovery.

Revision ID: 0007_mcp
Revises: 0006_agent_approvals
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007_mcp"
down_revision = "0006_agent_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "mcp_servers" not in tables:
        op.create_table(
            "mcp_servers",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(60), nullable=False),
            sa.Column("transport", sa.String(16), nullable=False, server_default="stdio"),
            sa.Column("command", sa.String(400), nullable=False, server_default=""),
            sa.Column("args_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("secrets_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column("url", sa.String(600), nullable=False, server_default=""),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("status", sa.String(24), nullable=False, server_default="unknown"),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("last_connected_at", sa.DateTime(), nullable=True),
            sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "name"),
        )
        op.create_index("ix_mcp_servers_workspace_id", "mcp_servers", ["workspace_id"])

    if "mcp_tools" not in tables:
        op.create_table(
            "mcp_tools",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "server_id",
                sa.String(36),
                sa.ForeignKey("mcp_servers.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "input_schema_json", sa.Text(), nullable=False, server_default="{}"
            ),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("server_id", "name"),
        )
        op.create_index("ix_mcp_tools_workspace_id", "mcp_tools", ["workspace_id"])
        op.create_index("ix_mcp_tools_server_id", "mcp_tools", ["server_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_tools_server_id", table_name="mcp_tools")
    op.drop_index("ix_mcp_tools_workspace_id", table_name="mcp_tools")
    op.drop_table("mcp_tools")
    op.drop_index("ix_mcp_servers_workspace_id", table_name="mcp_servers")
    op.drop_table("mcp_servers")
