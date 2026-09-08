"""A space carries the agent its new threads are born preferring.

One column: `spaces.default_agent_id`. It is a SEED, not a resolution layer —
`POST /api/conversations` copies it onto the new thread's
`conversation.default_agent_id`, and only the composer ever reads either
column. The run path still receives its agent explicitly per turn, so what
reached the provider stays answerable from the Run row alone; that contract
is the `Conversation` defaults' docstring, and this column deliberately
inherits it rather than becoming a fourth layer `resolve_directives` would
have to explain.

Not a ForeignKey, matching `conversations.default_agent_id`: an agent can be
retired after a space chose it, and the composer already self-heals a ghost
id to "" — a constraint here would instead make retiring the agent fail on a
row nobody is looking at.

No backfill: every existing space predates the choice, and "" already means
"the workspace default agent", which is exactly what those spaces have been
doing all along.

The downgrade drops the column. Spaces forget their preference and fall back
to the workspace default — the pre-upgrade behaviour, restated.

Revision ID: 0070_space_default_agent
Revises: 0069_embedding_generations
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0070_space_default_agent"
down_revision = "0069_embedding_generations"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    # Table-existence first (the 0042 add-column template): the replay test
    # runs this chain against a deliberately partial legacy database, where a
    # missing table must be a skip rather than a raise from the inspector —
    # and a database built by create_all already holds the column.
    if inspector.has_table("spaces") and "default_agent_id" not in _columns("spaces"):
        op.add_column(
            "spaces",
            sa.Column(
                "default_agent_id",
                sa.String(36),
                nullable=False,
                server_default="",
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    # Guarded on the way down as on the way up: a database migrated from empty
    # arrived at 0001 via create_all and holds the column regardless of which
    # revisions ran, but a genuinely partial legacy one may not.
    if inspector.has_table("spaces") and "default_agent_id" in _columns("spaces"):
        op.drop_column("spaces", "default_agent_id")
