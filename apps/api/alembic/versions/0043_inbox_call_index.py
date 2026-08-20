"""Index agent_tool_calls for the Inbox's unbounded waiting-set query.

`GET /api/inbox` lists every proposed call whose run is parked — deliberately
without a LIMIT, because the previous fifty-row window was how a parked
approval could vanish from every surface while its run sat waiting. Unbounded
is only safe if the filter is index-served: the waiting set is small, but the
predicate that finds it (`workspace_id AND status='proposed' ORDER BY
created_at`) would otherwise scan every call the workspace ever made, and that
table only grows.

Schema-only: one composite index, no rows touched, no rebuilds. Guarded
create/drop so a database whose tables were made by `create_all` (which reads
the same declaration off the model) upgrades cleanly instead of erroring on
"already exists".

NOTE for the parallel feature branches: another in-flight plan also names
"0043" in its filename. Alembic links revisions by these id strings, not by
filenames — if both land, the second needs its `down_revision` re-pointed here
(or a merge revision), and nothing else changes.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0043_inbox_call_index"
down_revision = "0042_spaces"
branch_labels = None
depends_on = None

INDEX = "ix_agent_tool_calls_workspace_status_created"


def _table_missing() -> bool:
    # Table-existence first, like 0042's guard: `get_indexes` raises on a table
    # that does not exist, and test_personal_scope_migration replays this chain
    # against a deliberately partial pre-0040 database that carries only the
    # tables 0040 rebuilds. A real database always has agent_tool_calls (0001
    # creates it), so skipping changes nothing outside that harness — and the
    # right degrade is to do nothing: no table, no index to serve it.
    return not sa.inspect(op.get_bind()).has_table("agent_tool_calls")


def _index_names() -> set[str]:
    bind = op.get_bind()
    return {index["name"] for index in sa.inspect(bind).get_indexes("agent_tool_calls")}


def upgrade() -> None:
    if _table_missing():
        return
    if INDEX not in _index_names():
        op.create_index(
            INDEX, "agent_tool_calls", ["workspace_id", "status", "created_at"]
        )


def downgrade() -> None:
    if _table_missing():
        return
    if INDEX in _index_names():
        op.drop_index(INDEX, table_name="agent_tool_calls")
