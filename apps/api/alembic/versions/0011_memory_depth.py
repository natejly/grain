"""Order the memory recall candidate window without sorting the workspace.

recall() now caps the vector scan with `WHERE workspace_id = ? AND status = ?
ORDER BY updated_at DESC LIMIT ?`. The existing (workspace_id, status) index
finds those rows but cannot order them, so above ~20k memories in one workspace
the planner sorts the whole workspace to serve a 5000-row window. Adding
updated_at as a third column makes the LIMIT a prefix read.

No column changes: embeddings stay packed float32 in a LargeBinary, scored in
numpy at query time. There is no vector-typed column and no shadow table.

Revision ID: 0011_memory_depth
Revises: 0010_project_kind
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011_memory_depth"
down_revision = "0010_project_kind"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_memory_items_ws_status_updated"


def upgrade() -> None:
    indexes = {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("memory_items")
    }
    if INDEX_NAME not in indexes:
        op.create_index(
            INDEX_NAME,
            "memory_items",
            ["workspace_id", "status", "updated_at"],
        )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="memory_items")
