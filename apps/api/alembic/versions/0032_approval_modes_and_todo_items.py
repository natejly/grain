"""Per-conversation approval mode, and the tick on a board card.

Two columns, one migration, because both are the same shape of change: a small
amount of state added to a table that already exists, so that a feature can be
built as a *view* over what is there rather than as a second entity beside it.

`conversations.approval_mode` is NOT NULL with a server default of the mode every
existing thread has been running under, so no row changes meaning: a conversation
written before this column existed asked before writes, and after it, still does.
Per conversation rather than per workspace on purpose — a bypass switched on to
get through one refactor must not be governing next week's chats.

`board_cards.done_at` is nullable, which is the one place in this schema where
NULL is the right spelling for "unset": nothing groups or filters by it the way
`documents.folder_id` is grouped by (see 0030), and the column's whole value is
the instant it holds. It sits on every card, not on a separate todo-item table,
because a todo list here is a one-column board rendered as checkboxes — which is
what lets a checked item graduate into a kanban card with no migration at all.

Revision ID: 0032_approval_modes_and_todo_items
Revises: 0031_agent_profiles
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0032_approval_modes_and_todo_items"
down_revision = "0031_agent_profiles"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Idempotent, matching 0024–0031: these databases have been through
    # `create_all` as well as alembic, so the columns can already be present.
    if "approval_mode" not in _columns("conversations"):
        op.add_column(
            "conversations",
            sa.Column(
                "approval_mode",
                sa.String(24),
                nullable=False,
                server_default="ask_writes",
            ),
        )
    if "done_at" not in _columns("board_cards"):
        op.add_column("board_cards", sa.Column("done_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("board_cards", "done_at")
    op.drop_column("conversations", "approval_mode")
