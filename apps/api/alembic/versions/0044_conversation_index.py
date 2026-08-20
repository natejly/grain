"""Conversation index: make past conversations searchable and quotable.

One new table, `conversation_chunks`: transcript windows plus one rolling
summary row per thread, each carrying provenance (`message_ids_json`,
`last_message_at`) and an optional embedding. Nothing existing changes shape —
`messages` stays the untouched record and the chunks are derived from it, so
this migration has no backfill to argue about: an empty table simply means "not
indexed yet", and both the post-run hook and the search-time reconcile fill it
as conversations are touched (scripts/backfill_conversation_index.py does the
rest in bulk).

Visibility is deliberately NOT a column here. A chunk is exactly as visible as
its conversation, and the Conversation row already holds that answer
(`created_by`, `shared`, `subject_id`); every search joins it. Copying any of
those onto the chunk would be a second visibility rule that could disagree with
the first — the class of bug 0040 existed to close.

Revision ID: 0044_conversation_index
Revises: 0043_inbox_call_index
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0044_conversation_index"
down_revision = "0043_inbox_call_index"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_conversation_chunks_workspace_id", ["workspace_id"]),
    ("ix_conversation_chunks_conversation_id", ["conversation_id"]),
    ("ix_conversation_chunks_ws_conversation", ["workspace_id", "conversation_id"]),
)


def upgrade() -> None:
    # Guarded like 0024-0042: a database migrated from empty already got this
    # table from `create_all`, and creating it again would fail.
    if sa.inspect(op.get_bind()).has_table("conversation_chunks"):
        return
    op.create_table(
        "conversation_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False, server_default="chunk"),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("message_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_message_at", sa.DateTime(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("embedding_model", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    for name, cols in _INDEXES:
        op.create_index(name, "conversation_chunks", cols)


def downgrade() -> None:
    # Derived data: dropping it loses nothing a reindex cannot rebuild.
    if not sa.inspect(op.get_bind()).has_table("conversation_chunks"):
        return
    live = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("conversation_chunks")
    }
    for name, _cols in _INDEXES:
        if name in live:
            op.drop_index(name, table_name="conversation_chunks")
    op.drop_table("conversation_chunks")
