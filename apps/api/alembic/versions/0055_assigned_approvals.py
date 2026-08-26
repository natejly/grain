"""A parked approval can be routed to one member: `assigned_to`.

`agent_tool_calls` gains a single column. It is routing, not decision — the
compare-and-set claim on `status='proposed'`, `decided_by`'s user-or-mode
semantics and the resume machinery are untouched; the decision endpoint merely
refuses (409) a reviewer the row does not name. '' means "anyone may answer",
which is also what every pre-existing row means, so a database migrated from
before this feature behaves byte-for-byte as it did.

Revision ID: 0055_assigned_approvals
Revises: 0054_comments_notifications
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0055_assigned_approvals"
down_revision = "0054_comments_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Guarded like 0024-0046: a database migrated from empty got the column
    # from `create_all` already. has_table BEFORE get_columns, because the
    # replay test runs this chain against a deliberately partial legacy
    # database (0001's create_all backfills the table, but the order of checks
    # is the convention that keeps every replay branch safe).
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("agent_tool_calls"):
        return
    columns = {column["name"] for column in inspector.get_columns("agent_tool_calls")}
    if "assigned_to" not in columns:
        op.add_column(
            "agent_tool_calls",
            sa.Column(
                "assigned_to", sa.String(36), nullable=False, server_default=""
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("agent_tool_calls"):
        return
    columns = {column["name"] for column in inspector.get_columns("agent_tool_calls")}
    if "assigned_to" in columns:
        op.drop_column("agent_tool_calls", "assigned_to")
