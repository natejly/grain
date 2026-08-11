"""Two things the server already produced and the app could not show.

`agent_tool_calls.artifacts_json` — a sandbox execution saves every figure it
draws as a workspace Source and hands back a descriptor for each. Until now the
only trace of that on the tool call was `result_preview`, a 500-character clip of
prose written for the model, whose artifact list is the part that gets clipped
away first. A column means the chat card can render the chart the run drew.

`messages.citation_report_json` — the citation validator's verdict lived only in
a `run.citations` event and an audit row. Both are write-only from the reader's
point of view: nothing in the product ever showed whether the [n] markers in an
answer matched the passages the model was handed. Stored on the message it is
about, so a fabricated citation is still flagged after a reload.

`sandbox_executions.artifacts_json` — the same fact for the console. The row
already counted its artifacts and could not name one, so reopening the sandbox
panel showed "3 artifacts" and three blank spaces where the charts had been.

Both artifact columns default to "[]", meaning "produced no files".
`citation_report_json` defaults to "", meaning "this answer was never checked",
which is not the same as "checked and clean" and must never render as one — a
denial and a budget park both complete a message without a verdict.

Revision ID: 0025_reachable_outputs
Revises: 0024_sandbox_slot_index
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0025_reachable_outputs"
down_revision = "0024_sandbox_slot_index"
branch_labels = None
depends_on = None


def _add(table: str, column: sa.Column) -> None:
    """Idempotent add, matching the house style of 0024: these databases have
    been through `create_all` as well as alembic, so a column can already be
    there and the migration still has to run to completion."""
    inspector = sa.inspect(op.get_bind())
    if column.name not in {existing["name"] for existing in inspector.get_columns(table)}:
        op.add_column(table, column)


def upgrade() -> None:
    _add(
        "agent_tool_calls",
        sa.Column(
            "artifacts_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    _add(
        "sandbox_executions",
        sa.Column(
            "artifacts_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    _add(
        "messages",
        sa.Column(
            "citation_report_json",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("messages", "citation_report_json")
    op.drop_column("sandbox_executions", "artifacts_json")
    op.drop_column("agent_tool_calls", "artifacts_json")
