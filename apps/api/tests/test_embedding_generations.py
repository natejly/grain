"""Embedding contracts: what a vector means, and how a corpus changes contract.

The behaviour under test is a safety property, not a feature. Changing how a
corpus is embedded used to be an unmonitored outage — the reader matched vectors
against the running configuration, so editing the model made every stored vector
fail the filter at once, hybrid search silently became lexical search, and
nothing errored because degrading to lexical is a *designed* behaviour. These
tests pin the three things that make the new path different: a build cannot
disturb the corpus being served, an incomplete build cannot become the live
index, and the generation it replaces can be reinstated without re-embedding.
"""
from __future__ import annotations

import hashlib

import numpy as np
import pytest
from sqlalchemy.orm import Session

from app.auth import DEV_SEED_USER_ID
from app.config import get_settings
from app.database import SessionLocal
from app.models import Chunk, EmbeddingVector, Source, Workspace
from app.services import embedding_generations as generations
from app.services import retrieval as retrieval_service
from app.services.embeddings import (
    content_fingerprint,
    pack_vector,
    truncate_vector,
    unpack_vector,
)
from app.services.retrieval import embed_chunks, index_chunks, search_evidence, tokenize
from tests.embedding_doubles import as_batch

EMBED_DIM = 64


def _fake_vector(text: str, dim: int = EMBED_DIM) -> bytes:
    values = [0.0] * dim
    for token in tokenize(text):
        values[int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % dim] += 1.0
    norm = sum(value * value for value in values) ** 0.5
    if norm:
        values = [value / norm for value in values]
    return pack_vector(values)


@pytest.fixture
def workspace(client) -> str:
    db = SessionLocal()
    try:
        row = Workspace(name="embedding-generations")
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _seed(db: Session, workspace_id: str, passages: list[str]) -> list[Chunk]:
    source = Source(
        workspace_id=workspace_id,
        created_by=DEV_SEED_USER_ID,
        filename="doc.md",
        media_type="text/markdown",
        object_key="/tmp/not-used",
        byte_size=1,
        status="ready",
        chunk_count=len(passages),
    )
    db.add(source)
    db.flush()
    chunks = []
    for ordinal, text in enumerate(passages):
        chunk = Chunk(
            workspace_id=workspace_id,
            source_id=source.id,
            ordinal=ordinal,
            content=text,
            char_start=0,
            char_end=len(text),
            token_count=len(text.split()),
        )
        db.add(chunk)
        chunks.append(chunk)
    db.flush()
    index_chunks(db, chunks)
    db.commit()
    return chunks


def _embedder(monkeypatch):
    monkeypatch.setattr(
        retrieval_service,
        "embed_batch",
        as_batch(lambda texts, settings=None: [_fake_vector(text) for text in texts]),
    )


# --------------------------------------------------------------------------- #
# Truncation: the property that makes a migration affordable.
# --------------------------------------------------------------------------- #


def test_truncation_is_a_renormalised_prefix():
    """A narrower vector is the leading dimensions, renormalised — not rescaled.

    Both halves matter. A prefix, because Matryoshka training puts the most
    information first and that is the only reason a shorter vector still ranks;
    renormalised, because dropping dimensions shortens the vector, and a cosine
    computed against an unnormalised one would depend on how much of the tail
    happened to be cut rather than on the direction that survived.
    """
    source = pack_vector([float(index) for index in range(1, 9)])
    cut = truncate_vector(source, dimensions=4, source_dtype="float32", dtype="float32")
    assert cut is not None
    values = unpack_vector(cut)

    original = np.array([1.0, 2.0, 3.0, 4.0])
    expected = original / np.linalg.norm(original)
    assert np.allclose(values, expected, atol=1e-6)
    assert pytest.approx(1.0, abs=1e-6) == float(np.linalg.norm(values))


def test_truncation_refuses_a_vector_it_cannot_cut():
    """Too short to cut is "not covered", not a silently wrong vector."""
    source = pack_vector([1.0, 2.0])
    assert truncate_vector(source, dimensions=8) is None


def test_truncation_refuses_an_all_zero_prefix():
    """Normalising it would divide by zero; storing it would index a vector that
    matches nothing at cosine 0 while claiming coverage."""
    source = pack_vector([0.0, 0.0, 3.0, 4.0])
    assert truncate_vector(source, dimensions=2) is None


def test_float16_storage_round_trips_through_the_scorer(workspace, monkeypatch):
    """A narrower dtype must survive the write/read cycle the dense arm uses.

    float16 halves storage, and on the evaluation corpus it changed no ranking at
    any k — but only because reader and writer agree on the width. A reader that
    guessed float32 would reinterpret every pair of vectors as one, which does not
    raise, it ranks.
    """
    values = [0.5, -0.25, 0.125, 0.0]
    blob = pack_vector(values, "float16")
    assert len(blob) == len(values) * 2
    assert unpack_vector(blob, "float16") == pytest.approx(values, abs=1e-3)


