"""0068 against a real pre-generation corpus.

The migration's promise is that it changes no retrieval result on the day it
runs: it describes the corpus that already exists, copies the vectors that
already answered the old reader's filter, and leaves behind exactly the ones that
filter already excluded. That is a claim about data, so it is tested against
data — a database built at 0067, upgraded out of process the way a deploy does
it, and inspected afterwards.

Run out of process because `alembic/env.py` overrides `sqlalchemy.url` from the
settings, so the environment variable is the only way to point it at a temporary
database rather than the suite's own.
"""
from __future__ import annotations

import os
import sqlite3
import struct
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]

SMALL = "text-embedding-3-small"
LARGE = "text-embedding-3-large"
DIMENSIONS = 1536


def _vector(seed: float, dim: int = DIMENSIONS) -> bytes:
    """A float32 little-endian blob, the only shape that existed before 0068."""
    return struct.pack(f"<{dim}f", *[seed] * dim)


def _alembic(path: Path, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=API_ROOT,
        env={
            **os.environ,
            "DATABASE_URL": f"sqlite:///{path}",
            "APP_ENV": "test",
            "PYTHONPATH": str(API_ROOT),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr[-3000:]


@pytest.fixture
def legacy_db(tmp_path: Path) -> Path:
    """A corpus at 0067: six chunks and a memory on one model, two on another.

    The minority model is the point of the fixture. Before generations the reader
    matched `embedding_model` against the running configuration, so those two
    chunks were already invisible to the dense arm; the migration must leave them
    that way rather than sweep them into a contract they do not belong to.
    """
    path = tmp_path / "legacy.db"
    _alembic(path, "upgrade", "0067_safe_mode")

    db = sqlite3.connect(path)
    try:
        org, workspace = str(uuid.uuid4()), str(uuid.uuid4())
        user, source = str(uuid.uuid4()), str(uuid.uuid4())
        db.execute(
            "INSERT INTO organizations (id, name, created_at, updated_at) "
            "VALUES (?, 'org', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (org,),
        )
        db.execute(
            "INSERT INTO workspaces (id, name, organization_id, created_at) "
            "VALUES (?, 'legacy', ?, CURRENT_TIMESTAMP)",
            (workspace, org),
        )
        db.execute(
            "INSERT INTO users (id, email, name, status, failed_logins, created_at,"
            " updated_at) VALUES (?, 'a@b.c', 'A', 'active', 0, CURRENT_TIMESTAMP,"
            " CURRENT_TIMESTAMP)",
            (user,),
        )
        db.execute(
            "INSERT INTO sources (id, workspace_id, created_by, filename, media_type,"
            " object_key, byte_size, status, error, chunk_count, created_at,"
            " updated_at) VALUES (?, ?, ?, 'f.md', 'text/markdown', 'k', 10, 'ready',"
            " '', 8, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (source, workspace, user),
        )
        for index in range(8):
            db.execute(
                "INSERT INTO chunks (id, workspace_id, source_id, ordinal, content,"
                " char_start, char_end, token_count, embedding, embedding_model,"
                " context_prefix, created_at) VALUES (?, ?, ?, ?, ?, 0, 5, 2, ?, ?,"
                " '', CURRENT_TIMESTAMP)",
                (
                    f"chunk-{index}",
                    workspace,
                    source,
                    index,
                    f"chunk {index}",
                    _vector(index / 10),
                    SMALL if index < 6 else LARGE,
                ),
            )
        db.execute(
            "INSERT INTO memory_items (id, workspace_id, run_id, kind, content,"
            " normalized_key, entity_names_json, message_ids_json, importance,"
            " status, embedding, embedding_model, created_at, updated_at)"
            " VALUES ('mem-1', ?, '', 'fact', 'a fact', 'k1', '[]', '[]', 1,"
            " 'active', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (workspace, _vector(0.5), SMALL),
        )
        db.commit()
    finally:
        db.close()
    return path


def _query(path: Path, sql: str, *params: object) -> list[tuple]:
    db = sqlite3.connect(path)
    try:
        return list(db.execute(sql, params))
    finally:
        db.close()


def test_0068_describes_the_corpus_that_already_exists(legacy_db: Path):
    """One generation, on the dominant model, at the width the data actually is.

    Both halves are read from the data rather than from configuration: the model
    because the question is what is in the table, and the width because a model
    name stopped determining it the moment Matryoshka truncation became available.
    """
    _alembic(legacy_db, "upgrade", "0068_embedding_generations")

    rows = _query(
        legacy_db,
        "SELECT model, dimensions, storage_dtype, normalization, dense_floor, status"
        " FROM embedding_generations",
    )
    assert len(rows) == 1
    model, dimensions, dtype, normalization, floor, status = rows[0]
    assert model == SMALL, "the dominant model is the one 6 of 8 chunks used"
    assert dimensions == DIMENSIONS, "width comes from a stored vector, not a table"
    assert (dtype, normalization) == ("float32", "l2")
    assert floor == pytest.approx(0.30), "the pre-existing floor, so behaviour holds"
    assert status == "active"


def test_0068_copies_only_the_vectors_the_old_reader_could_already_see(legacy_db: Path):
    """The two minority-model chunks stay behind, because they were already out.

    Sweeping them in would be the one thing the old `embedding_model ==` filter
    was protecting against — comparing vectors from two models — dressed up as a
    migration.
    """
    _alembic(legacy_db, "upgrade", "0068_embedding_generations")

    copied = {
        row[0]
        for row in _query(legacy_db, "SELECT owner_id FROM embedding_vectors")
    }
    assert copied == {f"chunk-{index}" for index in range(6)} | {"mem-1"}

    kinds = dict(
        _query(
            legacy_db,
            "SELECT owner_kind, COUNT(*) FROM embedding_vectors GROUP BY owner_kind",
        )
    )
    assert kinds == {"chunk": 6, "memory_item": 1}, "memory travels with documents"


def test_0068_preserves_the_vector_bytes_exactly(legacy_db: Path):
    """A move that rounds is not a move."""
    _alembic(legacy_db, "upgrade", "0068_embedding_generations")

    stored = _query(
        legacy_db,
        "SELECT vector FROM embedding_vectors WHERE owner_id = 'chunk-3'",
    )[0][0]
    assert stored == _vector(0.3)


def test_0068_leaves_the_source_columns_intact(legacy_db: Path):
    """What makes the downgrade real rather than nominal.

    The `embedding` columns are not emptied, so dropping the new tables loses the
    generation history and nothing else — every vector is still sitting in the
    column it was copied from.
    """
    _alembic(legacy_db, "upgrade", "0068_embedding_generations")
    assert _query(
        legacy_db, "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"
    ) == [(8,)]

    _alembic(legacy_db, "downgrade", "0067_safe_mode")
    assert _query(
        legacy_db, "SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL"
    ) == [(8,)]
    assert _query(
        legacy_db,
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        " AND name IN ('embedding_vectors', 'embedding_generations')",
    ) == [(0,)]


def test_0068_replays_to_the_same_state(legacy_db: Path):
    """Upgrade, downgrade, upgrade — one generation, not two.

    The backfill is skipped whenever a generation already exists, so a database
    that reached this state by another route cannot mint a second contract and
    split the corpus between them.
    """
    _alembic(legacy_db, "upgrade", "0068_embedding_generations")
    _alembic(legacy_db, "downgrade", "0067_safe_mode")
    _alembic(legacy_db, "upgrade", "0068_embedding_generations")

    assert _query(legacy_db, "SELECT COUNT(*) FROM embedding_generations") == [(1,)]
    assert _query(legacy_db, "SELECT COUNT(*) FROM embedding_vectors") == [(7,)]


def test_0068_mints_nothing_when_no_corpus_was_ever_embedded(tmp_path: Path):
    """No vectors means no contract to describe, and inventing one would assert
    that something was written under it. The first embed creates it instead."""
    path = tmp_path / "empty.db"
    _alembic(path, "upgrade", "head")
    assert _query(path, "SELECT COUNT(*) FROM embedding_generations") == [(0,)]
