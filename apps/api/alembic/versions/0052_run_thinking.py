"""Runs remember whether their turn shows a thinking trail.

One boolean on `runs`: the composer's "Thinking" toggle, persisted per turn
beside `requested_model`/`requested_effort` for the same reason they are — a
run parked for an approval and resumed in another process must keep streaming
(or keep not streaming) the reasoning summaries the user asked for. The trail
itself rides `run_events` as `thinking.delta` rows, which need no schema.

Revision ID: 0052_run_thinking
Revises: 0051_listing_installs
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0052_run_thinking"
down_revision = "0051_listing_installs"
branch_labels = None
depends_on = None


def _run_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    # A database that reaches this revision without a `runs` table gets the
    # column from `create_all` — inspecting a missing table would raise.
    if not inspector.has_table("runs"):
        return set()
    return {column["name"] for column in inspector.get_columns("runs")}


def upgrade() -> None:
    # Idempotent, matching 0024-0046: these databases have been through
    # `create_all` as well as alembic, so the column can already be present.
    columns = _run_columns()
    if columns and "show_thinking" not in columns:
        op.add_column(
            "runs",
            sa.Column(
                "show_thinking",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    if "show_thinking" in _run_columns():
        op.drop_column("runs", "show_thinking")
