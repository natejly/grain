"""Hybrid retrieval: BM25 over a portable term index, a dense arm, RRF over both.

Three properties are worth more than the ranking assertions and are tested first:
a chunk is retrievable whether or not anything indexed it, whether or not the
embedding provider answered, and a citation always quotes the author rather than
the situating blurb we generated.

The dense arm is exercised with a deterministic stand-in for the embedding model
(`_fake_vector`) — same trick, and same reasoning, as test_memory_depth.py: no
network, no key, but texts that share tokens are genuinely closer.
"""
from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import DEV_SEED_USER_ID
from app.config import get_settings
from app.database import SessionLocal
from app.models import Chunk, ChunkTerm, Source, Workspace
from app.services import retrieval as retrieval_service
from app.services.embeddings import pack_vector
from app.services.retrieval import (
    bm25_ranking,
    embed_chunks,
    index_chunks,
    reciprocal_rank_fusion,
    search_evidence,
    tokenize,
)
from tests.embedding_doubles import as_batch

# Wide enough that two unrelated sentences do not collide into a high cosine:
# with 16 buckets the hash stand-in scored everything against everything.
EMBED_DIM = 64


def _fake_vector(text: str, dim: int = EMBED_DIM) -> bytes:
    values = [0.0] * dim
    for token in tokenize(text):
        bucket = int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % dim
        values[bucket] += 1.0
    norm = sum(value * value for value in values) ** 0.5
    if norm:
        values = [value / norm for value in values]
    return pack_vector(values)


