"""Favorites: anything with a name, pinned to one person's sidebar.

One new per-user table. `kind` + `target_id` name any of eight kinds of row
(the closed set in `services/favorites.FAVORITE_KINDS`); `target_id` carries
no ForeignKey because one column cannot reference eight tables, and a favorite
must survive its target's deletion the way `runs.skill_id` survives a skill's
— listings simply stop resolving it. The unique key is (user_id, kind,
target_id): favoriting is a fact, not a log.

Revision ID: 0047_favorites
Revises: 0046_model_usage_agent
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0047_favorites"
down_revision = "0046_model_usage_agent"
branch_labels = None
depends_on = None

_TABLE = "favorites"
#: Matches what `create_all` builds from the model — index=True on the two FK
#: columns plus the composite the listing query walks — so a database migrated
#: from empty and one migrated from here converge on the same names.
_INDEXES = (
    ("ix_favorites_workspace_id", ["workspace_id"]),
    ("ix_favorites_user_id", ["user_id"]),
    ("ix_favorites_workspace_user", ["workspace_id", "user_id"]),
)


def upgrade() -> None:
    # Guarded like 0042–0046: a database migrated from empty already has this
    # table from `create_all`, and creating it again would fail.
    if sa.inspect(op.get_bind()).has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False
        ),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("target_id", sa.String(36), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "kind", "target_id"),
    )
    for name, columns in _INDEXES:
        op.create_index(name, _TABLE, columns)


def downgrade() -> None:
    # Guarded the same way down as up (the 0044 lesson, both directions): drop
    # only what is actually there.
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return
    live = {index["name"] for index in inspector.get_indexes(_TABLE)}
    for name, _columns in _INDEXES:
        if name in live:
            op.drop_index(name, table_name=_TABLE)
    op.drop_table(_TABLE)
