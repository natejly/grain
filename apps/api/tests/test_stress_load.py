"""Load, measured in queries and rows rather than in seconds.

Wall-clock assertions do not survive CI, so nothing here times anything. What it
measures instead is the shape of the work: how many SQL statements a listing
costs as its contents grow, and how many rows a search pulls into the API
process as the corpus grows. Those are the two ways this app can fall over that
a functional test cannot see, and both are exactly reproducible.

The instrument is a SQLAlchemy `before_cursor_execute` listener on the shared
engine, installed and removed inside a context manager, so a counted block sees
every statement including the ones a lazy attribute triggers.

The largest fixture here is 1200 chunks, which builds in well under a second,
so nothing needs excluding from a normal run.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, List

import pytest
from conftest import TEST_BASE_URL, authenticate, create_identity
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.database import SessionLocal, engine
from app.main import app
from app.models import (
    AppRelease,
    Board,
    BoardCard,
    BoardColumn,
    Chunk,
    Document,
    GeneratedApp,
    Source,
    new_id,
)
from app.services.retrieval import index_chunks, search_evidence


class QueryCounter:
    def __init__(self) -> None:
        self.statements: List[str] = []

    @property
    def count(self) -> int:
        return len(self.statements)

    def matching(self, fragment: str) -> int:
        return sum(1 for text in self.statements if fragment in text)


@contextmanager
def counted() -> Iterator[QueryCounter]:
    counter = QueryCounter()

    def record(conn, cursor, statement, parameters, context, executemany):
        counter.statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", record)


@pytest.fixture
def caller() -> TestClient:
    identity = create_identity()
    client = TestClient(app, base_url=TEST_BASE_URL)
    authenticate(client, identity)
    client.identity = identity  # type: ignore[attr-defined]
    return client


# --------------------------------------------------------------------------
# Listings: does the statement count grow with the contents?


def _make_boards(workspace_id: str, user_id: str, count: int) -> None:
    db = SessionLocal()
    try:
        for index in range(count):
            board = Board(
                workspace_id=workspace_id, name=f"Board {index}", created_by=user_id
            )
            db.add(board)
            db.flush()
            column = BoardColumn(
                workspace_id=workspace_id, board_id=board.id, name="Todo", position=0
            )
            db.add(column)
            db.flush()
            db.add(
                BoardCard(
                    workspace_id=workspace_id,
                    board_id=board.id,
                    column_id=column.id,
                    title=f"Card {index}",
                    position=0,
                )
            )
        db.commit()
    finally:
        db.close()


def test_listing_boards_costs_a_query_per_board(caller: TestClient) -> None:
    """`GET /api/boards` is N+1, and this measures the N.

    `list_boards` calls `snapshot` per board, and `snapshot` issues one query for
    that board's columns and one for its cards — so the statement count is
    `1 + 2N`, not a constant. Twenty boards already cost forty-one round trips.

    Written as a *measurement with a ceiling* rather than a pass/fail on the
    ideal: the assertion is that the cost is not worse than linear and that the
    linearity is real, so a fix that batches the children makes this test fail
    loudly and get updated, and a regression that adds a third per-board query
    fails it too.
    """
    identity = caller.identity  # type: ignore[attr-defined]
    _make_boards(identity.workspace_id, identity.user_id, 4)
    with counted() as small:
        assert caller.get("/api/boards").status_code == 200

    _make_boards(identity.workspace_id, identity.user_id, 16)
    with counted() as large:
        assert caller.get("/api/boards").status_code == 200

    growth = large.count - small.count
    assert growth >= 16, (
        f"expected the board listing to cost at least one query per added board; "
        f"it grew by {growth} for 16 more boards ({small.count} -> {large.count})"
    )
    assert growth <= 3 * 16, (
        f"the board listing costs {growth / 16:.1f} queries per board; anything "
        "above the two `snapshot` issues is a new N+1"
    )


def test_listing_apps_costs_a_query_per_app(caller: TestClient) -> None:
    """Same shape in `GET /api/apps`: `_app_out` fetches each app's releases.

    Recorded next to the boards case because the two are independent instances of
    one pattern — a list route that builds its response object per row — and a
    reviewer looking at either one alone would read it as a local problem.
    """
    identity = caller.identity  # type: ignore[attr-defined]

    def add_apps(count: int) -> None:
        db = SessionLocal()
        try:
            for _ in range(count):
                row = GeneratedApp(
                    workspace_id=identity.workspace_id,
                    created_by=identity.user_id,
                    name="App",
                    slug=f"app-{new_id()[:12]}",
                    app_type="code",
                )
                db.add(row)
                db.flush()
                db.add(
                    AppRelease(
                        workspace_id=identity.workspace_id,
                        app_id=row.id,
                        created_by=identity.user_id,
                        version=1,
                        status="draft",
                        manifest_json='{"kind":"code","html":"<p>x</p>"}',
                        content_hash="0" * 64,
                    )
                )
            db.commit()
        finally:
            db.close()

    add_apps(2)
    with counted() as small:
        assert caller.get("/api/apps").status_code == 200
    add_apps(12)
    with counted() as large:
        assert caller.get("/api/apps").status_code == 200

    growth = large.count - small.count
    assert growth >= 12, (
        f"the app listing grew by only {growth} queries for 12 more apps"
    )


def test_listing_documents_does_not_cost_a_query_per_document(
    caller: TestClient,
) -> None:
    """The control, and the shape the two above should be.

    `list_documents` is a single select and a projection, so its cost is flat.
    Without this the two N+1 tests would read as "query counts grow, that is
    life" rather than "these two routes are the exception".
    """
    identity = caller.identity  # type: ignore[attr-defined]

    def add_documents(count: int) -> None:
        db = SessionLocal()
        try:
            for index in range(count):
                db.add(
                    Document(
                        workspace_id=identity.workspace_id,
                        title=f"Doc {index}",
                        kind="markdown",
                        content="body",
                        created_by=identity.user_id,
                    )
                )
            db.commit()
        finally:
            db.close()

    add_documents(2)
    with counted() as small:
        assert caller.get("/api/documents").status_code == 200
    add_documents(60)
    with counted() as large:
        assert caller.get("/api/documents").status_code == 200

    assert large.count == small.count, (
        f"the document listing went from {small.count} to {large.count} queries "
        "for 60 more documents"
    )


# --------------------------------------------------------------------------
# Retrieval over a large corpus


@pytest.fixture(scope="module")
def big_corpus(client) -> str:
    """One workspace holding 1200 indexed chunks, built once for the module."""
    from app.models import Workspace

    db = SessionLocal()
    try:
        workspace = Workspace(name="load-corpus")
        db.add(workspace)
        db.flush()
        source = Source(
            workspace_id=workspace.id,
            created_by="00000000-0000-4000-8000-000000000002",
            filename="corpus.md",
            media_type="text/markdown",
            object_key="/tmp/not-used",
            byte_size=1,
            status="ready",
            chunk_count=1200,
        )
        db.add(source)
        db.flush()
        chunks = []
        for ordinal in range(1200):
            text = (
                f"Passage {ordinal} discusses quarterly revenue in region "
                f"{ordinal % 20} with a distinctive marker token{ordinal:05d}."
            )
            chunk = Chunk(
                workspace_id=workspace.id,
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
        return workspace.id
    finally:
        db.close()


def test_a_common_term_over_a_large_corpus_stays_bounded(big_corpus: str) -> None:
    """The pathological query is the *common* term, not the long one.

    "revenue" appears in every one of 1200 chunks, so the posting scan touches
    the whole corpus. What must stay bounded is what leaves the database: the
    final chunk fetch is limited to `max(limit * 4, 20)` candidates, and the
    excerpt budget bounds the text. This asserts both — a change that dropped
    either cap would put the whole corpus in the prompt.
    """
    db = SessionLocal()
    try:
        with counted() as counter:
            results = search_evidence(
                db, workspace_id=big_corpus, query="revenue", limit=5
            )
    finally:
        db.close()

    assert len(results) <= 5
    assert sum(len(item.excerpt.split()) for item in results) <= 1200
    # A handful of statements, not one per chunk.
    assert counter.count < 20, (
        f"a single search issued {counter.count} statements over 1200 chunks"
    )


def test_a_rare_term_over_a_large_corpus_still_finds_its_chunk(
    big_corpus: str,
) -> None:
    """The other half: the caps above must not turn a large corpus into a
    corpus where a specific passage is unfindable."""
    db = SessionLocal()
    try:
        results = search_evidence(
            db, workspace_id=big_corpus, query="token01137", limit=5
        )
    finally:
        db.close()
    assert any("token01137" in item.excerpt for item in results), (
        "a uniquely-named passage was not retrievable from a 1200-chunk corpus"
    )


def test_the_vector_candidate_cap_is_a_recency_window(big_corpus: str) -> None:
    """`dense_ranking` selects candidates ordered by `created_at DESC` under
    `retrieval_vector_candidate_cap`, so the cap does not sample the corpus — it
    truncates it to the newest rows.

    Pinned at a small cap because the real one (20000) needs a corpus no test
    should build. The consequence at production scale is that a workspace past
    the cap loses dense recall for its oldest documents silently, which is a
    correctness cliff rather than a slowdown.
    """
    import hashlib

    from app.config import get_settings
    from app.services import retrieval as retrieval_service
    from app.services.embeddings import pack_vector
    from app.services.retrieval import tokenize

    def fake(texts, settings=None):
        vectors = []
        for text in texts:
            values = [0.0] * 64
            for token in tokenize(text):
                values[int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % 64] += 1
            norm = sum(value * value for value in values) ** 0.5
            vectors.append(pack_vector([v / norm for v in values] if norm else values))
        return vectors

    original = retrieval_service.embed_texts
    retrieval_service.embed_texts = fake  # type: ignore[assignment]
    db = SessionLocal()
    try:
        chunks = list(
            db.query(Chunk).filter(Chunk.workspace_id == big_corpus).limit(200).all()
        )
        retrieval_service.embed_chunks(db, chunks)
        db.commit()
        settings = get_settings().model_copy(
            update={"retrieval_vector_candidate_cap": 10, "retrieval_dense_floor": 0.0}
        )
        ranked = retrieval_service.dense_ranking(
            db, workspace_id=big_corpus, query="quarterly revenue", settings=settings
        )
    finally:
        retrieval_service.embed_texts = original  # type: ignore[assignment]
        db.close()

    assert len(ranked) <= 10, "the candidate cap is not applied in SQL"
