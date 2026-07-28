"""Add graph, analytics, dashboard, and generated-app projections.

Revision ID: 0002_workspace_expansion
Revises: 0001_initial
"""
from __future__ import annotations

from alembic import op
from app import models  # noqa: F401
from app.database import Base

revision = "0002_workspace_expansion"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

TABLES = [
    "graph_projections",
    "graph_entities",
    "graph_edges",
    "datasets",
    "dataset_versions",
    "dashboards",
    "generated_apps",
    "app_releases",
]


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in TABLES:
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in reversed(TABLES):
        Base.metadata.tables[table_name].drop(bind=bind, checkfirst=True)
