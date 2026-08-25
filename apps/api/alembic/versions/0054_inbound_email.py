"""Inbound email addresses: mail routed into a workspace thread.

One table. `inbound_addresses` maps a hashed routing token — the local-part
secret in `inbox+<token>@<domain>` — to the workspace, the member delivered
threads are created as, and an optional target space. Only the token's sha256
is stored, the ApiToken posture: the table can recognise a recipient and can
never leak an address.

Revision ID: 0054_inbound_email
Revises: 0053_webhooks_api_tokens
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0054_inbound_email"
down_revision = "0053_webhooks_api_tokens"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_inbound_addresses_workspace_id", ["workspace_id"], False),
    ("ix_inbound_addresses_token_hash", ["token_hash"], True),
)


def upgrade() -> None:
    # Guarded like 0024-0053: a database built from empty already has the
    # table from `create_all`, and has_table is checked BEFORE any column or
    # index inspection because the replay test runs this chain against a
    # deliberately partial legacy database.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "inbound_addresses" in tables:
        return
    op.create_table(
        "inbound_addresses",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(length=36),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False, server_default=""),
        sa.Column(
            "target_space_id",
            sa.String(length=36),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "created_by",
            sa.String(length=36),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    for name, cols, unique in _INDEXES:
        op.create_index(name, "inbound_addresses", cols, unique=unique)


def downgrade() -> None:
    # Symmetrically guarded: drop only what is actually there.
    inspector = sa.inspect(op.get_bind())
    if "inbound_addresses" not in inspector.get_table_names():
        return
    live = {index["name"] for index in inspector.get_indexes("inbound_addresses")}
    for name, _cols, _unique in _INDEXES:
        if name in live:
            op.drop_index(name, table_name="inbound_addresses")
    op.drop_table("inbound_addresses")
