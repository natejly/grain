"""A run remembers the model and effort the sender chose for that one turn.

`runs` gains `requested_model` and `requested_effort`, the per-turn overrides
`send_message` persists so `process_run` — which re-opens a fresh session and
reads the run off the row, the HTTP request being gone — can hand them to the
model step. Both are empty-string-for-unset, like `paused_reason`: "" resolves
to the deployment defaults, so a run written before this feature is byte-for-byte
today's behaviour.

Revision ID: 0032_run_model_overrides
Revises: 0031_agent_profiles
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0033_run_model_overrides"
down_revision = "0032_approval_modes_and_todo_items"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Idempotent, matching 0024–0031: these databases have been through
    # `create_all` as well as alembic, so the columns can already be present.
    if "requested_model" not in _columns("runs"):
        op.add_column(
            "runs",
            sa.Column("requested_model", sa.String(80), nullable=False, server_default=""),
        )
    if "requested_effort" not in _columns("runs"):
        op.add_column(
            "runs",
            sa.Column("requested_effort", sa.String(16), nullable=False, server_default=""),
        )


def downgrade() -> None:
    op.drop_column("runs", "requested_effort")
    op.drop_column("runs", "requested_model")
