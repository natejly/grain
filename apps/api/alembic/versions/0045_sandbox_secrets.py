"""Workspace sandbox secrets (encrypted env vars for the execution sandbox).

One table, ``sandbox_secrets``: each row is a credential the workspace lets its
sandbox code read as an environment variable. ``value_enc`` is Fernet ciphertext
under the integrations key; the columns mirror the ORM 1:1 with server_defaults
so a database built through ``create_all`` and one built through alembic agree
byte-for-byte. Idempotent ``if "sandbox_secrets" not in tables`` guard, matching
0036: these databases have been through ``create_all`` as well as alembic, so
the table can already exist and the migration still has to run to completion.

Revision ID: 0045_sandbox_secrets
Revises: 0044_conversation_index

NOTE for the parallel feature branches: other in-flight plans also number their
next migration 0045/0046. Alembic links revisions by these id strings, not by
filenames — if they land first, re-point this `down_revision` onto the new head
(or add a merge revision); nothing else changes.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0045_sandbox_secrets"
down_revision = "0044_conversation_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "sandbox_secrets" not in tables:
        op.create_table(
            "sandbox_secrets",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("value_enc", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "created_by", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "name"),
        )
        op.create_index(
            "ix_sandbox_secrets_workspace_id", "sandbox_secrets", ["workspace_id"]
        )


def downgrade() -> None:
    # Guarded like the upgrade (and 0044/0043): these databases have been built
    # by both create_all and alembic, so the table may already be absent when a
    # downgrade runs. Dropping it unconditionally would raise on that path.
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("sandbox_secrets"):
        return
    live = {index["name"] for index in sa.inspect(bind).get_indexes("sandbox_secrets")}
    if "ix_sandbox_secrets_workspace_id" in live:
        op.drop_index("ix_sandbox_secrets_workspace_id", table_name="sandbox_secrets")
    op.drop_table("sandbox_secrets")
