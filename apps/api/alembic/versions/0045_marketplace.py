"""Marketplace: publish and share skills (later workflows and agents).

Two new tables, nothing existing changes shape. `listings` is the mutable head
(slug, title, visibility, status) and `listing_versions` holds the immutable
published payloads — the `GeneratedApp`/`AppRelease` split, applied to the
things a person authors in the composer. Installing copies a payload into the
caller's workspace as an ordinary local row, so no other table needs to learn
about the marketplace for it to work.

`organization_id` lives on the listing itself rather than being read through
the publisher's workspace, because org visibility filters on it directly — the
boundary column belongs to the row the boundary governs. `status` reserves
'taken_down' from day one so the report→takedown flow needs no schema change.

Revision ID: 0045_marketplace
Revises: 0044_conversation_index
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0045_marketplace"
down_revision = "0044_conversation_index"
branch_labels = None
depends_on = None

_TABLES = ("listing_versions", "listings")

_LISTING_INDEXES = (
    ("ix_listings_organization_id", ["organization_id"]),
    ("ix_listings_workspace_id", ["workspace_id"]),
)

_VERSION_INDEXES = (
    ("ix_listing_versions_workspace_id", ["workspace_id"]),
    ("ix_listing_versions_listing_id", ["listing_id"]),
    ("ix_listing_versions_ws_listing", ["workspace_id", "listing_id"]),
)


def upgrade() -> None:
    # Guarded like 0024-0044: a database migrated from empty already got these
    # tables from `create_all`, and creating them again would fail.
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("listings"):
        op.create_table(
            "listings",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "organization_id",
                sa.String(36),
                sa.ForeignKey("organizations.id"),
                nullable=False,
            ),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("kind", sa.String(20), nullable=False),
            sa.Column("slug", sa.String(80), nullable=False),
            sa.Column("title", sa.String(160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "visibility", sa.String(20), nullable=False, server_default="workspace"
            ),
            sa.Column(
                "status", sa.String(20), nullable=False, server_default="published"
            ),
            sa.Column("author_name", sa.String(120), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
            sa.Column("install_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "latest_version", sa.Integer(), nullable=False, server_default="1"
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("organization_id", "slug"),
        )
        for name, cols in _LISTING_INDEXES:
            op.create_index(name, "listings", cols)

    if not inspector.has_table("listing_versions"):
        op.create_table(
            "listing_versions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "listing_id",
                sa.String(36),
                sa.ForeignKey("listings.id"),
                nullable=False,
            ),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
            sa.Column("changelog", sa.Text(), nullable=False, server_default=""),
            sa.Column("source_id", sa.String(36), nullable=False, server_default=""),
            sa.Column("source_version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("listing_id", "version"),
        )
        for name, cols in _VERSION_INDEXES:
            op.create_index(name, "listing_versions", cols)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in _TABLES:
        if not inspector.has_table(table):
            continue
        live = {index["name"] for index in inspector.get_indexes(table)}
        indexes = _VERSION_INDEXES if table == "listing_versions" else _LISTING_INDEXES
        for name, _cols in indexes:
            if name in live:
                op.drop_index(name, table_name=table)
        op.drop_table(table)
