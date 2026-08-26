"""Live coworking: card claims, presence heartbeats, workspace events.

Three shapes for one feature. `board_cards` grows claim-lease columns so a
card can say who is working it (and until when — an expired claim reads as
free, no sweep). `presences` holds one upserted row per (actor, surface) with
cursor/typing state, ephemeral by TTL read. `workspace_events` is the
workspace-scoped sibling of `run_events`: one append-only log a shell tails
over a single SSE connection.

Revision ID: 0049_coworking
Revises: 0048_api_tokens
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0049_coworking"
down_revision = "0048_api_tokens"
branch_labels = None
depends_on = None

_CARD_COLUMNS = {
    "claimed_by": sa.Column(
        "claimed_by", sa.String(36), nullable=False, server_default=""
    ),
    "claimed_kind": sa.Column(
        "claimed_kind", sa.String(8), nullable=False, server_default=""
    ),
    "claimed_label": sa.Column(
        "claimed_label", sa.String(120), nullable=False, server_default=""
    ),
    "claimed_run_id": sa.Column(
        "claimed_run_id", sa.String(36), nullable=False, server_default=""
    ),
    "claim_expires_at": sa.Column("claim_expires_at", sa.DateTime(), nullable=True),
}


def upgrade() -> None:
    # Guarded like 0043-0048: a database built from empty already has all of
    # this from `create_all`.
    inspector = sa.inspect(op.get_bind())

    # A database that never had boards (a minimal fixture, a fresh install
    # about to `create_all`) gets the columns with the table itself.
    if inspector.has_table("board_cards"):
        existing = {column["name"] for column in inspector.get_columns("board_cards")}
        for name, column in _CARD_COLUMNS.items():
            if name not in existing:
                op.add_column("board_cards", column)

    if not inspector.has_table("workspace_events"):
        op.create_table(
            "workspace_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                index=True,
            ),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(40), nullable=False),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "sequence"),
        )
        op.create_index(
            "ix_workspace_events_ws_sequence",
            "workspace_events",
            ["workspace_id", "sequence"],
        )

    if not inspector.has_table("presences"):
        op.create_table(
            "presences",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                index=True,
            ),
            sa.Column("actor_id", sa.String(36), nullable=False),
            sa.Column("actor_kind", sa.String(8), nullable=False, server_default="user"),
            sa.Column(
                "actor_label", sa.String(120), nullable=False, server_default=""
            ),
            sa.Column("surface", sa.String(120), nullable=False, server_default=""),
            sa.Column("state_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False, index=True),
            sa.UniqueConstraint("workspace_id", "actor_id", "surface"),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("presences"):
        op.drop_table("presences")
    if inspector.has_table("workspace_events"):
        op.drop_index(
            "ix_workspace_events_ws_sequence", table_name="workspace_events"
        )
        op.drop_table("workspace_events")
    if inspector.has_table("board_cards"):
        existing = {
            column["name"] for column in inspector.get_columns("board_cards")
        }
        for name in _CARD_COLUMNS:
            if name in existing:
                op.drop_column("board_cards", name)