# --------------------------------------------------------------------------- #
# Build beside, verify, flip, roll back.
# --------------------------------------------------------------------------- #


def test_building_a_generation_does_not_disturb_the_one_being_served(
    workspace, monkeypatch
):
    """The property the whole design exists for.

    A vector used to live in a column on the row it described, which allowed
    exactly one per row — so building a new contract would have overwritten the
    corpus the live index was serving from. Here the two coexist, and retrieval
    keeps answering out of the active one while the new one fills up.
    """
    _embedder(monkeypatch)
    db = SessionLocal()
    try:
        chunks = _seed(db, workspace, ["Peregrine rollout is scheduled for June."])
        embed_chunks(db, chunks, get_settings())
        db.commit()
        active = generations.active_generation(db)
        assert active is not None

        before = search_evidence(db, workspace_id=workspace, query="peregrine rollout")
        assert before, "the fixture must retrieve something to begin with"

        # A narrower contract, built beside the live one.
        target = generations.create_generation(
            db, model=active.model, dimensions=16, storage_dtype="float16"
        )
        written = generations.materialize_by_truncation(
            db, source=active, target=target
        )
        db.commit()
        assert written == len(chunks)
        assert target.status == "building"

        after = search_evidence(db, workspace_id=workspace, query="peregrine rollout")
        assert [item.chunk_id for item in after] == [item.chunk_id for item in before]

        # Both contracts hold a vector for the same chunk, at their own widths.
        rows = {
            (row.generation_id, len(row.vector))
            for row in db.query(EmbeddingVector)
            .filter(EmbeddingVector.owner_id == chunks[0].id)
            .all()
        }
        assert rows == {(active.id, EMBED_DIM * 4), (target.id, 16 * 2)}
    finally:
        db.close()


def test_an_incomplete_generation_cannot_be_activated(workspace, monkeypatch):
    """A 90%-built generation is a 10% silent recall loss, not a partial win.

    The rows it does not cover simply drop out of the dense arm, which looks
    exactly like a corpus with nothing to say on the subject — so activation is
    refused rather than merely warned about.
    """
    _embedder(monkeypatch)
    db = SessionLocal()
    try:
        chunks = _seed(db, workspace, ["Kestrel migration audit is quarterly."])
        embed_chunks(db, chunks, get_settings())
        db.commit()
        active = generations.active_generation(db)
        assert active is not None

        empty = generations.create_generation(
            db, model=active.model, dimensions=active.dimensions
        )
        db.commit()
        report = generations.coverage(db, empty)
        assert report.pending > 0
        assert not report.complete

        with pytest.raises(ValueError, match="incomplete"):
            generations.activate(db, empty)
        assert generations.active_generation(db).id == active.id

        # `force` is the deliberate escape hatch, and it does activate.
        generations.activate(db, empty, force=True)
        db.commit()
        assert generations.active_generation(db).id == empty.id
    finally:
        db.close()


def test_rollback_reinstates_the_previous_contract_without_re_embedding(
    workspace, monkeypatch
):
    """Rollback has to be cheaper than the failure it undoes.

    The retired generation keeps every vector it wrote, so this is two status
    columns and no inference at all — which is what makes shipping a new contract
    a reversible decision rather than a one-way door.
    """
    _embedder(monkeypatch)
    db = SessionLocal()
    try:
        chunks = _seed(db, workspace, ["Osprey ledger reconciles overnight."])
        embed_chunks(db, chunks, get_settings())
        db.commit()
        original = generations.active_generation(db)
        assert original is not None
        original_id = original.id

        target = generations.create_generation(
            db, model=original.model, dimensions=16, storage_dtype="float16"
        )
        generations.materialize_by_truncation(db, source=original, target=target)
        generations.activate(db, target, force=True)
        db.commit()
        assert generations.active_generation(db).id == target.id

        restored = generations.rollback(db)
        db.commit()
        assert restored is not None and restored.id == original_id
        assert generations.active_generation(db).id == original_id

        # The vectors were never deleted, which is the whole point.
        surviving = (
            db.query(EmbeddingVector)
            .filter(EmbeddingVector.generation_id == original_id)
            .count()
        )
        assert surviving == len(chunks)
        assert search_evidence(db, workspace_id=workspace, query="osprey ledger")
    finally:
        db.close()


