"""Folders, and the folder a document is filed in.

Documents were a flat list, which is fine at ten and unusable at two hundred —
and the destination that holds them is now called Files, which is a promise the
flat list could not keep.

`folders.parent_id` and `documents.folder_id` are both empty-string-for-none
rather than NULL. Two reasons, and neither is taste: every query here groups or
filters by the column, and NULL does not group with itself in SQL; and the
existing columns in this schema that mean "unset" (`conversations.document_id`,
`documents.created_by`) already say so with "", so a nullable one here would
make the same idea have two spellings in one table.

Revision ID: 0030_document_folders
Revises: 0029_dashboard_templates_and_pins
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0030_document_folders"
down_revision = "0029_dashboard_templates_and_pins"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    # Idempotent, matching 0024–0029: these databases have been through
    # `create_all` as well as alembic, so the objects can already be present.
    if "folders" not in _tables():
        op.create_table(
            "folders",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("parent_id", sa.String(36), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
    if "ix_folders_workspace_id" not in _indexes("folders"):
        op.create_index("ix_folders_workspace_id", "folders", ["workspace_id"])
    if "ix_folders_workspace_parent" not in _indexes("folders"):
        op.create_index(
            "ix_folders_workspace_parent", "folders", ["workspace_id", "parent_id"]
        )

    if "folder_id" not in _columns("documents"):
        op.add_column(
            "documents",
            sa.Column("folder_id", sa.String(36), nullable=False, server_default=""),
        )
    if "ix_documents_folder_id" not in _indexes("documents"):
        op.create_index("ix_documents_folder_id", "documents", ["folder_id"])


def downgrade() -> None:
    op.drop_index("ix_documents_folder_id", table_name="documents")
    op.drop_column("documents", "folder_id")
    op.drop_index("ix_folders_workspace_parent", table_name="folders")
    op.drop_index("ix_folders_workspace_id", table_name="folders")
    op.drop_table("folders")
