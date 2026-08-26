"""Share links: revocable read-only public URLs for dashboards and documents.

One new table on `workspace_invites`' shape: only the SHA-256 of the link is
stored (`token_hash`, unique — it is the public route's whole lookup key),
`expires_at` bounds a leaked link's worth, and `revoked_at` is a terminal
timestamp rather than a DELETE. `resource_kind`/`resource_id` are the
polymorphic-subject pair ('dashboard' | 'document'); `resource_id` is a plain
column, never a ForeignKey, so deleting the shared thing simply makes the
public route 404 instead of blocking the delete. Published apps already have
their own public surface and are deliberately not a kind here.

Revision ID: 0059_share_links
Revises: 0058_usage_agent
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0059_share_links"
down_revision = "0058_usage_agent"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_share_links_workspace_id", ["workspace_id"], False),
    ("ix_share_links_token_hash", ["token_hash"], True),
)


def upgrade() -> None:
    # Guarded like 0024-0050: a database migrated from empty already got this
    # table from `create_all`, and creating it again would fail. has_table is
    # checked BEFORE any inspection of the table, because the replay test runs
    # this chain against a deliberately partial legacy database.
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "share_links" in tables:
        return
    op.create_table(
        "share_links",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(length=36),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("resource_kind", sa.String(length=16), nullable=False),
        sa.Column("resource_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )
    for name, cols, unique in _INDEXES:
        op.create_index(name, "share_links", cols, unique=unique)


def downgrade() -> None:
    # Dropping the table revokes every outstanding link at once — fail-closed,
    # which is the only acceptable direction for a public credential. Guarded
    # symmetrically.
    inspector = sa.inspect(op.get_bind())
    if "share_links" not in inspector.get_table_names():
        return
    live = {index["name"] for index in inspector.get_indexes("share_links")}
    for name, _cols, _unique in _INDEXES:
        if name in live:
            op.drop_index(name, table_name="share_links")
    op.drop_table("share_links")
