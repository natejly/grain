"""Per-member memory opt-out, and per-conversation incognito.

Two boolean preferences, both the `safe_mode` shape. `memberships.
memory_enabled` is whether the assistant remembers things from THIS member's
runs — recall injection and post-run extraction both consult it, the explicit
remember/forget tools deliberately do not. `conversations.incognito` is a
temporary chat: its runs neither recall nor store memories (the rolling
summary included), set at creation only.

No backfill: the server defaults are the correct historical truth — everyone
had memory on, and no thread was incognito.

The downgrade drops both columns.

Revision ID: 0071_memory_prefs
Revises: 0070_space_default_agent
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0071_memory_prefs"
down_revision = "0070_space_default_agent"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    # Table-existence first (the 0042 add-column template): the replay test
    # runs this chain against a deliberately partial legacy database, where a
    # missing table must be a skip rather than a raise from the inspector —
    # and a database built by create_all already holds the columns.
    if inspector.has_table("memberships") and "memory_enabled" not in _columns(
        "memberships"
    ):
        op.add_column(
            "memberships",
            sa.Column(
                "memory_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
        )
    if inspector.has_table("conversations") and "incognito" not in _columns(
        "conversations"
    ):
        op.add_column(
            "conversations",
            sa.Column(
                "incognito",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("memberships") and "memory_enabled" in _columns(
        "memberships"
    ):
        op.drop_column("memberships", "memory_enabled")
    if inspector.has_table("conversations") and "incognito" in _columns(
        "conversations"
    ):
        op.drop_column("conversations", "incognito")
