"""Add long-term memory items and graph memory provenance.

Revision ID: 0003_memory
Revises: 0002_workspace_expansion
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from app import models  # noqa: F401
from app.database import Base

revision = "0003_memory"
down_revision = "0002_workspace_expansion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.tables["memory_items"].create(bind=bind, checkfirst=True)
    inspector = sa.inspect(bind)
    for table in ("graph_entities", "graph_edges"):
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "memory_ids_json" not in columns:
            op.add_column(
                table,
                sa.Column("memory_ids_json", sa.Text(), nullable=False, server_default="[]"),
            )


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("graph_entities", "graph_edges"):
        op.drop_column(table, "memory_ids_json")
    Base.metadata.tables["memory_items"].drop(bind=bind, checkfirst=True)
