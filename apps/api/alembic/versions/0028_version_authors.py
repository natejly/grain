"""Who a document version is owed to.

Version history is about to stop being a list of whole edits. A reviewer can now
accept some of the changes in a proposal and reject the rest, which means a row
saying "Agent edit" can describe a document holding one of the three things the
agent asked for. The summary carries the count — see
`documents.edit_document` — and this column carries the person, so the pair
answers what was applied and to whom it is owed.

Empty means unattributed: every row written before this column existed, and any
future write with no human behind it.

Revision ID: 0028_version_authors
Revises: 0027_document_conversations
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0028_version_authors"
down_revision = "0027_document_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("document_versions")}
    if "created_by" not in existing:
        op.add_column(
            "document_versions",
            sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
        )


def downgrade() -> None:
    op.drop_column("document_versions", "created_by")