def test_a_configuration_change_opens_a_new_generation_rather_than_orphaning_the_corpus(
    workspace, monkeypatch
):
    """The regression this replaces: editing the model used to blind the dense arm.

    Now the write path opens a `building` generation for the new contract and
    reads carry on against the active one, so retrieval quality is unchanged until
    somebody finishes the build and flips it deliberately.
    """
    _embedder(monkeypatch)
    db = SessionLocal()
    try:
        chunks = _seed(db, workspace, ["Marlin index rebuild takes an hour."])
        settings = get_settings()
        embed_chunks(db, chunks, settings)
        db.commit()
        active = generations.active_generation(db)
        assert active is not None

        narrower = settings.model_copy(update={"openai_embedding_dimensions": 16})
        opened = generations.writable_generation(db, narrower)
        db.commit()

        assert opened.id != active.id
        assert opened.status == "building"
        # Unchanged: reads never consulted the configuration.
        assert generations.active_generation(db).id == active.id
        assert search_evidence(db, workspace_id=workspace, query="marlin index")
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Staleness: a vector that no longer describes its row.
# --------------------------------------------------------------------------- #


def test_editing_a_chunk_makes_its_vector_stale(workspace, monkeypatch):
    """Re-ingest rewrites `content` in place; the old vector must not survive it.

    Before the content hash, the reconcile predicate asked only whether *a* vector
    existed under the configured model — which it did, describing the previous
    revision of the text. The chunk stayed retrievable, kept its confident cosine,
    and pointed at words it no longer contained.
    """
    _embedder(monkeypatch)
    db = SessionLocal()
    try:
        chunks = _seed(db, workspace, ["Halcyon budget freeze begins in March."])
        settings = get_settings()
        embed_chunks(db, chunks, settings)
        db.commit()

        assert retrieval_service.chunks_needing_embedding(db, chunks, settings) == []

        chunks[0].content = "Halcyon budget freeze was cancelled entirely."
        db.commit()

        stale = retrieval_service.chunks_needing_embedding(db, chunks, settings)
        assert [chunk.id for chunk in stale] == [chunks[0].id]

        embed_chunks(db, stale, settings)
        db.commit()
        assert retrieval_service.chunks_needing_embedding(db, chunks, settings) == []

        stored = (
            db.query(EmbeddingVector)
            .filter(EmbeddingVector.owner_id == chunks[0].id)
            .one()
        )
        assert stored.content_hash == content_fingerprint(chunks[0].content)
    finally:
        db.close()


def test_deleting_a_source_drops_its_vectors_from_every_generation(
    workspace, monkeypatch
):
    """A deleted chunk must not be waiting inside a retired generation.

    Otherwise a rollback resurrects it, pointing `owner_id` at a row that no
    longer exists.
    """
    _embedder(monkeypatch)
    db = SessionLocal()
    try:
        chunks = _seed(db, workspace, ["Vireo archive is deleted on request."])
        source_id = chunks[0].source_id
        embed_chunks(db, chunks, get_settings())
        db.commit()
        active = generations.active_generation(db)
        assert active is not None

        retired = generations.create_generation(
            db, model=active.model, dimensions=16, storage_dtype="float16"
        )
        generations.materialize_by_truncation(db, source=active, target=retired)
        db.commit()
        owner_ids = [chunk.id for chunk in chunks]
        assert (
            db.query(EmbeddingVector)
            .filter(EmbeddingVector.owner_id.in_(owner_ids))
            .count()
            == 2 * len(chunks)
        )

        retrieval_service.clear_source_postings(db, source_id)
        db.commit()
        assert (
            db.query(EmbeddingVector)
            .filter(EmbeddingVector.owner_id.in_(owner_ids))
            .count()
            == 0
        )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# The floor travels with the contract.
# --------------------------------------------------------------------------- #


def test_a_narrower_generation_gets_its_own_calibrated_floor():
    """Cosine between unrelated vectors rises as dimensionality falls.

    Measured on `evals/corpus.json`, a 0.30 floor admits 11.5% of query-document
    pairs at 1536 dimensions and 24.4% at 256 — nearly tripling the junk reaching
    fusion while rescuing no additional true answer. So the floor cannot be one
    global number, and a generation carries the one its width requires.
    """
    assert generations.calibrated_floor(1536, 0.3) == 0.30
    assert generations.calibrated_floor(256, 0.3) > generations.calibrated_floor(1536, 0.3)
    # An unmeasured width gets the caller's default rather than an interpolation:
    # the shape of that curve is not something to guess at, and a wrong floor is
    # silent.
    assert generations.calibrated_floor(999, 0.42) == 0.42


def test_an_explicit_floor_still_overrides_the_generation():
    """Owning the floor must not mean confiscating it.

    An operator who sets RETRIEVAL_DENSE_FLOOR is making a deliberate statement
    about this deployment; silently ignoring it would leave a documented knob
    connected to nothing.
    """
    settings = get_settings()
    generation = generations.EmbeddingGeneration(
        model="m", dimensions=256, dense_floor=0.35
    )
    assert generations.effective_floor(generation, settings) == 0.35

    overridden = settings.model_copy(update={"retrieval_dense_floor": 0.0})
    assert generations.effective_floor(generation, overridden) == 0.0
