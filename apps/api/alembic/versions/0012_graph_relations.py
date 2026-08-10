"""Score typed graph relations.

Edges used to be co-occurrence wearing a relation label, so every edge was worth
the same. Extracted relations carry the extractor's certainty and co-occurrence
carries a fixed low constant, which is what lets a walk rank a named relation
above two names that merely shared a paragraph.

Revision ID: 0012_graph_relations
Revises: 0011_memory_depth
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012_graph_relations"
down_revision = "0011_memory_depth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("graph_edges")
    }
    if "confidence" not in columns:
        op.add_column(
            "graph_edges",
            sa.Column(
                "confidence", sa.Float(), nullable=False, server_default="0.25"
            ),
        )


def downgrade() -> None:
    op.drop_column("graph_edges", "confidence")
