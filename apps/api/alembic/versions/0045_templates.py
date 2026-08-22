"""Space templates and workflow templates: reusable starting points.

Two new tables, both snapshots rather than links. `space_templates` copies a
space's instructions (plus the agent ids the playbook expects, as plain
historical ids) so "set up a client space the way we always do" is one click;
`workflow_templates` copies a workflow's `graph_json` and `source_prompt` —
the whole definition — while deliberately leaving every schedule field behind,
so instantiating a template always produces a *draft* workflow with an empty
cron that a person must review and activate. Nothing existing changes shape,
so there is no backfill: an empty table simply means "no templates yet".

Both tables copy `dashboard_templates`' shape (0029): UNIQUE(workspace_id,
name) as the race arbiter for saves, and a plain workspace_id index for the
list scan.

Revision ID: 0045_templates
Revises: 0044_conversation_index
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0045_templates"
down_revision = "0044_conversation_index"
branch_labels = None
depends_on = None

_INDEXES = {
    "space_templates": (("ix_space_templates_workspace_id", ["workspace_id"]),),
    "workflow_templates": (("ix_workflow_templates_workspace_id", ["workspace_id"]),),
}


def upgrade() -> None:
    # Guarded like 0024-0044: a database migrated from empty already got these
    # tables from `create_all`, and creating them again would fail. has_table
    # is checked per table BEFORE any inspection of it, because the replay test
    # runs this chain against a deliberately partial legacy database.
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "space_templates" not in tables:
        op.create_table(
            "space_templates",
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
            sa.Column(
                "description", sa.String(length=500), nullable=False, server_default=""
            ),
            sa.Column("instructions", sa.Text(), nullable=False, server_default=""),
            sa.Column("agent_ids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "name"),
        )
        for name, cols in _INDEXES["space_templates"]:
            op.create_index(name, "space_templates", cols)

    if "workflow_templates" not in tables:
        op.create_table(
            "workflow_templates",
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
            sa.Column(
                "description", sa.String(length=500), nullable=False, server_default=""
            ),
            sa.Column("graph_json", sa.Text(), nullable=False),
            sa.Column("source_prompt", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "name"),
        )
        for name, cols in _INDEXES["workflow_templates"]:
            op.create_index(name, "workflow_templates", cols)


def downgrade() -> None:
    # Snapshots of things that still exist: dropping a template loses a saved
    # starting point, never live data. Guarded symmetrically with upgrade.
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table in ("workflow_templates", "space_templates"):
        if table not in tables:
            continue
        live = {index["name"] for index in inspector.get_indexes(table)}
        for name, _cols in _INDEXES[table]:
            if name in live:
                op.drop_index(name, table_name=table)
        op.drop_table(table)