@pytest.fixture
def workspace(client) -> str:
    """A fresh workspace, so one test's corpus cannot rank in another's."""
    db = SessionLocal()
    try:
        row = Workspace(name="retrieval-hybrid")
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _seed(
    db: Session, workspace_id: str, passages: list[str], *, filename: str = "doc.md"
) -> list[Chunk]:
    source = Source(
        workspace_id=workspace_id,
        created_by=DEV_SEED_USER_ID,
        filename=filename,
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
    db.commit()
    return chunks


def test_a_chunk_nobody_indexed_is_still_retrievable(workspace):
    """The term index is derived data, so retrieval reconciles it rather than
    depending on every writer having remembered to build it."""
    db = SessionLocal()
    try:
        _seed(db, workspace, ["Project Juniper uses a violet deployment ring."])
        assert db.scalar(
            select(ChunkTerm).where(ChunkTerm.workspace_id == workspace)
        ) is None, "seeding wrote postings; the test would prove nothing"

        results = search_evidence(db, workspace_id=workspace, query="violet deployment ring")

        assert results and "violet" in results[0].excerpt
        assert db.scalar(select(ChunkTerm).where(ChunkTerm.workspace_id == workspace)) is not None
    finally:
        db.close()


def test_retrieval_survives_an_embedding_provider_that_fails(workspace, monkeypatch):
    """A dead provider degrades the dense arm to nothing, not the search."""
    db = SessionLocal()
    try:
        chunks = _seed(db, workspace, ["Refunds are issued within five working days."])

        def _boom(texts, settings=None):
            raise RuntimeError("provider is down")

        monkeypatch.setattr(retrieval_service, "embed_batch", as_batch(_boom))

        # Ingest-side: the chunk is written and indexed even though embedding failed.
        assert embed_chunks(db, chunks) == 0
        assert chunks[0].embedding is None
        # Read-side: the query embedding fails the same way and search still answers.
        results = search_evidence(db, workspace_id=workspace, query="refunds working days")
        assert results and "Refunds" in results[0].excerpt
    finally:
        db.close()


def test_the_context_prefix_is_indexed_and_embedded_but_never_cited(workspace, monkeypatch):
    """Contextual Retrieval must not put our words in the user's mouth."""
    db = SessionLocal()
    try:
        chunks = _seed(db, workspace, ["It closes on the fifteenth."])
        chunks[0].context_prefix = "This excerpt is from the Northstar launch runbook."
        db.commit()
        index_chunks(db, chunks)
        db.commit()

        # The blurb's terms are searchable...
        results = search_evidence(db, workspace_id=workspace, query="Northstar launch runbook")
        assert results, "the situating blurb was not indexed"
        # ...and absent from the excerpt, which is the author's text alone.
        assert results[0].excerpt == "It closes on the fifteenth."
        assert "Northstar" not in results[0].excerpt

        monkeypatch.setattr(
            retrieval_service,
            "embed_batch",
            as_batch(lambda texts, settings=None: [_fake_vector(text) for text in texts]),
        )
        embed_chunks(db, chunks)
        # The vector covers prefix + content: embedding the content alone would
        # leave the dense arm blind to exactly the context the blurb restored.
        assert chunks[0].embedding == _fake_vector(
            "This excerpt is from the Northstar launch runbook.\n\nIt closes on the fifteenth."
        )
    finally:
        db.close()


def test_bm25_prefers_the_rare_term_over_the_common_one(workspace):
    """The whole point of IDF: "quarterly" appears everywhere and settles nothing,
    "kestrel" appears once and decides the answer. The pre-BM25 scorer weighted
    them identically."""
    db = SessionLocal()
    try:
        common = [f"The quarterly review for team {name} is scheduled." for name in "abcdefgh"]
        rare = "The quarterly kestrel migration audit is scheduled."
        chunks = _seed(db, workspace, [*common, rare])
        index_chunks(db, chunks)
        db.commit()

        ranked = bm25_ranking(db, workspace_id=workspace, query="quarterly kestrel")
        assert ranked[0][0] == chunks[-1].id
        # And it is not a near-tie: the rare term carries almost all the weight.
        assert ranked[0][1] > 2 * ranked[1][1]
    finally:
        db.close()


def test_a_long_prompt_keeps_its_rarest_term_not_its_longest(workspace):
    """`search_evidence` runs on the raw user prompt, which routinely carries more
    content words than one query is allowed to look up. Which ones it keeps decides
    everything, and long is not rare: a prompt carrying "infrastructure",
    "documentation" and "authentication" alongside "kestrel" used to keep the three
    boilerplate words and drop the proper noun — so BM25 never saw the term its IDF
    would have weighted highest, and ranked the answer outside the top five while
    the uncapped scorer it replaced ranked it first."""
    boiler = (
        "infrastructure documentation configuration provisioning orchestration "
        "authentication authorization observability instrumentation deployment "
        "environment repository"
    )
    db = SessionLocal()
    try:
        chunks = _seed(
            db,
            workspace,
            [*(f"{boiler} sparrow{n}" for n in range(8)), f"{boiler} kestrel"],
        )
        index_chunks(db, chunks)
        db.commit()
        prompt = (
            f"please review the {boiler} and tell me about kestrel"
        )
        assert len(tokenize(prompt)) > retrieval_service.MAX_QUERY_TERMS, (
            "the prompt no longer exceeds the cap, so this proves nothing"
        )

        ranked = bm25_ranking(db, workspace_id=workspace, query=prompt)
        assert ranked[0][0] == chunks[-1].id
        # And not by a hair: nothing else in the corpus contains the rare term.
        assert ranked[0][1] > 2 * ranked[1][1]

        results = search_evidence(db, workspace_id=workspace, query=prompt)
        assert results[0].chunk_id == chunks[-1].id
        # The scorer this replaced had no cap and got this right; BM25 must not be
        # a regression against its own baseline.
        legacy = retrieval_service.legacy_lexical_ranking(
            db, workspace_id=workspace, query=prompt
        )
        assert legacy[0][0] == chunks[-1].id
    finally:
        db.close()


def test_a_query_term_nothing_contains_does_not_squander_a_slot(workspace):
    """The rarest term in a query is often one the corpus has never seen. It
    contributes no postings and no score, so it must not push out a term that
    would have."""
    db = SessionLocal()
    try:
        boiler = (
            "infrastructure documentation configuration provisioning orchestration "
            "authentication authorization observability instrumentation deployment "
            "environment repository"
        )
        chunks = _seed(
            db,
            workspace,
            [*(f"{boiler} sparrow{n}" for n in range(8)), f"{boiler} kestrel"],
        )
        index_chunks(db, chunks)
        db.commit()
        absent = " ".join(f"zzunseen{n}" for n in range(20))
        prompt = f"please review the {boiler} {absent} and tell me about kestrel"

        ranked = bm25_ranking(db, workspace_id=workspace, query=prompt)
        assert ranked and ranked[0][0] == chunks[-1].id
    finally:
        db.close()


def test_the_dense_arm_finds_what_no_shared_word_could(workspace, monkeypatch):
    """A paraphrase with zero term overlap is the failure the dense arm exists for."""
    db = SessionLocal()
    try:
        chunks = _seed(
            db,
            workspace,
            [
                "Duplicate settlement entries are resolved by raising a correction ticket.",
                "Office snacks are restocked on Tuesday mornings.",
            ],
        )
        index_chunks(db, chunks)
        db.commit()

        query = "correction ticket settlement"
        # The stand-in embeds by shared tokens, so the query vector is built to
        # match the first chunk and nothing else.
        monkeypatch.setattr(
            retrieval_service,
            "embed_batch",
            as_batch(lambda texts, settings=None: [_fake_vector(text) for text in texts]),
        )
        embed_chunks(db, chunks)
        db.commit()

        settings = get_settings().model_copy(update={"retrieval_hybrid": True})
        dense = retrieval_service.dense_ranking(
            db, workspace_id=workspace, query=query, settings=settings
        )
        assert dense and dense[0][0] == chunks[0].id

        results = search_evidence(
            db, workspace_id=workspace, query=query, settings=settings
        )
        assert results[0].chunk_id == chunks[0].id
    finally:
        db.close()


def test_switching_the_dense_arm_off_leaves_the_lexical_ranking_untouched(workspace, monkeypatch):
    """RRF over one arm reproduces that arm's order, which is what makes the
    ablations honest: turning a stage off removes what it added and nothing else."""
    db = SessionLocal()
    try:
        chunks = _seed(
            db,
            workspace,
            [
                "The incident review covers the paging escalation policy.",
                "The escalation policy names a secondary responder.",
                "Snacks are restocked weekly.",
            ],
        )
        index_chunks(db, chunks)
        db.commit()
        monkeypatch.setattr(
            retrieval_service,
            "embed_batch",
            as_batch(lambda texts, settings=None: [_fake_vector(text) for text in texts]),
        )
        embed_chunks(db, chunks)
        db.commit()

        settings = get_settings()
        lexical_only = settings.model_copy(update={"retrieval_hybrid": False})
        ranked = bm25_ranking(
            db, workspace_id=workspace, query="escalation policy", settings=lexical_only
        )
        results = search_evidence(
            db, workspace_id=workspace, query="escalation policy", settings=lexical_only
        )
        assert [item.chunk_id for item in results] == [chunk_id for chunk_id, _ in ranked]
    finally:
        db.close()


def test_postings_do_not_leak_across_workspaces(workspace):
    """Every query is workspace-scoped, and the term index is one more table that
    has to honour that — including the lazy reconciliation, which must not index
    (or return) a neighbour's chunks just because it was the one searching."""
    db = SessionLocal()
    try:
        neighbour = Workspace(name="retrieval-hybrid-neighbour")
        db.add(neighbour)
        db.commit()
        _seed(db, workspace, ["Kestrel migration notes for the audit."])
        _seed(
            db,
            neighbour.id,
            ["Kestrel migration notes for the audit."],
            filename="theirs.md",
        )

        mine = search_evidence(db, workspace_id=workspace, query="kestrel migration audit")
        assert [item.filename for item in mine] == ["doc.md"]

        postings = db.scalars(
            select(ChunkTerm.workspace_id).where(ChunkTerm.workspace_id == neighbour.id)
        ).all()
        assert postings == [], "a foreign workspace's chunks were indexed by our search"
    finally:
        db.close()


def test_deleting_a_source_takes_its_postings_with_it(workspace, client):
    """Postings reference chunks. On Postgres an orphan makes the delete fail
    outright; on SQLite it is a leak that quietly grows the index forever."""
    db = SessionLocal()
    try:
        chunks = _seed(db, workspace, ["Kestrel migration audit notes."])
        index_chunks(db, chunks)
        db.commit()
        source_id = chunks[0].source_id
        assert db.scalars(
            select(ChunkTerm.id).where(ChunkTerm.chunk_id == chunks[0].id)
        ).all()

        from app.services.retrieval import clear_source_postings

        clear_source_postings(db, source_id)
        db.commit()
        remaining = db.scalars(
            select(ChunkTerm.id).where(ChunkTerm.chunk_id == chunks[0].id)
        ).all()
        assert remaining == []
    finally:
        db.close()


def test_a_query_with_nothing_but_stopwords_retrieves_nothing(workspace, monkeypatch):
    """The dense arm will rank a corpus against any string at all. A prompt with
    no content words is not a question, and citing five passages at it is worse
    than citing none."""
    db = SessionLocal()
    try:
        chunks = _seed(db, workspace, ["Refunds are issued within five working days."])
        index_chunks(db, chunks)
        monkeypatch.setattr(
            retrieval_service,
            "embed_batch",
            as_batch(lambda texts, settings=None: [_fake_vector(text) for text in texts]),
        )
        embed_chunks(db, chunks)
        db.commit()

        assert search_evidence(db, workspace_id=workspace, query="and the what") == []
        assert search_evidence(db, workspace_id=workspace, query="   ") == []
        assert search_evidence(db, workspace_id=workspace, query="refunds")
    finally:
        db.close()


def test_the_dense_arm_does_not_cite_a_corpus_that_has_no_answer(workspace, monkeypatch):
    """Cosine is almost never zero, so an ungated dense arm ranks everything and
    "we have nothing on that" stops being reachable. The floor keeps it reachable."""
    db = SessionLocal()
    try:
        chunks = _seed(db, workspace, ["Office snacks are restocked on Tuesday mornings."])
        index_chunks(db, chunks)
        monkeypatch.setattr(
            retrieval_service,
            "embed_batch",
            as_batch(lambda texts, settings=None: [_fake_vector(text) for text in texts]),
        )
        embed_chunks(db, chunks)
        db.commit()

        settings = get_settings()
        unrelated = "kestrel migration audit"
        assert search_evidence(db, workspace_id=workspace, query=unrelated) == []
        # Drop the floor and the same query cites the same unrelated passage,
        # which is the behaviour the floor exists to prevent.
        ungated = settings.model_copy(update={"retrieval_dense_floor": 0.0})
        assert search_evidence(db, workspace_id=workspace, query=unrelated, settings=ungated)
    finally:
        db.close()


def test_rrf_is_rank_only():
    """A cosine of 0.9 and a BM25 score of 42 are not comparable numbers; RRF
    never looks at either, which is why it can fuse them at all."""
    lexical = [("a", 42.0), ("b", 41.0)]
    dense = [("b", 0.91), ("c", 0.90)]
    fused = dict(reciprocal_rank_fusion([lexical, dense], k=60, depth=50))

    # b is second in both arms and beats a, which is first in one and absent in
    # the other — 1/62 + 1/61 > 1/61.
    assert fused["b"] > fused["a"] > fused["c"]
    # Scaling either arm's scores changes nothing at all.
    scaled = dict(
        reciprocal_rank_fusion(
            [[(item, score * 1000) for item, score in lexical], dense], k=60, depth=50
        )
    )
    assert scaled == fused


def test_a_failing_blurb_call_does_not_take_the_index_and_vectors_with_it(
    workspace, tmp_path, monkeypatch
):
    """Contextual Retrieval is the optional stage; the term index and the vectors
    are not. `situate_chunk` swallows every failure it can see, but not all of
    them — and running first, anything that escapes it used to skip both stages
    below. The index would be rebuilt on the next search; nothing on the read path
    rebuilds a vector, so an optional stage would have silently emptied the dense
    arm until someone ran the backfill."""
    from app.services import ingestion as ingestion_service
    from app.services.ingestion import ingest_source

    def _boom(*args, **kwargs):
        raise RuntimeError("the blurb model is down")

    monkeypatch.setattr(ingestion_service, "situate_chunk", _boom)
    monkeypatch.setattr(
        retrieval_service,
        "embed_batch",
        as_batch(lambda texts, settings=None: [_fake_vector(text) for text in texts]),
    )
    contextual = get_settings().model_copy(update={"retrieval_contextual": True})
    monkeypatch.setattr(ingestion_service, "get_settings", lambda: contextual)

    path = tmp_path / "runbook.md"
    path.write_text("The kestrel migration audit is owned by platform.", encoding="utf-8")
    db = SessionLocal()
    try:
        source = Source(
            workspace_id=workspace,
            created_by=DEV_SEED_USER_ID,
            filename="runbook.md",
            media_type="text/markdown",
            object_key=str(path),
            byte_size=path.stat().st_size,
            status="queued",
        )
        db.add(source)
        db.commit()
        source_id = source.id
    finally:
        db.close()

    ingest_source(source_id, DEV_SEED_USER_ID)

    db = SessionLocal()
    try:
        source = db.get(Source, source_id)
        assert source.status == "ready"
        chunks = db.scalars(select(Chunk).where(Chunk.source_id == source_id)).all()
        assert chunks
        # No blurb, but both retrieval artefacts are present anyway.
        assert all(chunk.context_prefix == "" for chunk in chunks)
        assert all(chunk.lexical_length is not None for chunk in chunks)
        assert all(chunk.embedding is not None for chunk in chunks)
        assert search_evidence(db, workspace_id=workspace, query="kestrel migration audit")
    finally:
        db.close()


def test_reingesting_a_source_does_not_leave_stale_postings(workspace, tmp_path):
    """Postings hang off chunk ids, and re-ingest replaces the chunks."""
    from app.services.ingestion import ingest_source

    path = tmp_path / "notes.md"
    path.write_text("Kestrel migration audit notes.", encoding="utf-8")
    db = SessionLocal()
    try:
        source = Source(
            workspace_id=workspace,
            created_by=DEV_SEED_USER_ID,
            filename="notes.md",
            media_type="text/markdown",
            object_key=str(path),
            byte_size=path.stat().st_size,
            status="queued",
        )
        db.add(source)
        db.commit()
        source_id = source.id
    finally:
        db.close()

    ingest_source(source_id, DEV_SEED_USER_ID)
    path.write_text("Peregrine deployment ring notes.", encoding="utf-8")
    ingest_source(source_id, DEV_SEED_USER_ID)

    db = SessionLocal()
    try:
        terms = set(
            db.scalars(select(ChunkTerm.term).where(ChunkTerm.workspace_id == workspace)).all()
        )
        assert "peregrine" in terms
        assert "kestrel" not in terms, "postings for a replaced chunk survived re-ingest"
        assert search_evidence(db, workspace_id=workspace, query="kestrel audit") == []
        assert search_evidence(db, workspace_id=workspace, query="peregrine deployment")
    finally:
        db.close()
