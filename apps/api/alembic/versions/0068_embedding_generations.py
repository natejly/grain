"""Embedding generations: a vector says which contract produced it, and where.

Before this, a vector's only provenance was `embedding_model`, a bare model name,
and the reader matched it against the running configuration. That left two holes.
Same-width vectors from two models compare cleanly and rank wrongly, which the
dense arm's own comment already admitted it could not catch. And editing the
configured model migrated nothing — it made every stored vector fail the filter
at once, so hybrid search silently became lexical search with no error anywhere,
because degrading to lexical is a designed behaviour rather than a fault.

Two tables fix it. `embedding_generations` records everything that changes what a
vector *is*: model, the revision the provider answered with, dimensions, storage
dtype, normalization, the version of the text formatting, and the dense floor
that width requires. Only one may be active, enforced by a partial unique index
rather than by the service that flips them, since activation is a check-then-write
that two concurrent callers would both pass.

`embedding_vectors` is where the vectors move to. This is the part that makes
generations more than a label: a vector stored in a column on the row it
describes can only ever be one generation deep, so building a new contract would
overwrite the corpus the live index is serving from. Build-beside, verify, flip,
and roll back all require the old and new vectors to exist at the same time, and
that requires a row per (owner, generation).

The `embedding` columns are left in place, populated, and unread. Dropping them
would make this migration irreversible in practice — a downgrade could restore
the schema but not the data — and they cost nothing beyond disk until a later
migration removes them deliberately.

The backfill is conservative. It creates one generation describing the corpus as
it already exists — model and width read from the data rather than assumed — and
copies across only the rows that match it. Rows embedded by some *other* model
are not copied and stay invisible to the dense arm, which is exactly what the old
`embedding_model ==` filter already did to them. So this migration changes no
retrieval result on the day it runs; it makes a distinction the reader was
already drawing into an explicit one.

Dimensions are inferred from an actual stored vector (byte length / 4, since
every pre-existing vector is float32 little-endian) rather than from the model
name, because the name does not determine the width once Matryoshka truncation is
available, and a lookup table would start lying the first time someone sets
`OPENAI_EMBEDDING_DIMENSIONS`.

Revision ID: 0068_embedding_generations
Revises: 0067_safe_mode
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0068_embedding_generations"
down_revision = "0067_safe_mode"
branch_labels = None
depends_on = None

#: {table holding vectors: the `owner_kind` its rows get in embedding_vectors}
VECTOR_TABLES = {
    "chunks": "chunk",
    "memory_items": "memory_item",
    "conversation_chunks": "conversation_chunk",
}

#: Dense floor for the width this backfill discovers. 0.30 is what the setting
#: held for 1536-dim vectors, and preserving current behaviour is the whole point
#: of the backfill; other widths get their calibrated value from the service.
LEGACY_FLOOR = 0.30

#: Rows copied per round trip. Vectors are 6KB each at 1536 dimensions, so a
#: larger batch buys little and holds tens of megabytes of blob in memory.
COPY_BATCH = 500


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    tables = _tables()

    if "embedding_generations" not in tables:
        op.create_table(
            "embedding_generations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("model", sa.String(64), nullable=False),
            sa.Column("revision", sa.String(128), nullable=False, server_default=""),
            sa.Column("dimensions", sa.Integer, nullable=False),
            sa.Column(
                "storage_dtype", sa.String(16), nullable=False, server_default="float32"
            ),
            sa.Column(
                "normalization", sa.String(16), nullable=False, server_default="l2"
            ),
            sa.Column(
                "input_format", sa.String(32), nullable=False, server_default="v1"
            ),
            sa.Column("dense_floor", sa.Float, nullable=False, server_default="0.3"),
            sa.Column(
                "status", sa.String(16), nullable=False, server_default="building"
            ),
            sa.Column("note", sa.Text, nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("activated_at", sa.DateTime, nullable=True),
            sa.Column("retired_at", sa.DateTime, nullable=True),
        )
        # One active generation, enforced by the database. See the module docstring.
        op.create_index(
            "uq_embedding_generations_active",
            "embedding_generations",
            ["status"],
            unique=True,
            sqlite_where=sa.text("status = 'active'"),
            postgresql_where=sa.text("status = 'active'"),
        )

    if "embedding_vectors" not in tables:
        op.create_table(
            "embedding_vectors",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "generation_id",
                sa.String(36),
                sa.ForeignKey("embedding_generations.id"),
                nullable=False,
            ),
            sa.Column("owner_kind", sa.String(24), nullable=False),
            sa.Column("owner_id", sa.String(36), nullable=False),
            sa.Column("workspace_id", sa.String(36), nullable=False),
            sa.Column("vector", sa.LargeBinary, nullable=False),
            sa.Column(
                "content_hash", sa.String(64), nullable=False, server_default=""
            ),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.UniqueConstraint(
                "generation_id",
                "owner_kind",
                "owner_id",
                name="uq_embedding_vectors_owner",
            ),
        )
        op.create_index(
            "ix_embedding_vectors_generation_workspace",
            "embedding_vectors",
            ["generation_id", "workspace_id", "owner_kind"],
        )
        op.create_index(
            "ix_embedding_vectors_owner", "embedding_vectors", ["owner_kind", "owner_id"]
        )
        op.create_index(
            "ix_embedding_vectors_generation_id", "embedding_vectors", ["generation_id"]
        )
        op.create_index(
            "ix_embedding_vectors_workspace_id", "embedding_vectors", ["workspace_id"]
        )

    # --- backfill: describe the corpus that already exists ------------------
    #
    # Skipped when a generation already exists, so a re-run — or a database that
    # reached this state another way — cannot mint a second one.
    if bind.execute(sa.text("SELECT COUNT(*) FROM embedding_generations")).scalar_one():
        return

    present: dict[str, int] = {}
    width: dict[str, int] = {}
    for table in VECTOR_TABLES:
        if table not in tables:
            continue
        rows = bind.execute(
            sa.text(  # noqa: S608 - table names come from the constant above
                f"SELECT embedding_model, COUNT(*) AS n, MIN(LENGTH(embedding)) AS w "
                f"FROM {table} WHERE embedding IS NOT NULL GROUP BY embedding_model"
            )
        ).all()
        for model, count, byte_length in rows:
            if not model:
                continue
            present[model] = present.get(model, 0) + int(count or 0)
            if byte_length:
                width.setdefault(model, int(byte_length) // 4)
    if not present:
        # Nothing embedded yet. No generation is minted, because there is no
        # corpus to describe and inventing one would assert a contract nothing
        # was written under. The first embed creates it.
        return

    # The dominant model among rows that actually hold a vector. Counted rather
    # than read from settings: the question is what is in the table, and a
    # configuration edited before this ran would answer a different one.
    model = max(present, key=lambda name: (present[name], name))
    dimensions = width.get(model) or 1536
    generation_id = str(uuid.uuid4())
    bind.execute(
        sa.text(
            "INSERT INTO embedding_generations "
            "(id, model, revision, dimensions, storage_dtype, normalization, "
            " input_format, dense_floor, status, note, created_at, activated_at) "
            "VALUES (:id, :model, '', :dims, 'float32', 'l2', 'v1', :floor, "
            "        'active', :note, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {
            "id": generation_id,
            "model": model,
            "dims": dimensions,
            "floor": LEGACY_FLOOR,
            "note": (
                "Backfilled by 0068 to describe the pre-generation corpus. Width "
                "inferred from stored vectors; revision unknown because nothing "
                "recorded what the provider answered."
            ),
        },
    )

    # Copy only the vectors this generation actually describes: right model,
    # right width. Anything else is left behind and stays invisible to the dense
    # arm, which is what the old model-name filter already did to it.
    #
    # `content_hash` is left empty rather than computed. The hash has to be of the
    # text that was embedded, and for chunks that is `context_prefix + content`
    # under whichever formatter was live at the time — which this migration cannot
    # know. An empty hash reads as "unknown provenance, do not claim freshness",
    # which is true, where a hash computed from today's formatter would read as a
    # match and assert something false.
    byte_length = dimensions * 4
    for table, owner_kind in VECTOR_TABLES.items():
        if table not in tables:
            continue
        offset = 0
        while True:
            rows = bind.execute(
                sa.text(  # noqa: S608 - table names come from the constant above
                    f"SELECT id, workspace_id, embedding FROM {table} "
                    f"WHERE embedding IS NOT NULL AND embedding_model = :model "
                    f"AND LENGTH(embedding) = :byte_length "
                    f"ORDER BY id LIMIT :limit OFFSET :offset"
                ),
                {
                    "model": model,
                    "byte_length": byte_length,
                    "limit": COPY_BATCH,
                    "offset": offset,
                },
            ).all()
            if not rows:
                break
            bind.execute(
                sa.text(
                    "INSERT INTO embedding_vectors "
                    "(id, generation_id, owner_kind, owner_id, workspace_id, "
                    " vector, content_hash, created_at) "
                    "VALUES (:id, :gid, :kind, :owner_id, :workspace_id, "
                    "        :vector, '', CURRENT_TIMESTAMP)"
                ),
                [
                    {
                        "id": str(uuid.uuid4()),
                        "gid": generation_id,
                        "kind": owner_kind,
                        "owner_id": row_id,
                        "workspace_id": workspace_id,
                        "vector": vector,
                    }
                    for row_id, workspace_id, vector in rows
                ],
            )
            offset += COPY_BATCH


def downgrade() -> None:
    tables = _tables()
    # The `embedding` columns were never emptied, so dropping these tables loses
    # the generation history and nothing else — every vector the upgrade copied
    # is still sitting in the column it was copied from.
    if "embedding_vectors" in tables:
        op.drop_index("ix_embedding_vectors_workspace_id", table_name="embedding_vectors")
        op.drop_index(
            "ix_embedding_vectors_generation_id", table_name="embedding_vectors"
        )
        op.drop_index("ix_embedding_vectors_owner", table_name="embedding_vectors")
        op.drop_index(
            "ix_embedding_vectors_generation_workspace", table_name="embedding_vectors"
        )
        op.drop_table("embedding_vectors")
    if "embedding_generations" in tables:
        op.drop_index(
            "uq_embedding_generations_active", table_name="embedding_generations"
        )
        op.drop_table("embedding_generations")
