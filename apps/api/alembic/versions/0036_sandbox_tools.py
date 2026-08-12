"""Workspace-defined sandbox tools (custom tool descriptors).

One table, ``sandbox_tools``: each row is a tool the agent can call, executed in
the session sandbox under the row's own egress allowlist and approval policy.
The columns mirror the ORM 1:1 with server_defaults so a database built through
``create_all`` and one built through alembic agree byte-for-byte. Idempotent
``if "sandbox_tools" not in tables`` guard, matching 0034: these databases have
been through ``create_all`` as well as alembic, so the table can already exist
and the migration still has to run to completion.

Revision ID: 0036_sandbox_tools
Revises: 0035_crons
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0036_sandbox_tools"
down_revision = "0035_crons"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "sandbox_tools" not in tables:
        op.create_table(
            "sandbox_tools",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=60), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "input_schema_json",
                sa.Text(),
                nullable=False,
                server_default='{"type":"object","properties":{}}',
            ),
            sa.Column("argv_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column(
                "egress_hosts_json", sa.Text(), nullable=False, server_default="[]"
            ),
            sa.Column(
                "approval", sa.String(length=16), nullable=False, server_default="inherit"
            ),
            sa.Column(
                "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column(
                "created_by", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "name"),
        )
        op.create_index(
            "ix_sandbox_tools_workspace_id", "sandbox_tools", ["workspace_id"]
        )


def downgrade() -> None:
    op.drop_table("sandbox_tools")
