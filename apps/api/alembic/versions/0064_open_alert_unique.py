"""One OPEN monitor alert per monitor, enforced by the database.

`monitors._open_alert_exists` narrowed the duplicate-alert race (a claim-free
run-now evaluating the same crossing concurrently with the tick) but is a
check-then-insert. This partial unique index on `notifications.monitor_id`
WHERE the row is an open monitor_alert closes it: the loser's commit raises,
and `monitors.evaluate` already turns any failure into a skip. Scoped to real
monitor ids, so every other notification kind (monitor_id '') is untouched.

Before creating the index, any duplicates the race already produced are
resolved down to one surviving open row per monitor — otherwise the CREATE
UNIQUE INDEX itself would fail on exactly the databases that need it. The
survivor is the *most recent* crossing (`created_at`, ties broken by id so the
choice is deterministic and the same on every replica), because that is the one
whose title and body describe the state the monitor is actually in; the rows it
supersedes are resolved the way the app resolves a notification — status and
`resolved_at` together — rather than left in a third state that is neither open
nor honestly acknowledged.

Revision ID: 0064_open_alert_unique
Revises: 0063_digests
"""
from __future__ import annotations

from datetime import datetime, timezone

import sqlalchemy as sa

from alembic import op

revision = "0064_open_alert_unique"
down_revision = "0063_digests"
branch_labels = None
depends_on = None

INDEX = "uq_notifications_open_monitor_alert"
WHERE = "kind = 'monitor_alert' AND status = 'open' AND monitor_id != ''"


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    # Table-existence first (the house guard template): the replay test runs this
    # chain against a deliberately partial legacy database, and inspecting an
    # absent table raises rather than answering "none". A database built from
    # empty already has the index from `create_all`, which the guard no-ops.
    if not sa.inspect(op.get_bind()).has_table("notifications"):
        return
    if INDEX in _indexes("notifications"):
        return
    # Duplicates the check-then-insert race already landed: keep the newest
    # open row per monitor, resolve the rest so the unique index can be built.
    # "Has a later sibling" rather than a MAX() over the id — ids are uuid4
    # strings, so MAX(id) picks a lexicographic winner, not a recent one. The
    # id only breaks ties on identical `created_at`, which the race can produce.
    # The newest row is by construction not a loser, so no evaluation order
    # inside this statement can leave a monitor with nothing open.
    op.get_bind().execute(
        sa.text(
            "UPDATE notifications SET status = 'resolved', resolved_at = :moment "
            f"WHERE {WHERE} AND EXISTS ("
            "SELECT 1 FROM notifications AS later "
            "WHERE later.kind = 'monitor_alert' AND later.status = 'open' "
            "AND later.monitor_id != '' "
            "AND later.monitor_id = notifications.monitor_id "
            "AND (later.created_at > notifications.created_at "
            "OR (later.created_at = notifications.created_at "
            "AND later.id > notifications.id)))"
        ),
        # The app stamps `resolved_at` whenever it flips a notification out of
        # the waiting set, and this migration is what flipped these: now is the
        # honest moment, naive UTC per the house clock. `resolved_by` stays ''
        # — no member acknowledged these, and saying one did would be a
        # nicer-looking lie.
        {"moment": datetime.now(timezone.utc).replace(tzinfo=None)},
    )
    op.create_index(
        INDEX,
        "notifications",
        ["monitor_id"],
        unique=True,
        sqlite_where=sa.text(WHERE),
        postgresql_where=sa.text(WHERE),
    )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table("notifications"):
        return
    if INDEX in _indexes("notifications"):
        op.drop_index(INDEX, table_name="notifications")
