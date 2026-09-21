"""Response styles, message feedback, and the PDF page count.

THE cycle's single migration — the placeholder note: columns flagged by the
export/share-links designer and the search/recap designer land HERE (check
their cluster specs for a "0072 column note" before merging; this file owns
the cycle's only migration slot and the other designers were told to design
migration-free). Collected so far:

* `sources.page_count` — the surfaces cluster's OPTIONAL note (its PDF card
  upgrades from a byte size to a true page count); recorded by ingestion for
  PDFs, 0 for everything else.

What this migration carries of its own:

* `memberships.style_preset` / `memberships.custom_style_text` — the
  per-member response style. "normal" means NO style block is injected, so
  the server default IS the byte-identity contract.
* `message_feedback` — one member's thumbs verdict per assistant message,
  unique on (message_id, user_id): the constraint is the upsert's concurrency
  control, the Membership invite-acceptance doctrine.

`users.display_name` is EXPLICITLY NOT INCLUDED: `users.name` already exists
(models.py), is set at signup from the payload or the email local part, and is
already the attribution source everywhere (`Actor.user_name` → bootstrap
Identity, coworking actor labels). `PATCH /api/me/profile` edits it in place,
migration-free.

No backfill: the server defaults are the correct historical truth — everyone
was "normal", no feedback existed, and no page count was recorded.

All adds are guarded with 0071's inspector template (has_table first, then
column presence), so the partial-legacy replay test and databases built by
`create_all` both skip cleanly. The downgrade drops the table, then the
columns, guarded the same way — a create_all database arrives at 0001 holding
today's columns, and an unguarded drop would fail on exactly the databases
built cleanly from scratch.

Revision ID: 0072_identity_styles_feedback
Revises: 0071_memory_prefs
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0072_identity_styles_feedback"
down_revision = "0071_memory_prefs"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("memberships"):
        if "style_preset" not in _columns("memberships"):
            op.add_column(
                "memberships",
                sa.Column(
                    "style_preset",
                    sa.String(length=16),
                    nullable=False,
                    server_default="normal",
                ),
            )
        if "custom_style_text" not in _columns("memberships"):
            op.add_column(
                "memberships",
                sa.Column(
                    "custom_style_text",
                    sa.Text(),
                    nullable=False,
                    server_default="",
                ),
            )
    if inspector.has_table("sources") and "page_count" not in _columns("sources"):
        op.add_column(
            "sources",
            sa.Column(
                "page_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
    if not inspector.has_table("message_feedback"):
        op.create_table(
            "message_feedback",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "message_id",
                sa.String(length=36),
                sa.ForeignKey("messages.id"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                sa.String(length=36),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("verdict", sa.String(length=8), nullable=False),
            sa.Column("note", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "message_id", "user_id", name="uq_message_feedback_message_user"
            ),
        )
        op.create_index(
            "ix_message_feedback_workspace_id", "message_feedback", ["workspace_id"]
        )
        op.create_index(
            "ix_message_feedback_message_id", "message_feedback", ["message_id"]
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("message_feedback"):
        op.drop_table("message_feedback")
    if inspector.has_table("sources") and "page_count" in _columns("sources"):
        op.drop_column("sources", "page_count")
    if inspector.has_table("memberships"):
        if "custom_style_text" in _columns("memberships"):
            op.drop_column("memberships", "custom_style_text")
        if "style_preset" in _columns("memberships"):
            op.drop_column("memberships", "style_preset")
