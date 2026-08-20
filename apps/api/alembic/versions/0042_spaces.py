"""Spaces: group threads under standing instructions, knowledge and memory.

A space is a Claude-Projects-shaped grouping inside a workspace: many rail
threads, a block of custom instructions appended to each of their turns,
knowledge files retrieved only there, and a memory shelf of its own. Three
tables gain a `space_id` — `conversations`, `sources`, `memory_items` — plus
the new `spaces` table itself.

**The backfill is `''` everywhere, and that is the status quo restated rather
than a decision about existing data.** Every scope predicate this feature adds
is of the form `space_id IN ('', S)` with `S` defaulting to `''`, which
collapses to `space_id = ''` — so a database where every row holds `''`
behaves byte-for-byte as it did yesterday: every source retrieves everywhere,
every memory recalls everywhere, no thread carries a chip. There is nothing to
attribute and no direction to argue, unlike 0040, because spaces did not exist
before this revision; no row *could* belong to one.

`space_id` is a sentinel and never NULL, for 0040's exact reason: on
`memory_items` it joins the unique key, and both engines treat NULLs as
distinct inside a unique index, so a nullable column would stop constraining
the workspace-global rows — two live rows on one claim key, supersession dead,
and the stale-served rate back where evaluate_memory.py found it before claim
keys existed. On `conversations` and `sources` it does not join a key, but one
shape per axis is the rule this schema keeps (`owner_id`, `folder_id`,
`parent_id` all spell "unset" the same way).

`memory_items` is rebuilt rather than altered because its unique constraint
grows by the new column and the old constraint was declared inline, with no
portable name to drop — the same wall 0020 and 0040 hit, resolved the same
rename/create/copy/drop way. The copy moves the embedding blobs, so on a large
corpus this migration is not instant. `conversations` and `sources` change no
constraint and take a plain ADD COLUMN.

Revision ID: 0042_spaces
Revises: 0041_organizations
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0042_spaces"
down_revision = "0041_organizations"
branch_labels = None
depends_on = None

_MEMORY_COLUMNS = (
    "id, workspace_id, conversation_id, owner_id, run_id, kind, content, "
    "normalized_key, entity_names_json, message_ids_json, importance, status, "
    "superseded_by, embedding, embedding_model, created_at, updated_at"
)
_MEMORY_INDEXES = (
    ("ix_memory_items_workspace_id", ["workspace_id"]),
    ("ix_memory_items_conversation_id", ["conversation_id"]),
    ("ix_memory_items_owner_id", ["owner_id"]),
    ("ix_memory_items_space_id", ["space_id"]),
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


def _create_memory(*, spaced: bool) -> None:
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
        sa.Column("owner_id", sa.String(36), nullable=False, server_default=""),
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
    if spaced:
        columns.insert(
            4, sa.Column("space_id", sa.String(36), nullable=False, server_default="")
        )
        columns.append(
            sa.UniqueConstraint(
                "workspace_id", "owner_id", "space_id", "kind", "normalized_key"
            )
        )
    else:
        columns.append(
            sa.UniqueConstraint("workspace_id", "owner_id", "kind", "normalized_key")
        )
    op.create_table("memory_items", *columns)
    for name, cols in _MEMORY_INDEXES:
        if name == "ix_memory_items_space_id" and not spaced:
            continue
        op.create_index(name, "memory_items", cols)


def _add_space_column(table: str) -> None:
    # Table-existence first: the migration-replay tests build partial databases
    # that stop before some tables exist, and inspecting the columns of an
    # absent table raises rather than answering "none".
    if not sa.inspect(op.get_bind()).has_table(table):
        return
    if "space_id" not in _columns(table):
        op.add_column(
            table,
            sa.Column("space_id", sa.String(36), nullable=False, server_default=""),
        )
    index = f"ix_{table}_space_id"
    if index not in _indexes(table):
        op.create_index(index, table, ["space_id"])


def upgrade() -> None:
    # Guarded like 0024–0041: a database migrated from empty got everything
    # here from `create_all` in 0001/0003 already, and rebuilding or re-adding
    # would fail or copy a table into itself.
    if not sa.inspect(op.get_bind()).has_table("spaces"):
        op.create_table(
            "spaces",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(120), nullable=False),
            sa.Column("instructions", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(36), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_spaces_workspace_id", "spaces", ["workspace_id"])

    _add_space_column("conversations")
    _add_space_column("sources")

    if sa.inspect(op.get_bind()).has_table("memory_items") and "space_id" not in _columns(
        "memory_items"
    ):
        _drop_indexes("memory_items", tuple(name for name, _ in _MEMORY_INDEXES))
        op.rename_table("memory_items", "memory_items_pre_space")
        _create_memory(spaced=True)
        # '' stated rather than left to the server default, so the "everything
        # existing is workspace-global" decision is visible here, not implied.
        op.execute(
            sa.text(
                f"INSERT INTO memory_items ({_MEMORY_COLUMNS}, space_id) "
                f"SELECT {_MEMORY_COLUMNS}, '' FROM memory_items_pre_space"
            )
        )
        op.drop_table("memory_items_pre_space")


def downgrade() -> None:
    # Dropping the axis keeps only the workspace-global rows, the same
    # fail-closed direction as 0040's downgrade: a space memory that vanishes
    # is re-learnable; a space memory promoted to global is a leak.
    if sa.inspect(op.get_bind()).has_table("memory_items") and "space_id" in _columns(
        "memory_items"
    ):
        _drop_indexes("memory_items", tuple(name for name, _ in _MEMORY_INDEXES))
        op.rename_table("memory_items", "memory_items_spaced")
        _create_memory(spaced=False)
        op.execute(
            sa.text(
                f"INSERT INTO memory_items ({_MEMORY_COLUMNS}) "
                f"SELECT {_MEMORY_COLUMNS} FROM memory_items_spaced WHERE space_id = ''"
            )
        )
        op.drop_table("memory_items_spaced")

    for table in ("sources", "conversations"):
        if sa.inspect(op.get_bind()).has_table(table) and "space_id" in _columns(table):
            _drop_indexes(table, (f"ix_{table}_space_id",))
            op.drop_column(table, "space_id")

    if sa.inspect(op.get_bind()).has_table("spaces"):
        _drop_indexes("spaces", ("ix_spaces_workspace_id",))
        op.drop_table("spaces")
