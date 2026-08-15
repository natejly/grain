"""An invitation can be withdrawn.

`workspace_invites` has existed since 0013 and was referenced by nothing: the
table, its token hash, its role and its expiry were all written and never read.
Wiring it up needs exactly one column it did not have — a way to say "this link
should stop working" that is distinct from "nobody has used it yet".

Deleting the row would have served for the link, but not for the owner: an
invitation withdrawn is a fact about the workspace, and a row that vanishes
leaves the Members panel unable to tell "revoked" from "never sent". Nullable
and null by default, so every existing row is, correctly, not revoked.

Revision ID: 0039_invite_revocation
Revises: 0038_conversation_subjects
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0039_invite_revocation"
down_revision = "0038_conversation_subjects"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Idempotent, matching 0024–0038: these databases have been through
    # `create_all` as well as alembic, so the column can already be present.
    if "revoked_at" not in _columns("workspace_invites"):
        op.add_column(
            "workspace_invites", sa.Column("revoked_at", sa.DateTime(), nullable=True)
        )


def downgrade() -> None:
    if "revoked_at" in _columns("workspace_invites"):
        op.drop_column("workspace_invites", "revoked_at")
