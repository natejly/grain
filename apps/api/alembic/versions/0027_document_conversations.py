"""A conversation can belong to a document.

The document editor grows a chat panel beside the text, and the thread in that
panel is not a general chat that happens to be open in the wrong place: the turn
is handed the document's contents, the agent's `edit_document` defaults to it,
and the thread is created and deleted with the document. All of that needs the
association to be a fact about the conversation rather than something the
browser remembers, so it survives a reload and so the server can act on it.

Empty means an ordinary chat, which is every existing row. `GET /api/conversations`
filters scoped threads out of the Chat rail — a list that fills with one entry
per document opened is not a list of conversations any more — so this column is
also what keeps that surface honest.

Indexed because the get-or-create for a document reads by it on every open.

Revision ID: 0027_document_conversations
Revises: 0026_document_kinds
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0027_document_conversations"
down_revision = "0026_document_kinds"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Idempotent, matching 0024/0025: these databases have been through
    # `create_all` as well as alembic, so the column can already be present.
    if "document_id" not in _columns("conversations"):
        op.add_column(
            "conversations",
            sa.Column("document_id", sa.String(36), nullable=False, server_default=""),
        )
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("conversations")}
    if "ix_conversations_document_id" not in indexes:
        op.create_index(
            "ix_conversations_document_id", "conversations", ["document_id"]
        )


def downgrade() -> None:
    op.drop_index("ix_conversations_document_id", table_name="conversations")
    op.drop_column("conversations", "document_id")
