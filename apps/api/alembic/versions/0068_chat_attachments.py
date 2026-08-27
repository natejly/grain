"""Files brought into a chat: attachments, and the scope that keeps them there.

Two shapes for one feature. `chat_attachments` is the polymorphic link from a
conversation to a file it is about — `kind`/`target_id` pointing at a Document
(text, editable) or a Source (bytes, quotable), the same shape and for the same
reason as `conversations.subject_kind`/`subject_id`. `sources.conversation_id`
is the scope that makes an attached file *this thread's* file: "" is the
workspace library and behaves exactly as today, and a non-empty value means the
file reaches its own conversation's retrieval and no other.

The scope column is the load-bearing half. Without it, uploading a contract to
ask one question about clause 4 would permanently change what every other thread
in the workspace retrieves for "clause" — the library is what the workspace
knows, and a file attached to a question was never a claim about that. It is
deliberately the same predicate shape as `space_id` so that
`retrieval._live_sources` filters both in one tuple; a scope enforced by some
ranking arms and not others is a bypass with extra steps.

No backfill, and none is possible or wanted: every existing source predates
attachments and belongs to the library, which is exactly what the "" server
default says about it.

The downgrade drops both. Attachment rows are lost, which is honest — there is
nowhere in the old schema to put "this conversation is about that file" — and
the files themselves survive, because a Document and a Source outlive the row
that pointed at them. Sources that had been scoped to a conversation return to
the library rather than vanishing: on a schema with no scope column, visible is
the only thing "" can mean.

Revision ID: 0068_chat_attachments
Revises: 0067_safe_mode
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0068_chat_attachments"
down_revision = "0067_safe_mode"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # Table-existence first (the 0042 add-column template): the replay test runs
    # this chain against a deliberately partial legacy database, where a missing
    # table must be a skip rather than a raise from the column inspector.
    if inspector.has_table("sources") and "conversation_id" not in _columns("sources"):
        op.add_column(
            "sources",
            sa.Column(
                "conversation_id",
                sa.String(36),
                nullable=False,
                server_default="",
            ),
        )
        op.create_index("ix_sources_conversation_id", "sources", ["conversation_id"])

    if not inspector.has_table("chat_attachments"):
        op.create_table(
            "chat_attachments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                index=True,
            ),
            sa.Column(
                "conversation_id",
                sa.String(36),
                sa.ForeignKey("conversations.id"),
                index=True,
            ),
            sa.Column("message_id", sa.String(36), nullable=False, server_default=""),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("target_id", sa.String(36), nullable=False, index=True),
            sa.Column("filename", sa.String(255), nullable=False),
            sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_chat_attachments_conversation",
            "chat_attachments",
            ["conversation_id", "created_at"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("chat_attachments"):
        op.drop_index("ix_chat_attachments_conversation", table_name="chat_attachments")
        op.drop_table("chat_attachments")
    if inspector.has_table("sources") and "conversation_id" in _columns("sources"):
        op.drop_index("ix_sources_conversation_id", table_name="sources")
        op.drop_column("sources", "conversation_id")
