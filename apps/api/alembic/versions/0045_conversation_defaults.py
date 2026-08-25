"""Per-thread composer defaults: agent, model, effort.

Three columns on `conversations`, all "" ("use the deployment's default"),
mirroring `Run.requested_model`'s convention. They are seeds for the composer's
pickers when a thread reopens — the run path never reads them, so nothing about
how a turn resolves its model changes shape here (see the column comments in
`models.Conversation`).

Revision ID: 0045_conversation_defaults
Revises: 0044_conversation_index
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0045_conversation_defaults"
down_revision = "0044_conversation_index"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("default_agent_id", sa.String(36)),
    ("default_model", sa.String(120)),
    ("default_effort", sa.String(24)),
)


def _existing() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns("conversations")}


def upgrade() -> None:
    # Guarded like 0024-0044: a database migrated from empty already has these
    # columns from `create_all`, and adding them again would fail.
    present = _existing()
    for name, type_ in _COLUMNS:
        if name not in present:
            op.add_column(
                "conversations",
                sa.Column(name, type_, nullable=False, server_default=""),
            )


def downgrade() -> None:
    # Guarded the same way DOWN as up: a database built clean from empty holds
    # every column via `create_all` at 0001, so this drop must only remove what
    # is actually there (the 0044 lesson, applied in both directions).
    present = _existing()
    for name, _ in _COLUMNS:
        if name in present:
            op.drop_column("conversations", name)
