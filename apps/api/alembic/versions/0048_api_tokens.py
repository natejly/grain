"""Bearer tokens for the workspace's own MCP server surface.

External agents (Claude Code, Codex, an Agents-SDK stack) reach
`POST /api/mcp` with a bearer secret; this table holds the sha256 of each
secret plus the member it acts as. Revocation stamps rather than deletes, so
the trail can always answer what a dead credential was.

Revision ID: 0048_api_tokens
Revises: 0047_favorites
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0048_api_tokens"
down_revision = "0047_favorites"
branch_labels = None
depends_on = None

_TABLE = "api_tokens"


def upgrade() -> None:
    # Guarded like 0043-0047: a database built from empty already has the
    # table from `create_all`.
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), index=True
        ),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id")),
        sa.Column("name", sa.String(80), nullable=False, server_default=""),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    # Guarded the same way down as up: drop only what is actually there.
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table(_TABLE):
        op.drop_table(_TABLE)
