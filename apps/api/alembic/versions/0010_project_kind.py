"""Distinguish web projects from LaTeX projects.

Both are the same virtual filesystem; only the preview differs (esbuild-wasm
bundling versus TeX compilation), so this is a discriminator rather than a new
table.

Revision ID: 0010_project_kind
Revises: 0009_db_and_projects
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010_project_kind"
down_revision = "0009_db_and_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("projects")
    }
    if "kind" not in columns:
        op.add_column(
            "projects",
            sa.Column("kind", sa.String(16), nullable=False, server_default="web"),
        )


def downgrade() -> None:
    op.drop_column("projects", "kind")
