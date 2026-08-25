"""Attribute the usage ledger to the agent whose turn spent it.

`runs.agent_id` has always existed, so per-agent cost was recoverable only by
joining every ledger row back through its run — and embeddings and other
run-less rows fell out of the join entirely. Denormalizing the agent onto
`model_usage` (plain string, "" = no agent, exactly the run_id convention)
makes the per-agent scorecard a single GROUP BY, and the paired index gives it
the same shape the workspace/run index gives the runaway-loop question.

Revision ID: 0046_model_usage_agent
Revises: 0045_conversation_defaults
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0046_model_usage_agent"
down_revision = "0045_conversation_defaults"
branch_labels = None
depends_on = None

_TABLE = "model_usage"
_COLUMN = "agent_id"
_INDEX = "ix_model_usage_workspace_agent"


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _columns() -> set[str]:
    inspector = _inspector()
    # Table guard before get_columns: the migration-replay tests run this chain
    # against deliberately partial databases, and the inspector raises on a
    # table that does not exist yet.
    if not inspector.has_table(_TABLE):
        return set()
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def _indexes() -> set[str]:
    inspector = _inspector()
    if not inspector.has_table(_TABLE):
        return set()
    return {index["name"] for index in inspector.get_indexes(_TABLE)}


def upgrade() -> None:
    # Absent table first: the replay tests build partial databases that stop
    # before `model_usage` exists, and on those `_columns()` answers "none" —
    # which would send the add_column below at a table that is not there.
    if not _inspector().has_table(_TABLE):
        return
    # Guarded: a database migrated from empty already has this column from
    # `create_all`, and adding it again would fail.
    if _COLUMN not in _columns():
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.String(36), nullable=False, server_default=""),
        )
    if _COLUMN in _columns() and _INDEX not in _indexes():
        op.create_index(_INDEX, _TABLE, ["workspace_id", _COLUMN])


def downgrade() -> None:
    # Guarded the same way down as up (the 0044 lesson, both directions):
    # drop only what is actually there.
    if _INDEX in _indexes():
        op.drop_index(_INDEX, table_name=_TABLE)
    if _COLUMN in _columns():
        op.drop_column(_TABLE, _COLUMN)
