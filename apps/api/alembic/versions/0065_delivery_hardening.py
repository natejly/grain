"""QA hardening for the two machine doors: retry backoff and a mail flood cap.

Three columns, no new table. `webhook_deliveries.next_attempt_at` is the
retry schedule: a failed send attempt stamps when the next claim may happen
(exponential spread in services/webhooks), so a receiver that is down for a
deploy gets hours of horizon instead of ~3 minutes, and NULL keeps meaning
"due now" for fresh rows. `inbound_addresses.daily_count` /
`daily_count_day` are the per-address per-UTC-day delivery counter the
inbound-email door checks — mail beyond the cap is a quiet 200 that lands
nothing (services/inbound_email.DAILY_CAP).

Revision ID: 0065_delivery_hardening
Revises: 0064_open_alert_unique
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0065_delivery_hardening"
down_revision = "0064_open_alert_unique"
branch_labels = None
depends_on = None

#: (table, column name, type, server_default or None-for-nullable)
_COLUMNS = (
    ("webhook_deliveries", "next_attempt_at", sa.DateTime(), None),
    ("inbound_addresses", "daily_count", sa.Integer(), "0"),
    ("inbound_addresses", "daily_count_day", sa.String(length=10), ""),
)


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Table-existence first (the 0042 add-column template): the replay test
    # runs this chain against a deliberately partial legacy database, and
    # inspecting the columns of an absent table raises rather than answering
    # "none". A database built from empty already has the columns from
    # `create_all`, which the per-column guard makes a no-op.
    inspector = sa.inspect(op.get_bind())
    for table, name, kind, server_default in _COLUMNS:
        if not inspector.has_table(table):
            continue
        if name in _columns(table):
            continue
        if server_default is None:
            op.add_column(table, sa.Column(name, kind, nullable=True))
        else:
            op.add_column(
                table,
                sa.Column(name, kind, nullable=False, server_default=server_default),
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table, name, _kind, _server_default in reversed(_COLUMNS):
        if not inspector.has_table(table):
            continue
        if name in _columns(table):
            op.drop_column(table, name)
