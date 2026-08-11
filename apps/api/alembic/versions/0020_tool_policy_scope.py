"""Scope a tool policy to the situation it was granted in, and give the ticker a claim.

Two changes, both in service of the residual risk ADR 0007 named as sharpest:
`tool_policies` was workspace-wide, so one click of "always allow" in a chat
authorised every future *unattended* workflow run to use that tool forever.

`tool_policies.scope` splits the grant in two — `chat` and `workflow` — and the
unique key becomes (workspace_id, tool_name, scope) so one tool can carry a
different verdict in each. **Every existing row becomes `chat`**, which is
precisely what it meant when it was written: a person clicked it while typing.
Nothing is widened and nothing is narrowed; what changes is that the grant no
longer reaches a situation nobody was asked about.

The unique constraint on the old table was declared inline and therefore has no
portable name to drop (SQLite has none at all), so the table is rebuilt rather
than altered: rename, create, copy, drop. That is portable across SQLite and
Postgres and it preserves every id, which matters because audit rows point at
tool names, not policy ids, but a foreign key could appear later.

`workflows.last_dispatched_at` is the schedule ticker's claim. It stores the
minute a workflow was last dispatched for, and the ticker advances it with a
conditional UPDATE — so two ticks in the same minute produce one run, not two.

Revision ID: 0020_tool_policy_scope
Revises: 0019_workflows
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0020_tool_policy_scope"
down_revision = "0019_workflows"
branch_labels = None
depends_on = None

_COLUMNS = "id, workspace_id, tool_name, policy, created_by, created_at, updated_at"


def _create_policies(*, scoped: bool) -> None:
    columns: list[sa.schema.SchemaItem] = [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(120), nullable=False),
        sa.Column("policy", sa.String(16), nullable=False, server_default="ask"),
        sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]
    if scoped:
        columns.insert(
            4, sa.Column("scope", sa.String(16), nullable=False, server_default="chat")
        )
        columns.append(sa.UniqueConstraint("workspace_id", "tool_name", "scope"))
    else:
        columns.append(sa.UniqueConstraint("workspace_id", "tool_name"))
    op.create_table("tool_policies", *columns)
    op.create_index("ix_tool_policies_workspace_id", "tool_policies", ["workspace_id"])


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "tool_policies" in tables and "scope" not in {
        column["name"] for column in inspector.get_columns("tool_policies")
    }:
        # The index name is global in SQLite, so it has to go before the new
        # table claims it back.
        op.drop_index("ix_tool_policies_workspace_id", table_name="tool_policies")
        op.rename_table("tool_policies", "tool_policies_pre_scope")
        _create_policies(scoped=True)
        # 'chat' is stated here rather than left to the server default so the
        # backfill is visible in the migration instead of implied by the DDL.
        op.execute(
            sa.text(
                f"INSERT INTO tool_policies ({_COLUMNS}, scope) "
                f"SELECT {_COLUMNS}, 'chat' FROM tool_policies_pre_scope"
            )
        )
        op.drop_table("tool_policies_pre_scope")

    if "workflows" in tables and "last_dispatched_at" not in {
        column["name"] for column in inspector.get_columns("workflows")
    }:
        op.add_column(
            "workflows", sa.Column("last_dispatched_at", sa.DateTime(), nullable=True)
        )
        op.create_index(
            "ix_workflows_schedule",
            "workflows",
            ["status", "trigger_kind"],
        )


def downgrade() -> None:
    op.drop_index("ix_workflows_schedule", table_name="workflows")
    op.drop_column("workflows", "last_dispatched_at")

    # Collapsing two scopes into one key means choosing which grant survives.
    # The chat row wins: it is the one that existed before this migration, and
    # dropping a workflow grant fails closed while dropping a chat grant would
    # silently revoke something a person clicked.
    op.drop_index("ix_tool_policies_workspace_id", table_name="tool_policies")
    op.rename_table("tool_policies", "tool_policies_scoped")
    _create_policies(scoped=False)
    op.execute(
        sa.text(
            f"INSERT INTO tool_policies ({_COLUMNS}) "
            f"SELECT {_COLUMNS} FROM tool_policies_scoped WHERE scope = 'chat'"
        )
    )
    op.drop_table("tool_policies_scoped")
