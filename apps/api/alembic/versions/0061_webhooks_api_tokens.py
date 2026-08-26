"""Webhooks (outbound event push) and API tokens (inbound bearer auth).

Three tables. `api_tokens` is a deliberate twin of the base branch's
0048_api_tokens (commit ed7195b ships the same table for its `POST /api/mcp`
surface): same columns, same types, same name, so that after the branches
merge one upgrade path no-ops against a database the other already built —
the has_table guard below is what makes that replay safe in both orders.

`webhook_endpoints` holds owner-configured destination URLs with a
Fernet-encrypted signing secret; `webhook_deliveries` is the outbox the tick
sweeps — pending rows claimed by conditional UPDATE, sent with an HMAC
signature, failed after three attempts. `endpoint_id` is a plain column, not
a ForeignKey: the delivery trail outlives the endpoint it belonged to.

Revision ID: 0061_webhooks_api_tokens
Revises: 0060_dashboard_subscriptions
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0061_webhooks_api_tokens"
down_revision = "0060_dashboard_subscriptions"
branch_labels = None
depends_on = None

_DELIVERY_INDEXES = (
    ("ix_webhook_deliveries_workspace_id", ["workspace_id"], False),
    (
        "ix_webhook_deliveries_workspace_status_created",
        ["workspace_id", "status", "created_at"],
        False,
    ),
)


def upgrade() -> None:
    # Guarded like 0024-0052, and per-table rather than all-or-nothing: a
    # database built from empty has all three from `create_all`, and one that
    # ran the base branch's 0048_api_tokens already has `api_tokens` alone.
    # has_table is checked BEFORE any column/index inspection because the
    # replay test runs this chain against a deliberately partial legacy DB.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "api_tokens" not in tables:
        op.create_table(
            "api_tokens",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                index=True,
            ),
            sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id")),
            sa.Column("name", sa.String(80), nullable=False, server_default=""),
            sa.Column(
                "token_hash", sa.String(64), nullable=False, unique=True, index=True
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
        )
    if "webhook_endpoints" not in tables:
        op.create_table(
            "webhook_endpoints",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "name", sa.String(length=120), nullable=False, server_default=""
            ),
            sa.Column("url", sa.String(length=600), nullable=False),
            sa.Column(
                "secret_encrypted", sa.Text(), nullable=False, server_default=""
            ),
            sa.Column(
                "events_json", sa.Text(), nullable=False, server_default="[]"
            ),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column(
                "created_by",
                sa.String(length=36),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_webhook_endpoints_workspace_id", "webhook_endpoints", ["workspace_id"]
        )
    if "webhook_deliveries" not in tables:
        op.create_table(
            "webhook_deliveries",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("endpoint_id", sa.String(length=36), nullable=False),
            sa.Column("event", sa.String(length=40), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column(
                "last_error", sa.Text(), nullable=False, server_default=""
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
        )
        for name, cols, unique in _DELIVERY_INDEXES:
            op.create_index(name, "webhook_deliveries", cols, unique=unique)


def downgrade() -> None:
    # Symmetrically guarded: drop only what is actually there. `api_tokens` is
    # dropped too — in OUR chain this revision is its only creator; a merged
    # deployment that got it from 0048 will have run that chain, whose own
    # downgrade owns the table there.
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "webhook_deliveries" in tables:
        live = {
            index["name"] for index in inspector.get_indexes("webhook_deliveries")
        }
        for name, _cols, _unique in _DELIVERY_INDEXES:
            if name in live:
                op.drop_index(name, table_name="webhook_deliveries")
        op.drop_table("webhook_deliveries")
    if "webhook_endpoints" in tables:
        live = {
            index["name"] for index in inspector.get_indexes("webhook_endpoints")
        }
        if "ix_webhook_endpoints_workspace_id" in live:
            op.drop_index(
                "ix_webhook_endpoints_workspace_id", table_name="webhook_endpoints"
            )
        op.drop_table("webhook_endpoints")
    if "api_tokens" in tables:
        op.drop_table("api_tokens")
