"""Notification digests: per-member opt-in columns on memberships.

Three columns, no new table. `digest_enabled` is the opt-in (off by default —
unattended email is something a member asks for), `digest_hour_utc` the UTC
hour after which the daily mail may go, and `digest_last_sent_at` the
per-member claim the tick's conditional UPDATE advances so however many ticks
land after the hour, at most one send per member per day wins. The sweep's
at-most-hourly gate reuses the `sweep_claims` table 0050 created; nothing new
is needed there.

Revision ID: 0063_digests
Revises: 0062_inbound_email
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0063_digests"
down_revision = "0062_inbound_email"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("digest_enabled", sa.Boolean(), sa.false()),
    ("digest_hour_utc", sa.Integer(), "9"),
    ("digest_last_sent_at", sa.DateTime(), None),
)


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Table-existence first (the 0042 add-column template): the replay test
    # runs this chain against a deliberately partial legacy database, and
    # inspecting the columns of an absent table raises rather than answering
    # "none". A database built from empty already has the columns from
    # `create_all`, which the per-column guard makes a no-op.
    if not sa.inspect(op.get_bind()).has_table("memberships"):
        return
    live = _columns("memberships")
    for name, kind, server_default in _COLUMNS:
        if name in live:
            continue
        if server_default is None:
            op.add_column("memberships", sa.Column(name, kind, nullable=True))
        else:
            op.add_column(
                "memberships",
                sa.Column(name, kind, nullable=False, server_default=server_default),
            )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("memberships"):
        return
    live = _columns("memberships")
    for name, _kind, _server_default in reversed(_COLUMNS):
        if name in live:
            op.drop_column("memberships", name)
