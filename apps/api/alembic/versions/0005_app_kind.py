"""Add app_type to generated apps for sandboxed code apps.

Revision ID: 0005_app_kind
Revises: 0004_integrations
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_app_kind"
down_revision = "0004_integrations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("generated_apps")}
    if "app_type" not in columns:
        op.add_column(
            "generated_apps",
            sa.Column(
                "app_type", sa.String(20), nullable=False, server_default="dashboard"
            ),
        )


def downgrade() -> None:
    op.drop_column("generated_apps", "app_type")
