"""Database connections and multi-file code projects.

Revision ID: 0009_db_and_projects
Revises: 0008_artifacts
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009_db_and_projects"
down_revision = "0008_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "db_connections" not in tables:
        op.create_table(
            "db_connections",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("engine", sa.String(20), nullable=False, server_default="postgres"),
            sa.Column("dsn_encrypted", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "read_only", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("status", sa.String(24), nullable=False, server_default="unknown"),
            sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "name"),
        )
        op.create_index(
            "ix_db_connections_workspace_id", "db_connections", ["workspace_id"]
        )

    if "projects" not in tables:
        op.create_table(
            "projects",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "entry_path", sa.String(400), nullable=False, server_default="index.tsx"
            ),
            sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "name"),
        )
        op.create_index("ix_projects_workspace_id", "projects", ["workspace_id"])

    if "project_files" not in tables:
        op.create_table(
            "project_files",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "project_id",
                sa.String(36),
                sa.ForeignKey("projects.id"),
                nullable=False,
            ),
            sa.Column("path", sa.String(400), nullable=False),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("project_id", "path"),
        )
        op.create_index("ix_project_files_workspace_id", "project_files", ["workspace_id"])
        op.create_index("ix_project_files_project_id", "project_files", ["project_id"])


def downgrade() -> None:
    op.drop_table("project_files")
    op.drop_table("projects")
    op.drop_table("db_connections")
