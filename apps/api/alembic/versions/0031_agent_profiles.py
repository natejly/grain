"""An agent becomes something a person authors, not a row the signup writes.

`agents` gains the columns a creator needs: a `description` for the list card,
`allowed_tools_json` for the provisioned subset, `created_by` for attribution,
and `updated_at` because an edited system prompt is now an ordinary event.

`allowed_tools_json` is empty-string-for-unset, like every other "unset" in
this schema: "" means the agent sees the whole registry, while a stored JSON
list — `"[]"` included — is an explicit grant. That keeps "unset" to one
spelling and still lets a prompt-only agent with zero tools exist.

Revision ID: 0031_agent_profiles
Revises: 0030_document_folders
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0031_agent_profiles"
down_revision = "0030_document_folders"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Idempotent, matching 0024–0030: these databases have been through
    # `create_all` as well as alembic, so the columns can already be present.
    if "description" not in _columns("agents"):
        op.add_column(
            "agents", sa.Column("description", sa.Text(), nullable=False, server_default="")
        )
    if "allowed_tools_json" not in _columns("agents"):
        op.add_column(
            "agents",
            sa.Column("allowed_tools_json", sa.Text(), nullable=False, server_default=""),
        )
    if "created_by" not in _columns("agents"):
        op.add_column(
            "agents",
            sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
        )
    if "updated_at" not in _columns("agents"):
        # SQLite rejects ALTER TABLE … DEFAULT CURRENT_TIMESTAMP (non-constant).
        # Add nullable, backfill from created_at; the ORM still writes on update.
        op.add_column("agents", sa.Column("updated_at", sa.DateTime(), nullable=True))
        op.execute(sa.text("UPDATE agents SET updated_at = created_at WHERE updated_at IS NULL"))


def downgrade() -> None:
    op.drop_column("agents", "updated_at")
    op.drop_column("agents", "created_by")
    op.drop_column("agents", "allowed_tools_json")
    op.drop_column("agents", "description")
