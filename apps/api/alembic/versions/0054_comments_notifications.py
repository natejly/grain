"""Comments on things, and the generic notifications store behind @mentions.

Two new tables. `comments` hangs a remark on the established polymorphic pair
(`subject_kind` + `subject_id`) — thread, document or dashboard — soft-deleted
via `deleted_at` so a discussion keeps its shape when one remark is withdrawn.
`notifications` is deliberately wider than mentions: a generic "look at this"
row (`kind`, `target_user_id` with '' meaning every member, a fan of '' -unset
deep-link ids) whose `status='open'` set is the Inbox's unbounded waiting set,
so later features (monitor alerts, spend anomalies) add a `kind`, not a table.

Both composite indexes follow 0043's rationale: the unbounded scans each table
exists to serve get an index shaped exactly like the query. No backfill — an
empty table simply means "nothing said, nobody notified yet".

Revision ID: 0054_comments_notifications
Revises: 0053_templates
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0054_comments_notifications"
down_revision = "0053_templates"
branch_labels = None
depends_on = None

_INDEXES = {
    "comments": (
        ("ix_comments_workspace_id", ["workspace_id"]),
        (
            "ix_comments_workspace_subject_created",
            ["workspace_id", "subject_kind", "subject_id", "created_at"],
        ),
    ),
    "notifications": (
        ("ix_notifications_workspace_id", ["workspace_id"]),
        (
            "ix_notifications_workspace_status_created",
            ["workspace_id", "status", "created_at"],
        ),
    ),
}


def upgrade() -> None:
    # Guarded like 0024-0045: a database migrated from empty already got these
    # tables from `create_all`, and creating them again would fail. has_table
    # is checked per table BEFORE any inspection of it, because the replay test
    # runs this chain against a deliberately partial legacy database.
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "comments" not in tables:
        op.create_table(
            "comments",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("subject_kind", sa.String(length=16), nullable=False),
            sa.Column("subject_id", sa.String(length=36), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column(
                "mentions_json", sa.Text(), nullable=False, server_default="[]"
            ),
            sa.Column(
                "created_by",
                sa.String(length=36),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
        )
        for name, cols in _INDEXES["comments"]:
            op.create_index(name, "comments", cols)

    if "notifications" not in tables:
        op.create_table(
            "notifications",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "target_user_id",
                sa.String(length=36),
                nullable=False,
                server_default="",
            ),
            sa.Column("kind", sa.String(length=32), nullable=False),
            sa.Column(
                "status", sa.String(length=16), nullable=False, server_default="open"
            ),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "conversation_id",
                sa.String(length=36),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "document_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "dashboard_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "comment_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "monitor_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "agent_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "created_by", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.Column(
                "resolved_by", sa.String(length=36), nullable=False, server_default=""
            ),
        )
        for name, cols in _INDEXES["notifications"]:
            op.create_index(name, "notifications", cols)


def downgrade() -> None:
    # Remarks and reminders about things that still exist: dropping them loses
    # commentary, never the commented-on data. Guarded symmetrically.
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for table in ("notifications", "comments"):
        if table not in tables:
            continue
        live = {index["name"] for index in inspector.get_indexes(table)}
        for name, _cols in _INDEXES[table]:
            if name in live:
                op.drop_index(name, table_name=table)
        op.drop_table(table)
