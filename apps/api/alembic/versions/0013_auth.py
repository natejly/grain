"""Real user authentication: credentials, federated identities, sessions.

The app previously trusted an X-User-Id header with a default, so any client
could claim any identity. Multi-tenant makes that a data-leak rather than a
convenience, and these tables are what replace it.

Revision ID: 0013_auth
Revises: 0012_graph_relations
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013_auth"
down_revision = "0012_graph_relations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    columns = {column["name"] for column in inspector.get_columns("users")}
    additions = [
        ("password_hash", sa.Column("password_hash", sa.String(255), nullable=True)),
        ("email_verified_at", sa.Column("email_verified_at", sa.DateTime(), nullable=True)),
        (
            "status",
            sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        ),
        (
            "failed_logins",
            sa.Column("failed_logins", sa.Integer(), nullable=False, server_default="0"),
        ),
        ("locked_until", sa.Column("locked_until", sa.DateTime(), nullable=True)),
        ("updated_at", sa.Column("updated_at", sa.DateTime(), nullable=True)),
    ]
    for name, column in additions:
        if name not in columns:
            op.add_column("users", column)

    if "user_identities" not in tables:
        op.create_table(
            "user_identities",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("provider", sa.String(32), nullable=False),
            sa.Column("subject", sa.String(255), nullable=False),
            sa.Column("email", sa.String(320), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("provider", "subject"),
        )
        op.create_index("ix_user_identities_user_id", "user_identities", ["user_id"])

    if "user_sessions" not in tables:
        op.create_table(
            "user_sessions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("csrf_secret", sa.String(64), nullable=False, server_default=""),
            sa.Column("user_agent", sa.String(400), nullable=False, server_default=""),
            sa.Column("ip_address", sa.String(64), nullable=False, server_default=""),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
        op.create_index(
            "ix_user_sessions_token_hash", "user_sessions", ["token_hash"], unique=True
        )
        op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])

    if "email_tokens" not in tables:
        op.create_table(
            "email_tokens",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("purpose", sa.String(24), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("consumed_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_email_tokens_user_id", "email_tokens", ["user_id"])
        op.create_index(
            "ix_email_tokens_token_hash", "email_tokens", ["token_hash"], unique=True
        )

    if "workspace_invites" not in tables:
        op.create_table(
            "workspace_invites",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False
            ),
            sa.Column("email", sa.String(320), nullable=False),
            sa.Column("role", sa.String(24), nullable=False, server_default="member"),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("invited_by", sa.String(36), nullable=False, server_default=""),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("accepted_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_workspace_invites_workspace_id", "workspace_invites", ["workspace_id"]
        )
        op.create_index("ix_workspace_invites_email", "workspace_invites", ["email"])
        op.create_index(
            "ix_workspace_invites_token_hash",
            "workspace_invites",
            ["token_hash"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_table("workspace_invites")
    op.drop_table("email_tokens")
    op.drop_table("user_sessions")
    op.drop_table("user_identities")
    for name in (
        "updated_at",
        "locked_until",
        "failed_logins",
        "status",
        "email_verified_at",
        "password_hash",
    ):
        op.drop_column("users", name)
