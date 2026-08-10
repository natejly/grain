"""Documents, kanban boards, and the approval-card change preview.

Revision ID: 0008_artifacts
Revises: 0007_mcp
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008_artifacts"
down_revision = "0007_mcp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    columns = {column["name"] for column in inspector.get_columns("agent_tool_calls")}
    if "proposal_preview" not in columns:
        op.add_column(
            "agent_tool_calls",
            sa.Column(
                "proposal_preview", sa.Text(), nullable=False, server_default=""
            ),
        )

    if "documents" not in tables:
        op.create_table(
            "documents",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("kind", sa.String(16), nullable=False, server_default="markdown"),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_documents_workspace_id", "documents", ["workspace_id"])

    if "document_versions" not in tables:
        op.create_table(
            "document_versions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "document_id",
                sa.String(36),
                sa.ForeignKey("documents.id"),
                nullable=False,
            ),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("summary", sa.String(300), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_document_versions_workspace_id", "document_versions", ["workspace_id"]
        )
        op.create_index(
            "ix_document_versions_document_id", "document_versions", ["document_id"]
        )
        op.create_index(
            "ix_document_versions_doc_created",
            "document_versions",
            ["document_id", "created_at"],
        )

    if "boards" not in tables:
        op.create_table(
            "boards",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_boards_workspace_id", "boards", ["workspace_id"])

    if "board_columns" not in tables:
        op.create_table(
            "board_columns",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "board_id", sa.String(36), sa.ForeignKey("boards.id"), nullable=False
            ),
            sa.Column("name", sa.String(80), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index("ix_board_columns_workspace_id", "board_columns", ["workspace_id"])
        op.create_index("ix_board_columns_board_id", "board_columns", ["board_id"])

    if "board_cards" not in tables:
        op.create_table(
            "board_cards",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "board_id", sa.String(36), sa.ForeignKey("boards.id"), nullable=False
            ),
            sa.Column(
                "column_id",
                sa.String(36),
                sa.ForeignKey("board_columns.id"),
                nullable=False,
            ),
            sa.Column("title", sa.String(300), nullable=False),
            sa.Column("body", sa.Text(), nullable=False, server_default=""),
            sa.Column("labels_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_board_cards_workspace_id", "board_cards", ["workspace_id"])
        op.create_index("ix_board_cards_board_id", "board_cards", ["board_id"])
        op.create_index("ix_board_cards_column_id", "board_cards", ["column_id"])
        op.create_index(
            "ix_board_cards_column_position", "board_cards", ["column_id", "position"]
        )


def downgrade() -> None:
    op.drop_table("board_cards")
    op.drop_table("board_columns")
    op.drop_table("boards")
    op.drop_table("document_versions")
    op.drop_table("documents")
    op.drop_column("agent_tool_calls", "proposal_preview")
