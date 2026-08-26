"""Safe mode: agentic by default, asking by choice.

Three changes, one decision. `memberships.safe_mode` is the per-member opt-in
to the approval step (off by default). `conversations.approval_mode` flips its
server default from `ask_writes` to `auto_writes`, so a thread created from now
on acts and reports rather than stopping at every write. And the existing rows
still sitting on `ask_writes` are moved with it.

That backfill is the one part worth arguing for, because it rewrites state a
person could have chosen. It is safe *at this instant and only at this instant*:
`safe_mode` does not exist until the line above adds it, so no member has opted
into asking yet, and `ask_writes` on every one of these rows is the old column
default rather than a decision anybody made. Run it later, after members have
had the switch, and it would be overwriting real answers — which is why it is
guarded on the old default and lives in the same migration as the column that
makes the preference expressible.

Deliberately NOT backfilled: `ask_all`, `plan`, and `guardian` rows. Those modes
are only ever reached by picking them, and a migration has no business
un-picking them.

The downgrade restores the old server default for new rows and drops the column.
It does not put the backfilled rows back — `auto_writes` and "was ask_writes
before 0064" are indistinguishable afterwards, and inventing the difference
would be a worse lie than leaving the mode where the upgrade put it.

What this does not touch, because none of it is the approval mode's to grant:
a policy `deny` still denies, workflow scope still ignores the mode entirely,
and a turn the prompt-injection screen flags is still forced to `ask_all` over
whatever is stored here (`agent_loop.approval_mode_for_run`).

Revision ID: 0064_safe_mode
Revises: 0063_digests
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0064_safe_mode"
down_revision = "0063_digests"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    # Table-existence first (the 0042 add-column template): the replay test runs
    # this chain against a deliberately partial legacy database, where a missing
    # table must be a skip rather than a raise from the column inspector.
    if sa.inspect(bind).has_table("memberships"):
        if "safe_mode" not in _columns("memberships"):
            op.add_column(
                "memberships",
                sa.Column(
                    "safe_mode",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                ),
            )

    if not sa.inspect(bind).has_table("conversations"):
        return
    if "approval_mode" not in _columns("conversations"):
        # 0032 skipped, or a database built from `create_all` at a revision
        # before that column existed. Nothing here to re-default.
        return
    # SQLite cannot ALTER a default in place, so both dialects go through
    # batch_alter_table, which rebuilds the table there and emits a plain
    # ALTER on Postgres.
    with op.batch_alter_table("conversations") as batch:
        batch.alter_column(
            "approval_mode",
            existing_type=sa.String(24),
            existing_nullable=False,
            server_default="auto_writes",
        )
    # Guarded on the old default: see the module docstring for why this is only
    # correct in this migration.
    op.execute(
        sa.text(
            "UPDATE conversations SET approval_mode = 'auto_writes' "
            "WHERE approval_mode = 'ask_writes'"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("conversations") and "approval_mode" in _columns(
        "conversations"
    ):
        with op.batch_alter_table("conversations") as batch:
            batch.alter_column(
                "approval_mode",
                existing_type=sa.String(24),
                existing_nullable=False,
                server_default="ask_writes",
            )
    if sa.inspect(bind).has_table("memberships") and "safe_mode" in _columns(
        "memberships"
    ):
        op.drop_column("memberships", "safe_mode")
