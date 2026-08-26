"""Marketplace lineage: one row per workspace per installed listing.

`listing_installs` records that a workspace installed a listing and which
version it took, so updates can be offered ("a newer version exists") and
divergence detected ("you edited your copy") without either one requiring the
installed row itself to know where it came from — copies stay ordinary local
rows. `UNIQUE(workspace_id, listing_id)` keeps lineage unambiguous: a
re-install re-points the row instead of growing a second history.

Revision ID: 0051_listing_installs
Revises: 0050_marketplace
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0051_listing_installs"
down_revision = "0050_marketplace"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_listing_installs_workspace_id", ["workspace_id"]),
    ("ix_listing_installs_listing_id", ["listing_id"]),
)


def upgrade() -> None:
    # Guarded like 0024-0045: a database migrated from empty already got the
    # table from `create_all`, and creating it again would fail.
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("listing_installs"):
        return
    op.create_table(
        "listing_installs",
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
        sa.Column(
            "listing_version_id",
            sa.String(36),
            sa.ForeignKey("listing_versions.id"),
            nullable=False,
        ),
        sa.Column("target_kind", sa.String(20), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column(
            "content_hash_at_install", sa.String(64), nullable=False, server_default=""
        ),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "listing_id"),
    )
    for name, cols in _INDEXES:
        op.create_index(name, "listing_installs", cols)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("listing_installs"):
        return
    live = {index["name"] for index in inspector.get_indexes("listing_installs")}
    for name, _cols in _INDEXES:
        if name in live:
            op.drop_index(name, table_name="listing_installs")
    op.drop_table("listing_installs")
