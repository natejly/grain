"""Give a memory and a tool grant an owner, so a workspace can hold two people.

ADR 0010. `memory_items` and `tool_policies` were addressed by `workspace_id`
alone, which was indistinguishable from "per person" while a workspace had one
member and stopped being so at 0039. Both gain `owner_id` — "" for the
workspace's own, a user id otherwise — and both unique keys grow by that column,
so a personal value for a claim and the shared value for the same claim are two
rows rather than one row fighting over a slot.

**The two backfills differ on purpose, because the evidence differs.**

`memory_items.owner_id` stays "" for every existing row. There is no honest owner
to recover: `run_id` names one run, but `importance` counts how many runs
re-touched the row and `message_ids_json` spans them, so a memory three members
reinforced would be attributed to whichever of them spoke first — a guess, stored
as a fact. And it fails in the losing direction: attributing a row to one member
*removes* it from everyone else's recall, silently, with no error and just a
worse answer. Shared is what every existing row already behaves as, so this
changes what nobody sees. Same reasoning, same direction, as 0037 backfilling
every existing conversation to `shared = 1`.

`tool_policies.owner_id` becomes `created_by`. Nothing is guessed here —
`_upsert_policy` has always stamped the acting user, and the rows it could not
attribute already hold "", which is precisely the sentinel for shared. So the
backfill is one assignment with no CASE. The direction of failure is inverted
too: narrowing a grant deletes nothing, it means a member is asked to approve a
write nobody asked them about yesterday. Fail-closed, one click to restore, and
it applies the fix to the grants that motivated the ADR rather than only to
grants made after it.

Both tables are rebuilt rather than altered. The old unique constraints were
declared inline, so they have no portable name to drop — SQLite has none at all —
which is the same wall 0020 hit when it added `scope`, and this follows the
rename/create/copy/drop it settled on. The honest cost for `memory_items` is that
the copy moves the `embedding` blobs too, ~6KB a row; on a corpus large enough
for that to matter this migration is not instant.

Revision ID: 0040_personal_scope
Revises: 0039_invite_revocation
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0040_personal_scope"
down_revision = "0039_invite_revocation"
branch_labels = None
depends_on = None

_POLICY_COLUMNS = (
    "id, workspace_id, tool_name, policy, scope, created_by, created_at, updated_at"
)
_MEMORY_COLUMNS = (
    "id, workspace_id, conversation_id, run_id, kind, content, normalized_key, "
    "entity_names_json, message_ids_json, importance, status, superseded_by, "
    "embedding, embedding_model, created_at, updated_at"
)
_MEMORY_INDEXES = (
    ("ix_memory_items_workspace_id", ["workspace_id"]),
    ("ix_memory_items_conversation_id", ["conversation_id"]),
    ("ix_memory_items_owner_id", ["owner_id"]),
    ("ix_memory_items_workspace_status", ["workspace_id", "status"]),
    ("ix_memory_items_ws_status_updated", ["workspace_id", "status", "updated_at"]),
)


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _drop_indexes(table: str, names: tuple[str, ...]) -> None:
    # Index names are global in SQLite, so every one has to be released before
    # the replacement table claims it back.
    live = _indexes(table)
    for name in names:
        if name in live:
            op.drop_index(name, table_name=table)


def _create_policies(*, owned: bool) -> None:
    columns: list[sa.schema.SchemaItem] = [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("tool_name", sa.String(120), nullable=False),
        sa.Column("policy", sa.String(16), nullable=False, server_default="ask"),
        sa.Column("scope", sa.String(16), nullable=False, server_default="chat"),
        sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]
    if owned:
        columns.insert(
            5, sa.Column("owner_id", sa.String(36), nullable=False, server_default="")
        )
        columns.append(
            sa.UniqueConstraint("workspace_id", "owner_id", "tool_name", "scope")
        )
    else:
        columns.append(sa.UniqueConstraint("workspace_id", "tool_name", "scope"))
    op.create_table("tool_policies", *columns)
    op.create_index("ix_tool_policies_workspace_id", "tool_policies", ["workspace_id"])
    if owned:
        op.create_index("ix_tool_policies_owner_id", "tool_policies", ["owner_id"])


def _create_memory(*, owned: bool) -> None:
    columns: list[sa.schema.SchemaItem] = [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(36),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            sa.String(36),
            sa.ForeignKey("conversations.id"),
            nullable=True,
        ),
        sa.Column("run_id", sa.String(36), nullable=False, server_default=""),
        sa.Column("kind", sa.String(24), nullable=False, server_default="fact"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("normalized_key", sa.String(200), nullable=False),
        sa.Column("entity_names_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("message_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("importance", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("superseded_by", sa.String(36), nullable=True),
        sa.Column("embedding", sa.LargeBinary(), nullable=True),
        sa.Column("embedding_model", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]
    if owned:
        columns.insert(
            3, sa.Column("owner_id", sa.String(36), nullable=False, server_default="")
        )
        columns.append(
            sa.UniqueConstraint("workspace_id", "owner_id", "kind", "normalized_key")
        )
    else:
        columns.append(sa.UniqueConstraint("workspace_id", "kind", "normalized_key"))
    op.create_table("memory_items", *columns)
    for name, cols in _MEMORY_INDEXES:
        if name == "ix_memory_items_owner_id" and not owned:
            continue
        op.create_index(name, "memory_items", cols)


def upgrade() -> None:
    # Guarded like 0024–0039: 0003 builds `memory_items` from the live model
    # metadata via `create_all`, so a database migrated from empty already
    # carries `owner_id` and the new unique key by the time this runs, and
    # rebuilding it again would copy a table into itself.
    if "owner_id" not in _columns("memory_items"):
        _drop_indexes("memory_items", tuple(name for name, _ in _MEMORY_INDEXES))
        op.rename_table("memory_items", "memory_items_pre_owner")
        _create_memory(owned=True)
        # '' stated rather than left to the server default, so the decision not
        # to attribute is visible in the migration instead of implied by DDL.
        op.execute(
            sa.text(
                f"INSERT INTO memory_items ({_MEMORY_COLUMNS}, owner_id) "
                f"SELECT {_MEMORY_COLUMNS}, '' FROM memory_items_pre_owner"
            )
        )
        op.drop_table("memory_items_pre_owner")

    if "owner_id" not in _columns("tool_policies"):
        _drop_indexes("tool_policies", ("ix_tool_policies_workspace_id",))
        op.rename_table("tool_policies", "tool_policies_pre_owner")
        _create_policies(owned=True)
        op.execute(
            sa.text(
                f"INSERT INTO tool_policies ({_POLICY_COLUMNS}, owner_id) "
                f"SELECT {_POLICY_COLUMNS}, created_by FROM tool_policies_pre_owner"
            )
        )
        op.drop_table("tool_policies_pre_owner")


def downgrade() -> None:
    # Collapsing owners into one key means choosing which row survives, and both
    # answers here are "the shared one". For memory that is every row the
    # migration created, so nothing is lost. For policies it discards the
    # personal grants — which fails closed, in the same spirit as 0020 keeping
    # the chat row: a dropped grant asks again, a wrongly kept one does not.
    if "owner_id" in _columns("memory_items"):
        _drop_indexes("memory_items", tuple(name for name, _ in _MEMORY_INDEXES))
        op.rename_table("memory_items", "memory_items_owned")
        _create_memory(owned=False)
        op.execute(
            sa.text(
                f"INSERT INTO memory_items ({_MEMORY_COLUMNS}) "
                f"SELECT {_MEMORY_COLUMNS} FROM memory_items_owned WHERE owner_id = ''"
            )
        )
        op.drop_table("memory_items_owned")

    if "owner_id" in _columns("tool_policies"):
        _drop_indexes(
            "tool_policies",
            ("ix_tool_policies_workspace_id", "ix_tool_policies_owner_id"),
        )
        op.rename_table("tool_policies", "tool_policies_owned")
        _create_policies(owned=False)
        op.execute(
            sa.text(
                f"INSERT INTO tool_policies ({_POLICY_COLUMNS}) "
                f"SELECT {_POLICY_COLUMNS} FROM tool_policies_owned WHERE owner_id = ''"
            )
        )
        op.drop_table("tool_policies_owned")
