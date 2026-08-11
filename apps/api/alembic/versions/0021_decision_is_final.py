"""Make a human decision on a tool call unoverwritable by a second one.

`POST /api/{agent-,}tool-calls/{id}/decision` used to read `status == 'proposed'`
in Python and then assign the new status. Two reviewers who opened the same card
therefore both passed the check, both committed, and both scheduled the resume —
last writer wins, so an approval could replace a denial and the tool the human
refused ran anyway. The routes now claim the row with
`UPDATE ... WHERE status = 'proposed'` and let the rowcount pick the winner.

This migration adds the same rule to the store, so it holds for any writer and
not only for the two routes: once a row carries `approved` or `denied`, a later
*contradicting* decision does not land. Only approved <-> denied is refused —
`proposed -> approved` is the decision, and `approved -> executing | succeeded |
failed` is the executor reporting on the call that was approved.

The SQL is spelled out here rather than imported from `app.models` on purpose: a
migration is a snapshot of what was applied on the day it ran, and importing the
live definition would silently rewrite this one's meaning the next time the
trigger changes.

Revision ID: 0021_decision_is_final
Revises: 0020_tool_policy_scope
"""
from __future__ import annotations

from alembic import op

revision = "0021_decision_is_final"
down_revision = "0020_tool_policy_scope"
branch_labels = None
depends_on = None

_TABLES = ("tool_calls", "agent_tool_calls")


def _sqlite(table: str) -> str:
    # SQLite cannot rewrite NEW, so the row is written and then put back. The
    # restoring UPDATE does not re-enter the trigger because SQLite leaves
    # `PRAGMA recursive_triggers` off; a BEFORE ... RAISE(IGNORE) would skip the
    # row instead, which reports 0 rows matched and makes every ORM flush raise
    # rather than simply having no effect.
    return f"""
    CREATE TRIGGER IF NOT EXISTS {table}_decision_is_final
    AFTER UPDATE OF status ON {table}
    FOR EACH ROW
    WHEN OLD.status IN ('approved', 'denied')
     AND NEW.status IN ('approved', 'denied')
     AND NEW.status <> OLD.status
    BEGIN
        UPDATE {table}
           SET status = OLD.status,
               decided_by = OLD.decided_by,
               decided_at = OLD.decided_at
         WHERE id = OLD.id;
    END
    """


def _postgresql(table: str) -> str:
    return f"""
    CREATE OR REPLACE FUNCTION {table}_decision_is_final() RETURNS trigger AS $$
    BEGIN
        IF OLD.status IN ('approved', 'denied')
           AND NEW.status IN ('approved', 'denied')
           AND NEW.status <> OLD.status THEN
            NEW.status := OLD.status;
            NEW.decided_by := OLD.decided_by;
            NEW.decided_at := OLD.decided_at;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    DROP TRIGGER IF EXISTS {table}_decision_is_final ON {table};
    CREATE TRIGGER {table}_decision_is_final
    BEFORE UPDATE OF status ON {table}
    FOR EACH ROW EXECUTE FUNCTION {table}_decision_is_final();
    """


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    for table in _TABLES:
        if dialect == "sqlite":
            op.execute(_sqlite(table))
        elif dialect == "postgresql":
            op.execute(_postgresql(table))


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    for table in _TABLES:
        if dialect == "postgresql":
            op.execute(f"DROP TRIGGER IF EXISTS {table}_decision_is_final ON {table}")
            op.execute(f"DROP FUNCTION IF EXISTS {table}_decision_is_final()")
        else:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_decision_is_final")
