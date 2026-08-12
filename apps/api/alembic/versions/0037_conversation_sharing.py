"""Personal vs shared conversations, and per-message sender attribution.

`conversations` gains `shared`: personal (False, visible only to the creator
within the workspace) vs shared (True, visible to every member of the same
workspace). New threads default to personal, but every EXISTING conversation is
member-visible today — the list query has always filtered `workspace_id` only,
never `created_by` — so the backfill sets every existing row to `shared = 1`,
preserving exactly who can see what. `messages` gains `created_by`, the member a
message is attributed to, backfilled from the run that produced it so a shared
thread can show who said what for history predating the column.

The load-bearing property: `shared` only relaxes the *within-workspace* creator
filter. The `workspace_id` filter is never removed, so this can never expose a
thread cross-workspace.

Revision ID: 0037_conversation_sharing
Revises: 0036_sandbox_tools
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0037_conversation_sharing"
down_revision = "0036_sandbox_tools"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Idempotent, matching 0024–0036: these databases have been through
    # `create_all` as well as alembic, so the columns can already be present.
    if "shared" not in _columns("conversations"):
        op.add_column(
            "conversations",
            sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        # Every EXISTING conversation is member-visible today (the list query
        # filters workspace_id only), so preserve that exactly. New rows created
        # after this migration default to personal.
        op.execute("UPDATE conversations SET shared = 1")
    if "created_by" not in _columns("messages"):
        op.add_column(
            "messages",
            sa.Column("created_by", sa.String(length=36), nullable=False, server_default=""),
        )
        # Attribute historical messages to the member whose run produced them.
        op.execute(
            "UPDATE messages SET created_by = "
            "(SELECT created_by FROM runs WHERE runs.id = messages.run_id) "
            "WHERE created_by = '' AND run_id <> ''"
        )


def downgrade() -> None:
    op.drop_column("messages", "created_by")
    op.drop_column("conversations", "shared")
