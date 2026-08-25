"""Dashboard subscriptions: scheduled snapshot delivery by mail.

One new table on the Cron's shape: a 5-field `schedule_cron` + IANA zone pair
and a `last_dispatched_at` claim column advanced by the shared tick's
conditional UPDATE. `dashboard_id` and `recipient_user_id` are plain columns,
never ForeignKeys, per the house convention for references that outlive their
target — a purged dashboard or a departed member turns the fire into a
skip-with-audit rather than an error (or a mail to someone who left).

Revision ID: 0052_dashboard_subscriptions
Revises: 0051_share_links
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0052_dashboard_subscriptions"
down_revision = "0051_share_links"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_dashboard_subscriptions_workspace_id", ["workspace_id"], False),
    (
        "ix_dashboard_subscriptions_workspace_enabled",
        ["workspace_id", "enabled"],
        False,
    ),
)


def upgrade() -> None:
    # Guarded like 0024-0051: a database migrated from empty already got this
    # table from `create_all`, and creating it again would fail. has_table is
    # checked BEFORE any inspection of the table, because the replay test runs
    # this chain against a deliberately partial legacy database.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "dashboard_subscriptions" in tables:
        return
    op.create_table(
        "dashboard_subscriptions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(length=36),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("dashboard_id", sa.String(length=36), nullable=False),
        sa.Column("recipient_user_id", sa.String(length=36), nullable=False),
        sa.Column("schedule_cron", sa.String(length=120), nullable=False),
        sa.Column("schedule_timezone", sa.String(length=64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_dispatched_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_by",
            sa.String(length=36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for name, cols, unique in _INDEXES:
        op.create_index(name, "dashboard_subscriptions", cols, unique=unique)


def downgrade() -> None:
    # Dropping the table stops every scheduled mail at once — fail-closed, the
    # only acceptable direction for unattended email. Guarded symmetrically.
    inspector = sa.inspect(op.get_bind())
    if "dashboard_subscriptions" not in inspector.get_table_names():
        return
    live = {index["name"] for index in inspector.get_indexes("dashboard_subscriptions")}
    for name, _cols, _unique in _INDEXES:
        if name in live:
            op.drop_index(name, table_name="dashboard_subscriptions")
    op.drop_table("dashboard_subscriptions")
