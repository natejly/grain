"""Follow-up chips: determinism, the read floor, scoping, and failure isolation.

Every chip this feature offers is a promise that the workspace has something to
say. These tests are about the four ways that promise could be broken: a chip
derived non-deterministically (so no eval could measure it), a floor derived
rather than read (so noise is admitted at narrow widths), a probe that forgot a
scope axis (so a chip points at a file this thread cannot retrieve), and a
suggester that can take a completed run down with it.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models import Chunk, Conversation, Source, Workspace, new_id
from app.services import embedding_generations as generations
from app.services import followups, retrieval
from app.services.embeddings import pack_vector
from tests.embedding_doubles import as_batch

EMBED_DIM = 64


def _fake_vector(text: str, dim: int = EMBED_DIM) -> bytes:
    """Deterministic stand-in for an embedding model (evaluate_memory's shape)."""
    values = [0.0] * dim
    for token in retrieval.tokenize(text):
        bucket = int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % dim
        values[bucket] += 1.0
    norm = sum(value * value for value in values) ** 0.5
    if norm:
        values = [value / norm for value in values]
    return pack_vector(values)


@pytest.fixture
def embedder(monkeypatch):
    monkeypatch.setattr(
        retrieval,
        "embed_batch",
        as_batch(lambda texts, settings=None, **contract: [
            _fake_vector(text) for text in texts
        ]),
    )


def _workspace(db) -> str:
    workspace_id = new_id()
    db.add(Workspace(id=workspace_id, name="Followups"))
    db.flush()
    return workspace_id


def _indexed_source(
    db,
    workspace_id: str,
    *,
    filename: str,
    text: str,
    space_id: str = "",
    conversation_id: str = "",
):
    source = Source(
        workspace_id=workspace_id,
        created_by="",
        filename=filename,
        media_type="text/markdown",
        object_key="/x/" + filename,
        byte_size=len(text),
        status="ready",
        space_id=space_id,
        conversation_id=conversation_id,
    )
    db.add(source)
    db.flush()
    chunk = Chunk(
        workspace_id=workspace_id,
        source_id=source.id,
        ordinal=0,
        content=text,
        char_start=0,
        char_end=len(text),
        token_count=len(text.split()),
    )
    db.add(chunk)
    db.flush()
    retrieval.index_chunks(db, [chunk])
    retrieval.embed_chunks(db, [chunk], get_settings())
    return source, chunk


RETENTION = (
    "The retention window for raw event logs is ninety days. Billing events "
    "are archived for seven years in a write-once bucket."
)
ANSWER = (
    "Logs roll up after ninety days [1].\n"
    "\n"
    "## Retention window\n"
    "\n"
    "Billing events are the exception [1].\n"
)


def _evidence(source, chunk) -> retrieval.Evidence:
    return retrieval.Evidence(
        chunk_id=chunk.id,
        source_id=source.id,
        filename=source.filename,
        ordinal=0,
        excerpt=chunk.content[:200],
        score=1.0,
    )


def test_headings_become_candidates_in_document_order_and_deduplicate():
    """Pure, so the chip text is a function of the answer and nothing else."""
    answer = "# One\n\ntext\n\n**Two:**\n\n## one\n\n## Three.\n"
    assert followups.heading_candidates(answer) == [
        "What does the workspace say about One?",
        "What does the workspace say about Two?",
        "What does the workspace say about Three?",
    ]


def test_the_chip_set_is_byte_identical_across_two_runs(embedder, monkeypatch):
    """The eval gate's premise: one corpus, one answer, one chip list."""
    monkeypatch.setenv("RETRIEVAL_DENSE_FLOOR", "0.05")
    get_settings.cache_clear()
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        source, chunk = _indexed_source(
            db, workspace_id, filename="retention.md", text=RETENTION
        )
        db.commit()
        settings = get_settings()
        first = followups.serialize(
            followups.suggest(
                db,
                workspace_id=workspace_id,
                answer=ANSWER,
                evidence=[_evidence(source, chunk)],
                settings=settings,
            )
        )
        second = followups.serialize(
            followups.suggest(
                db,
                workspace_id=workspace_id,
                answer=ANSWER,
                evidence=[_evidence(source, chunk)],
                settings=settings,
            )
        )
        assert first
        assert json.dumps(first) == json.dumps(second)
        assert len(first) <= followups.MAX_FOLLOWUPS
        assert {item["origin"] for item in first} <= {"kg", "heading"}
    finally:
        db.close()
        get_settings.cache_clear()


def test_the_floor_is_read_and_an_explicit_override_wins(embedder, monkeypatch):
    """`effective_floor`'s contract, exercised end to end.

    The same corpus and the same answer admit a chip under a permissive floor
    and admit nothing under a strict one — with no other change. A floor
    DERIVED from the width (the sqrt(d) closed form the calibration table warns
    about) would make this test's two runs identical.
    """
    monkeypatch.setenv("RETRIEVAL_DENSE_FLOOR", "0.05")
    get_settings.cache_clear()
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        source, chunk = _indexed_source(
            db, workspace_id, filename="retention.md", text=RETENTION
        )
        db.commit()
        settings = get_settings()
        generation = generations.active_generation(db)
        assert generation is not None
        assert generations.effective_floor(generation, settings) == 0.05
        permissive = followups.suggest(
            db,
            workspace_id=workspace_id,
            answer=ANSWER,
            evidence=[_evidence(source, chunk)],
            settings=settings,
        )
        assert permissive

        strict = settings.model_copy(update={"retrieval_dense_floor": 0.99})
        assert generations.effective_floor(generation, strict) == 0.99
        retrieval.query_embedding_cache.clear()
        assert (
            followups.suggest(
                db,
                workspace_id=workspace_id,
                answer=ANSWER,
                evidence=[_evidence(source, chunk)],
                settings=strict,
            )
            == []
        )
    finally:
        db.close()
        get_settings.cache_clear()


def test_a_chip_is_never_offered_for_a_passage_this_thread_cannot_retrieve(
    embedder, monkeypatch
):
    """`_live_sources`' two axes. A probe that forgot one would offer a chip
    about a file scoped to a space, or attached to somebody else's chat."""
    monkeypatch.setenv("RETRIEVAL_DENSE_FLOOR", "0.05")
    get_settings.cache_clear()
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        space_id = new_id()
        conversation = Conversation(workspace_id=workspace_id, created_by="")
        db.add(conversation)
        db.flush()
        space_source, space_chunk = _indexed_source(
            db,
            workspace_id,
            filename="scoped.md",
            text=RETENTION,
            space_id=space_id,
        )
        attached_source, attached_chunk = _indexed_source(
            db,
            workspace_id,
            filename="attached.md",
            text=RETENTION,
            conversation_id=conversation.id,
        )
        db.commit()
        settings = get_settings()
        evidence = [_evidence(space_source, space_chunk)]

        retrieval.query_embedding_cache.clear()
        in_space = followups.suggest(
            db,
            workspace_id=workspace_id,
            answer=ANSWER,
            evidence=evidence,
            space_id=space_id,
            settings=settings,
        )
        assert in_space

        retrieval.query_embedding_cache.clear()
        in_thread = followups.suggest(
            db,
            workspace_id=workspace_id,
            answer=ANSWER,
            evidence=[_evidence(attached_source, attached_chunk)],
            conversation_id=conversation.id,
            settings=settings,
        )
        assert in_thread

        # The general thread sees neither axis, so neither passage is reachable
        # and no chip may be offered.
        retrieval.query_embedding_cache.clear()
        assert (
            followups.suggest(
                db,
                workspace_id=workspace_id,
                answer=ANSWER,
                evidence=evidence,
                settings=settings,
            )
            == []
        )
    finally:
        db.close()
        get_settings.cache_clear()


def test_a_heading_of_pure_stopwords_is_refused_before_it_is_probed(monkeypatch):
    """MIN_QUERY_TERMS, applied to the HEADING rather than the rendered
    question — the fixed template always contributes "workspace", so a check on
    the finished string would pass everything and the dense arm would be asked
    to rank the whole corpus by similarity to "what is the"."""
    probed: list[str] = []

    def spy(db, **kwargs):
        probed.append(kwargs["query"])
        return (0.0, [])

    monkeypatch.setattr(followups, "probe", spy)
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        db.commit()
        stopwords_only = "## the\n\n## what about this\n"
        assert followups.heading_candidates(stopwords_only) == []
        followups.suggest(
            db,
            workspace_id=workspace_id,
            answer=stopwords_only,
            evidence=[],
            settings=get_settings(),
        )
        assert probed == []
    finally:
        db.close()


def test_serialize_is_the_one_shape_the_column_and_the_payload_share():
    item = followups.Followup(
        text="What next?", origin="kg", probe_score=0.123456789, chunk_ids=("a", "b")
    )
    assert followups.serialize([item]) == [
        {
            "text": "What next?",
            "origin": "kg",
            "probe_score": 0.123457,
            "chunk_ids": ["a", "b"],
        }
    ]


def test_polish_is_the_identity_under_the_scripted_provider(monkeypatch):
    """CI never reaches a provider, and the eval measures the derived path."""
    called: list[object] = []

    def explode(*args, **kwargs):  # pragma: no cover - must not run
        called.append(args)
        raise AssertionError("polish reached the provider under scripted")

    from app.services import model as model_service

    monkeypatch.setattr(model_service, "polish_followups", explode)
    db = SessionLocal()
    try:
        items = [
            followups.Followup(
                text="What next?", origin="heading", probe_score=0.5, chunk_ids=("a",)
            )
        ]
        settings = get_settings()
        assert settings.active_model_provider == "scripted"
        assert (
            followups.polish(
                db, items, workspace_id="w", user_id="u", settings=settings
            )
            == items
        )
        assert called == []
    finally:
        db.close()


def test_a_raising_suggester_leaves_the_column_empty_not_an_empty_list(monkeypatch):
    """'' is "never computed"; '[]' is "computed and nothing admitted". A
    failure must record the first, because the second is a claim about the
    corpus that nobody made."""
    from app.services import runs as runs_service

    def boom(*args, **kwargs):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(followups, "suggest", boom)
    db = SessionLocal()
    try:
        from app.models import Run

        workspace_id = _workspace(db)
        conversation = Conversation(workspace_id=workspace_id, created_by="")
        db.add(conversation)
        db.flush()
        run = Run(
            id=new_id(),
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            agent_id="",
            created_by="",
            status="running",
            prompt="q",
        )
        db.add(run)
        db.commit()
        assert runs_service._record_followups(run, "an answer", []) is None
    finally:
        db.close()


def test_the_graph_scan_is_bounded_by_the_cited_chunks_it_will_consult():
    """The candidate build runs before `message.completed`, so it is tail
    latency on every completed run.

    Each cited chunk costs an unindexable LIKE scan over the workspace's entity
    projection plus a BFS per entity it finds, and that used to scale with the
    evidence list — which a plan-execute or council run fills with dozens of
    passages — while the probe cap bounded only what happened afterwards. Two
    bounds now: distinct chunks, deduplicated and capped, and a lazy stream the
    caller stops.
    """
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        db.commit()
        chunk_ids = [new_id() for _ in range(12)]
        evidence = [
            retrieval.Evidence(
                chunk_id=chunk_id,
                source_id=new_id(),
                filename="x.md",
                ordinal=0,
                excerpt="x",
                score=1.0,
            )
            # Each id twice: the same passage cited twice used to repeat the
            # identical scan.
            for chunk_id in chunk_ids + chunk_ids
        ]
        scans: list[str] = []
        from sqlalchemy import event

        def record(conn, cursor, statement, parameters, context, executemany):
            if "graph_entities" in statement:
                scans.append(statement)

        event.listen(db.get_bind(), "before_cursor_execute", record)
        try:
            assert (
                followups.kg_candidates(
                    db, workspace_id=workspace_id, evidence=evidence
                )
                == []
            )
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", record)
        assert len(scans) <= followups.MAX_CITED_CHUNKS, len(scans)
    finally:
        db.close()


def test_kg_candidates_are_empty_when_the_projection_has_never_been_built():
    """An empty list is a valid answer, not a failure to report to the user."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        db.commit()
        assert (
            followups.kg_candidates(
                db,
                workspace_id=workspace_id,
                evidence=[
                    retrieval.Evidence(
                        chunk_id=new_id(),
                        source_id=new_id(),
                        filename="x.md",
                        ordinal=0,
                        excerpt="x",
                        score=1.0,
                    )
                ],
            )
            == []
        )
    finally:
        db.close()
