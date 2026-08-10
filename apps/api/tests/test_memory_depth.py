"""Bounded-candidate recall, and the tools that write memory on purpose.

The recall change is a *performance* change: the candidate set is bounded, the
scoring is not. `_legacy_recall` below is a transcription of the full-scan
implementation it replaced, and the first test asserts the two rank a fixed
corpus identically — with and without embeddings, and with a mixed-dimension
blob in the corpus.
"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, List, Optional, Tuple

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import DEV_SEED_USER_ID
from app.config import get_settings
from app.database import SessionLocal
from app.models import Conversation, MemoryItem, Workspace
from app.services import memory as memory_service
from app.services.embeddings import cosine_similarity, pack_vector, unpack_vector
from app.services.llm_tools import ToolContext
from app.services.memory import recall
from app.services.memory_tools import registry_tools
from app.services.retrieval import tokenize

EMBED_DIM = 8


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


def _fake_vector(text: str, dim: int = EMBED_DIM) -> bytes:
    """A deterministic stand-in for an embedding model.

    Tokens land in a bucket by hash and the vector is L2-normalised, so texts
    sharing tokens are genuinely closer — enough for a ranking comparison, with
    no network and no API key.
    """
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
    """A fresh, empty workspace so seeded corpora cannot collide across tests."""
    db = SessionLocal()
    try:
        row = Workspace(name="memory-depth")
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _conversation(db: Session, workspace_id: str) -> str:
    row = Conversation(workspace_id=workspace_id, created_by=DEV_SEED_USER_ID)
    db.add(row)
    db.flush()
    return row.id


def _seed(
    db: Session,
    workspace_id: str,
    content: str,
    *,
    kind: str = "fact",
    conversation_id: Optional[str] = None,
    importance: int = 1,
    embedding: Optional[bytes] = None,
) -> MemoryItem:
    item = MemoryItem(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        kind=kind,
        content=content,
        normalized_key=hashlib.sha256(content.encode()).hexdigest()[:40],
        importance=importance,
        embedding=embedding,
    )
    db.add(item)
    db.flush()
    return item


# --------------------------------------------------------------------------- #
# The implementation this change replaced, transcribed verbatim.
# --------------------------------------------------------------------------- #


def _legacy_recall(
    db: Session,
    *,
    workspace_id: str,
    conversation_id: str,
    query: str,
    limit: int,
    query_vector: Optional[List[float]] = None,
) -> List[MemoryItem]:
    items = list(
        db.scalars(
            select(MemoryItem).where(
                MemoryItem.workspace_id == workspace_id,
                MemoryItem.status == "active",
            )
        )
    )
    query_terms = set(tokenize(query))
    scored: List[Tuple[float, MemoryItem]] = []
    for item in items:
        if item.kind == "summary" and item.conversation_id == conversation_id:
            scored.append((float("inf"), item))
            continue
        item_terms = set(tokenize(item.content))
        lexical = len(query_terms & item_terms) / max(1, len(query_terms))
        semantic = 0.0
        if query_vector is not None and item.embedding is not None:
            semantic = max(
                0.0, cosine_similarity(query_vector, unpack_vector(item.embedding))
            )
        score = lexical + semantic + min(item.importance, 5) * 0.05
        if lexical > 0 or semantic > 0.3:
            scored.append((score, item))
    # Ties fell out of database scan order here and are broken by id in the new
    # implementation; sorting both the same way compares the scoring, which is
    # the part that must not move.
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [item for _score, item in scored[:limit]]


CORPUS = [
    "Project Atlas ships the deployment ring on a weekly schedule.",
    "Atlas deployment notes from the platform team.",
    "The ring buffer schedule is unrelated to Atlas.",
    "Canary releases use a violet ring.",
    "Dana Reyes leads the platform team.",
]
QUERY = "atlas deployment ring schedule"


@pytest.mark.parametrize("with_embeddings", [False, True])
def test_recall_ranking_matches_the_full_scan_it_replaced(
    workspace, monkeypatch, with_embeddings
):
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        for text in CORPUS:
            _seed(
                db,
                workspace,
                text,
                embedding=_fake_vector(text) if with_embeddings else None,
            )
        # A blob from a different embedding model. The old code scored it 0.0 via
        # cosine_similarity's length guard; the new code must drop it rather than
        # let np.reshape raise.
        _seed(
            db,
            workspace,
            "Atlas ring rotation, embedded by an older model.",
            embedding=_fake_vector("Atlas ring rotation", dim=4),
        )
        db.commit()

        query_vector: Optional[List[float]] = None
        if with_embeddings:
            query_blob = _fake_vector(QUERY)
            query_vector = unpack_vector(query_blob)
            monkeypatch.setattr(
                memory_service, "embed_texts", lambda texts, settings=None: [query_blob]
            )
        else:
            monkeypatch.setattr(
                memory_service, "embed_texts", lambda texts, settings=None: None
            )

        settings = get_settings()
        expected = _legacy_recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query=QUERY,
            limit=settings.memory_recall_limit,
            query_vector=query_vector,
        )
        actual = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query=QUERY,
            settings=settings,
        )
        assert expected, "the fixture must actually match something"
        assert [item.id for item in actual.items] == [item.id for item in expected]
        assert actual.items[0].content == CORPUS[0]
    finally:
        db.close()


def test_recall_pins_the_current_conversation_summary(workspace, monkeypatch):
    monkeypatch.setattr(memory_service, "embed_texts", lambda texts, settings=None: None)
    db = SessionLocal()
    try:
        mine = _conversation(db, workspace)
        theirs = _conversation(db, workspace)
        _seed(
            db,
            workspace,
            "Conversation topics so far: quarterly planning",
            kind="summary",
            conversation_id=mine,
        )
        _seed(
            db,
            workspace,
            "Conversation topics so far: hiring",
            kind="summary",
            conversation_id=theirs,
        )
        _seed(db, workspace, "Atlas ships on Fridays.")
        db.commit()

        found = recall(
            db, workspace_id=workspace, conversation_id=mine, query="atlas"
        )
        contents = [item.content for item in found.items]
        # Pinned first, even though it shares no term with the query.
        assert contents[0] == "Conversation topics so far: quarterly planning"
        assert "Atlas ships on Fridays." in contents
        # Another conversation's summary is scored normally, so it does not match.
        assert "Conversation topics so far: hiring" not in contents

        # ...and it is pinned exactly once, not also pulled in as a candidate.
        summary_hits = [c for c in contents if c.startswith("Conversation topics")]
        assert len(summary_hits) == 1
    finally:
        db.close()


def test_recall_never_crosses_a_workspace_boundary(client, monkeypatch):
    """One assertion per candidate path: pinned summary, lexical, vector."""
    db = SessionLocal()
    try:
        mine = Workspace(name="mine")
        theirs = Workspace(name="theirs")
        db.add_all([mine, theirs])
        db.flush()
        shared_conversation = _conversation(db, mine.id)
        secret = "Obsidian Vault password rotation runs on Tuesdays."
        _seed(
            db,
            theirs.id,
            secret,
            embedding=_fake_vector(secret),
        )
        _seed(
            db,
            theirs.id,
            "Conversation topics so far: obsidian vault",
            kind="summary",
            conversation_id=shared_conversation,
        )
        _seed(db, mine.id, "Nothing to do with vaults.")
        db.commit()

        query_blob = _fake_vector("obsidian vault rotation")
        monkeypatch.setattr(
            memory_service, "embed_texts", lambda texts, settings=None: [query_blob]
        )
        found = recall(
            db,
            workspace_id=mine.id,
            conversation_id=shared_conversation,
            query="obsidian vault rotation",
        )
        assert all("Obsidian" not in item.content for item in found.items)
        assert all(item.kind != "summary" for item in found.items)
    finally:
        db.close()


def test_lexical_prefilter_is_case_folded_and_escapes_wildcards(workspace, monkeypatch):
    """SQLite's LIKE is ASCII-case-insensitive and Postgres's is not, so the
    prefilter must fold explicitly; TOKEN_RE admits `_`, which LIKE treats as a
    wildcard, so tokens must be escaped."""
    monkeypatch.setattr(memory_service, "embed_texts", lambda texts, settings=None: None)
    db = SessionLocal()
    try:
        _seed(db, workspace, "The READ_ONLY flag gates every mutation.")
        _seed(db, workspace, "The readXonly flag is a different thing entirely.")
        db.commit()

        found = recall(
            db, workspace_id=workspace, conversation_id="none", query="read_only flag"
        )
        contents = [item.content for item in found.items]
        assert "The READ_ONLY flag gates every mutation." in contents
        # readXonly matches `read_only` only if `_` leaked through as a wildcard.
        # It still surfaces on the shared token "flag", so check the ranking.
        assert contents[0] == "The READ_ONLY flag gates every mutation."
    finally:
        db.close()


def test_vector_candidate_cap_bounds_the_scan_without_hiding_lexical_matches(
    workspace, monkeypatch
):
    """Twelve rows are a perfect vector match; a cap of 1 must score exactly one
    of them. The row pushed outside the recency window still arrives, because the
    lexical prefilter is not subject to the cap — that is what makes the cap
    survivable."""
    monkeypatch.setattr(memory_service, "embed_texts", lambda texts, settings=None: None)
    db = SessionLocal()
    try:
        for index in range(12):
            _seed(
                db,
                workspace,
                f"Filler memory number {index} about unrelated cabbages.",
                embedding=_fake_vector("cabbages"),
            )
        oldest = _seed(
            db, workspace, "Zephyr Program launches in March.", embedding=None
        )
        db.commit()
        # Make the interesting row the *oldest*, so a cap of 1 excludes it from
        # the vector scan entirely.
        db.execute(
            MemoryItem.__table__.update()
            .where(MemoryItem.id == oldest.id)
            .values(updated_at=memory_service.utcnow().replace(year=2000))
        )
        db.commit()

        query_blob = _fake_vector("cabbages")
        monkeypatch.setattr(
            memory_service, "embed_texts", lambda texts, settings=None: [query_blob]
        )
        settings = get_settings().model_copy(
            update={"memory_recall_candidate_cap": 1, "memory_recall_limit": 6}
        )
        found = recall(
            db,
            workspace_id=workspace,
            conversation_id="none",
            query="zephyr program launch",
            settings=settings,
        )
        contents = [item.content for item in found.items]
        assert sum(1 for text in contents if text.startswith("Filler")) == 1
        assert "Zephyr Program launches in March." in contents
    finally:
        db.close()


def test_uncapped_recall_scores_every_vector(workspace, monkeypatch):
    """cap=0 means "exactness over latency" — every embedded row is scored."""
    db = SessionLocal()
    try:
        for index in range(12):
            _seed(
                db,
                workspace,
                f"Filler memory number {index} about unrelated cabbages.",
                embedding=_fake_vector("cabbages"),
            )
        db.commit()
        query_blob = _fake_vector("cabbages")
        monkeypatch.setattr(
            memory_service, "embed_texts", lambda texts, settings=None: [query_blob]
        )
        settings = get_settings().model_copy(
            update={"memory_recall_candidate_cap": 0, "memory_recall_limit": 12}
        )
        found = recall(
            db,
            workspace_id=workspace,
            conversation_id="none",
            query="something entirely different",
            settings=settings,
        )
        assert len(found.items) == 12
    finally:
        db.close()


def test_vector_scores_tolerates_degenerate_input():
    query = _fake_vector("atlas")
    assert memory_service._vector_scores([], query) == ({}, [])
    assert memory_service._vector_scores([("a", None)], query) == ({}, [])
    # Wrong dimension, truncated blob, and a zero vector must all be survivable.
    assert memory_service._vector_scores([("a", b"\x00\x01\x02")], query) == ({}, [])
    zero, zero_short = memory_service._vector_scores(
        [("a", pack_vector([0.0] * EMBED_DIM))], query
    )
    assert zero == {"a": 0.0}
    assert zero_short == ["a"]
    single, single_short = memory_service._vector_scores([("a", query)], query)
    assert single["a"] == pytest.approx(1.0, abs=1e-6)
    assert single_short == ["a"]


def test_vector_scores_returns_every_row_but_shortlists_only_the_best():
    """The two return values do different jobs and must not be conflated.

    The shortlist bounds how many rows the *vector* path adds to the candidate
    set. The score map has to cover every row scanned, because a row can also
    arrive lexically — and the full scan this replaced gave that row its real
    semantic term. Returning only the shortlist scored such a row 0.0.
    """
    query = _fake_vector("atlas deployment")
    rows = [(f"id{index}", _fake_vector(f"atlas note {index}")) for index in range(200)]
    scores, shortlist = memory_service._vector_scores(rows, query)
    assert len(scores) == 200, "every scanned row keeps a similarity"
    assert len(shortlist) == memory_service.VECTOR_SHORTLIST
    # The shortlist really is the top-k, not an arbitrary slice. Compared by
    # score rather than by id, because equal-similarity rows make the *set*
    # ambiguous while the multiset of scores is not.
    best = sorted(scores.values(), reverse=True)[: memory_service.VECTOR_SHORTLIST]
    assert sorted((scores[key] for key in shortlist), reverse=True) == best


def test_lexical_hit_outside_the_vector_shortlist_keeps_its_semantic_score(
    workspace, monkeypatch
):
    """A row can enter through the lexical prefilter and still deserve its
    semantic term. Scoring it 0.0 because it missed the vector top-k drops up to
    a full 1.0 off its score and silently evicts it from the injected context —
    a ranking change disguised as a performance change.
    """
    db = SessionLocal()
    try:
        # Enough perfect-vector filler to fill the shortlist on its own.
        for index in range(memory_service.VECTOR_SHORTLIST + 5):
            _seed(
                db,
                workspace,
                f"Filler {index} about cabbages and cabbages.",
                embedding=_fake_vector("cabbages cabbages"),
            )
        # Shares every query term, but its vector is a weaker match than the
        # filler wall, so it lands outside the shortlist.
        target = _seed(
            db,
            workspace,
            "Zephyr Program cabbages ship in March.",
            embedding=_fake_vector("zephyr program march cabbages"),
        )
        db.commit()

        query = "zephyr program march"
        query_blob = _fake_vector("cabbages cabbages")
        monkeypatch.setattr(
            memory_service, "embed_texts", lambda texts, settings=None: [query_blob]
        )
        settings = get_settings()

        scores, shortlist = memory_service._vector_candidates(
            db,
            workspace_id=workspace,
            query_blob=query_blob,
            exclude_id=None,
            settings=settings,
        )
        assert target.id not in shortlist, "fixture must push the row off the shortlist"
        assert scores[target.id] > 0.0, "but its similarity is still known"

        found = recall(
            db,
            workspace_id=workspace,
            conversation_id="none",
            query=query,
            settings=settings,
        )
        expected = _legacy_recall(
            db,
            workspace_id=workspace,
            conversation_id="none",
            query=query,
            limit=settings.memory_recall_limit,
            query_vector=unpack_vector(query_blob),
        )
        # The filler wall is a 69-way score tie, so only the head of the ranking
        # is well defined — but the head is the whole point: with the semantic
        # term dropped the target ties with the filler and sorts by id instead.
        assert expected[0].id == target.id
        assert found.items[0].id == target.id
        assert _score(target, query, query_blob) == pytest.approx(
            _score(found.items[0], query, query_blob)
        )
    finally:
        db.close()


def test_lexical_prefilter_truncates_by_relevance_not_by_popularity(
    workspace, monkeypatch
):
    """The prefilter's LIMIT decides which lexical matches are scored at all.

    `importance` contributes at most 0.25 to a score whose lexical term spans a
    full 1.0, so ordering the truncation by importance discards the memory that
    matches every query term in favour of a popular one that matches a single
    stop-word-adjacent term.
    """
    monkeypatch.setattr(memory_service, "embed_texts", lambda texts, settings=None: None)
    db = SessionLocal()
    try:
        for index in range(30):
            _seed(
                db,
                workspace,
                f"Popular filler {index} mentioning zephyr only.",
                importance=99,
            )
        target = _seed(
            db,
            workspace,
            "Zephyr Program launches in March with Dana Reyes.",
            importance=1,
        )
        db.commit()

        settings = get_settings().model_copy(
            update={"memory_lexical_candidate_limit": 5}
        )
        ids = memory_service._lexical_candidates(
            db,
            workspace_id=workspace,
            terms=["zephyr", "program", "launches", "march", "dana", "reyes"],
            exclude_id=None,
            limit=settings.memory_lexical_candidate_limit,
        )
        assert target.id in ids, "the row matching six terms must survive the LIMIT"

        found = recall(
            db,
            workspace_id=workspace,
            conversation_id="none",
            query="zephyr program launches march dana reyes",
            settings=settings,
        )
        assert found.items[0].id == target.id
    finally:
        db.close()


def _score(item: MemoryItem, query: str, query_blob: bytes) -> float:
    """The original scoring formula, for comparing two rankings by score."""
    query_terms = set(tokenize(query))
    item_terms = set(tokenize(item.content))
    lexical = len(query_terms & item_terms) / max(1, len(query_terms))
    semantic = max(
        0.0, cosine_similarity(unpack_vector(query_blob), unpack_vector(item.embedding))
    )
    return lexical + semantic + min(item.importance, 5) * 0.05


@pytest.mark.parametrize("corpus_size", [200, 1200])
def test_recall_matches_the_full_scan_beyond_the_shortlist_and_prefilter_limits(
    workspace, monkeypatch, corpus_size
):
    """The ranking comparison, run at sizes where the bounds actually bite.

    The 5-row fixture above cannot see either cap: it is smaller than the vector
    shortlist and the lexical LIMIT. These sizes are larger than both, which is
    where a bounded candidate set stops being equivalent to the full scan.
    """
    import random

    words = [
        "atlas", "deployment", "ring", "schedule", "platform", "canary", "violet",
        "buffer", "rotation", "dana", "reyes", "quarterly", "hiring", "vault",
    ]
    rng = random.Random(corpus_size)
    db = SessionLocal()
    try:
        for index in range(corpus_size):
            text = (
                " ".join(rng.choice(words) for _ in range(rng.randint(3, 7)))
                + f" note{index}"
            )
            _seed(db, workspace, text, importance=rng.randint(1, 6),
                  embedding=_fake_vector(text))
        db.commit()

        settings = get_settings()
        for query in (
            "atlas deployment ring schedule",
            "vault rotation quarterly hiring",
            "canary release violet buffer platform",
        ):
            query_blob = _fake_vector(query)
            monkeypatch.setattr(
                memory_service,
                "embed_texts",
                lambda texts, settings=None, blob=query_blob: [blob],
            )
            expected = _legacy_recall(
                db,
                workspace_id=workspace,
                conversation_id="none",
                query=query,
                limit=settings.memory_recall_limit,
                query_vector=unpack_vector(query_blob),
            )
            found = recall(
                db,
                workspace_id=workspace,
                conversation_id="none",
                query=query,
                settings=settings,
            )
            assert expected, "the fixture must actually match something"
            # Exact score ties fell out of database scan order in the full scan
            # and out of float32 rounding here, so compare the scores rather than
            # the ids: the ranking must be score-identical, not merely close.
            assert [
                round(_score(item, query, query_blob), 9) for item in found.items
            ] == [
                round(_score(item, query, query_blob), 9) for item in expected
            ], f"ranking moved for query {query!r} at {corpus_size} rows"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Agentic memory tools
# --------------------------------------------------------------------------- #


def _tools(db: Session, workspace_id: str, conversation_id: str):
    context = ToolContext(
        workspace_id=workspace_id,
        user_id=DEV_SEED_USER_ID,
        conversation_id=conversation_id,
    )
    return registry_tools(db, context), context


def test_remember_is_recallable_in_the_same_conversation(workspace):
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        db.commit()
        tools, context = _tools(db, workspace, conversation_id)

        result = tools["remember"].executor(
            db,
            context,
            {
                "content": "Nate prefers terse answers with no preamble.",
                "kind": "preference",
                "entities": ["Nate"],
            },
        )
        assert "Stored" in result.content

        # No commit in between: the same turn that stored it must see it.
        found = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query="How should I answer Nate?",
        )
        assert any("terse answers" in item.content for item in found.items)

        stored = db.scalar(
            select(MemoryItem).where(
                MemoryItem.workspace_id == workspace,
                MemoryItem.kind == "preference",
            )
        )
        assert stored is not None
        assert json.loads(stored.entity_names_json) == ["Nate"]
        db.commit()
    finally:
        db.close()


def test_remember_requires_content_and_previews_what_it_stores(workspace):
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        db.commit()
        tools, context = _tools(db, workspace, conversation_id)
        spec = tools["remember"]
        assert spec.read_only is False
        assert spec.preview is not None

        assert "Error" in spec.executor(db, context, {}).content
        assert "will fail" in spec.preview(db, context, {})

        preview = spec.preview(
            db,
            context,
            {"content": "  Deploys   happen on Fridays.  ", "kind": "fact"},
        )
        assert "Deploys happen on Fridays." in preview
        assert "fact" in preview
        # A preview must not write anything.
        assert db.scalar(
            select(MemoryItem).where(MemoryItem.workspace_id == workspace)
        ) is None
    finally:
        db.close()


def test_remember_dedupes_and_overrides_a_tombstone(workspace):
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        db.commit()
        tools, context = _tools(db, workspace, conversation_id)
        args = {"content": "Atlas deploys on Fridays.", "kind": "fact"}

        tools["remember"].executor(db, context, args)
        again = tools["remember"].executor(db, context, args)
        assert "reinforced" in again.content
        rows = list(
            db.scalars(
                select(MemoryItem).where(MemoryItem.workspace_id == workspace)
            )
        )
        assert len(rows) == 1
        assert rows[0].importance == 2

        tools["forget"].executor(db, context, {"memory_id": rows[0].id})
        assert rows[0].status == "deleted"

        # An explicit "remember this" overrides the tombstone that exists to stop
        # the *extractor* from resurrecting it.
        restored = tools["remember"].executor(db, context, args)
        assert "Restored" in restored.content
        assert rows[0].status == "active"
        db.commit()
    finally:
        db.close()


def test_forget_preview_names_every_memory_it_would_tombstone(workspace):
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        _seed(db, workspace, "Atlas deploys on Fridays.")
        _seed(db, workspace, "Atlas is owned by the platform team.")
        keeper = _seed(db, workspace, "Orion is owned by the data team.")
        db.commit()
        tools, context = _tools(db, workspace, conversation_id)
        spec = tools["forget"]
        assert spec.read_only is False
        assert spec.preview is not None

        preview = spec.preview(db, context, {"content": "atlas"})
        assert "Forget 2 memories" in preview
        assert "Atlas deploys on Fridays." in preview
        assert "Atlas is owned by the platform team." in preview
        assert "Orion" not in preview
        # Previewing must not tombstone anything.
        assert (
            db.scalars(
                select(MemoryItem).where(
                    MemoryItem.workspace_id == workspace,
                    MemoryItem.status == "active",
                )
            ).all()
            != []
        )

        result = spec.executor(db, context, {"content": "atlas"})
        forgotten = json.loads(result.content)["forgotten"]
        assert len(forgotten) == 2
        remaining = list(
            db.scalars(
                select(MemoryItem).where(
                    MemoryItem.workspace_id == workspace,
                    MemoryItem.status == "active",
                )
            )
        )
        assert [item.id for item in remaining] == [keeper.id]
    finally:
        db.close()


def test_forget_refuses_an_unusable_target(workspace):
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        for index in range(memory_service.MAX_FORGET_MATCHES + 1):
            _seed(db, workspace, f"Atlas note number {index}.")
        db.commit()
        tools, context = _tools(db, workspace, conversation_id)

        assert "Provide either" in tools["forget"].executor(db, context, {}).content
        assert "No active memory with id" in tools["forget"].executor(
            db, context, {"memory_id": "does-not-exist"}
        ).content
        too_broad = tools["forget"].executor(db, context, {"content": "atlas"})
        assert "more than" in too_broad.content
        # Nothing was tombstoned by any of the three refusals.
        assert (
            len(
                list(
                    db.scalars(
                        select(MemoryItem).where(
                            MemoryItem.workspace_id == workspace,
                            MemoryItem.status == "active",
                        )
                    )
                )
            )
            == memory_service.MAX_FORGET_MATCHES + 1
        )
    finally:
        db.close()


def test_forget_cannot_reach_another_workspace(client):
    db = SessionLocal()
    try:
        mine = Workspace(name="forget-mine")
        theirs = Workspace(name="forget-theirs")
        db.add_all([mine, theirs])
        db.flush()
        conversation_id = _conversation(db, mine.id)
        victim = _seed(db, theirs.id, "Their private launch date is in April.")
        db.commit()
        tools, context = _tools(db, mine.id, conversation_id)

        result = tools["forget"].executor(db, context, {"memory_id": victim.id})
        assert "No active memory with id" in result.content
        by_content = tools["forget"].executor(db, context, {"content": "private launch"})
        assert by_content.content.startswith("Error:")
        assert victim.status == "active"
    finally:
        db.close()


def test_search_memory_digs_deeper_than_the_injected_context(workspace, monkeypatch):
    monkeypatch.setattr(memory_service, "embed_texts", lambda texts, settings=None: None)
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        for index in range(12):
            _seed(db, workspace, f"Atlas milestone {index} was reviewed in sprint {index}.")
        db.commit()
        tools, context = _tools(db, workspace, conversation_id)

        default = json.loads(
            tools["search_memory"].executor(db, context, {"query": "atlas milestone"}).content
        )
        assert len(default["memories"]) == get_settings().memory_recall_limit

        deeper = json.loads(
            tools["search_memory"]
            .executor(db, context, {"query": "atlas milestone", "limit": 12})
            .content
        )
        assert len(deeper["memories"]) == 12
        assert all(entry["id"] for entry in deeper["memories"])

        # Out-of-range and nonsense limits clamp instead of failing.
        clamped = json.loads(
            tools["search_memory"]
            .executor(db, context, {"query": "atlas milestone", "limit": 999})
            .content
        )
        assert len(clamped["memories"]) == 12
        assert "Error" in tools["search_memory"].executor(db, context, {}).content
        assert tools["search_memory"].read_only is True
    finally:
        db.close()


def test_search_memory_reports_an_empty_workspace(workspace):
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        db.commit()
        tools, context = _tools(db, workspace, conversation_id)
        result = tools["search_memory"].executor(db, context, {"query": "anything"})
        assert result.content == "No stored memories match that query."
    finally:
        db.close()


def test_a_semantic_only_memory_outside_the_cap_disappears(workspace, monkeypatch):
    """The cap's real cost, written down as a test.

    A memory that shares no words with the query is reachable ONLY through the
    vector scan, so once it falls outside the recency window it is not
    down-ranked, it is absent. The neighbouring cap test seeds a row that also
    matches lexically, so the prefilter rescues it and the loss stays hidden —
    which is exactly why this went unnoticed: five memories at cosine 0.97 went
    from 5/5 recalled to 0/5 when the window was 5000 rows, and no test failed.
    """
    monkeypatch.setattr(memory_service, "embed_texts", lambda texts, settings=None: None)
    db = SessionLocal()
    try:
        for index in range(12):
            _seed(
                db,
                workspace,
                f"Filler memory number {index} about unrelated cabbages.",
                embedding=_fake_vector("cabbages"),
            )
        # Shares its embedding with the query but not a single word of it.
        buried = _seed(
            db, workspace, "Zephyr Program launches in March.",
            embedding=_fake_vector("quarterly rollout schedule"),
        )
        db.commit()
        db.execute(
            MemoryItem.__table__.update()
            .where(MemoryItem.id == buried.id)
            .values(updated_at=memory_service.utcnow().replace(year=2000))
        )
        db.commit()

        query_blob = _fake_vector("quarterly rollout schedule")
        monkeypatch.setattr(
            memory_service, "embed_texts", lambda texts, settings=None: [query_blob]
        )

        def recalled(cap: int) -> list[str]:
            settings = get_settings().model_copy(
                update={"memory_recall_candidate_cap": cap, "memory_recall_limit": 6}
            )
            found = recall(
                db,
                workspace_id=workspace,
                conversation_id="none",
                query="quarterly rollout schedule",
                settings=settings,
            )
            return [item.content for item in found.items]

        # Window too small to reach it: silently gone.
        assert "Zephyr Program launches in March." not in recalled(2)
        # Window wide enough: recovered, which is what the default must guarantee.
        assert "Zephyr Program launches in March." in recalled(100)
    finally:
        db.close()


def test_settings_expose_the_recall_bounds():
    settings = get_settings()
    assert settings.memory_recall_candidate_cap > 0
    assert settings.memory_lexical_candidate_limit > 0
    knobs: Dict[str, int] = {
        "cap": settings.memory_recall_candidate_cap,
        "lexical": settings.memory_lexical_candidate_limit,
    }
    assert knobs["cap"] >= knobs["lexical"]
    # A guard on the number itself, because shrinking it is silent: the cap is a
    # recency window, and semantic-only memories older than it vanish rather than
    # rank lower. 20000 reproduced an unbounded scan's ordering on a 10k corpus.
    assert knobs["cap"] >= 20000


def test_memory_tools_are_actually_offered_to_the_model(workspace):
    """The tools exist only if `build_registry` returns them.

    Every other test in this file reaches into `memory_tools.registry_tools`
    directly, which passes whether or not the module is wired into the agent's
    registry — and it was not, so `remember`/`forget`/`search_memory` were
    unreachable in the running app while the suite stayed green.
    """
    from app.services.llm_tools import build_registry

    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        db.commit()
        context = ToolContext(
            workspace_id=workspace,
            user_id=DEV_SEED_USER_ID,
            conversation_id=conversation_id,
        )
        registry = build_registry(db, context)
        for name in ("remember", "forget", "search_memory"):
            assert name in registry, f"{name} is not reachable by the agent"
        # Write-capable tools must be approval-gated and must render a card;
        # `_tool_policy` derives "ask" from read_only alone.
        for name in ("remember", "forget"):
            assert registry[name].read_only is False
            assert registry[name].preview is not None
        assert registry["search_memory"].read_only is True
    finally:
        db.close()


def test_recall_index_exists_in_a_create_all_schema():
    """Development and test schemas come from `create_all`, not from alembic.

    An index declared only in a migration is absent exactly where the app boots
    in development, and `alembic revision --autogenerate` would propose dropping
    it for not appearing in the model metadata.
    """
    from sqlalchemy import inspect

    from app.database import engine
    from app.models import MemoryItem

    declared = {index.name for index in MemoryItem.__table__.indexes}
    assert "ix_memory_items_ws_status_updated" in declared
    live = {index["name"] for index in inspect(engine).get_indexes("memory_items")}
    assert "ix_memory_items_ws_status_updated" in live


def test_a_failed_extraction_leaves_a_warning_behind(workspace, monkeypatch, caplog):
    """Best-effort must still be audible.

    Extraction is a model call on every install now that there is no offline
    path around it, so a rate-limited or timing-out provider drops a turn's
    memories on the floor. `write_conversation_memory` swallows that on purpose —
    a run that already answered must not be failed retroactively — but swallowed
    plus unlogged means a workspace can stop learning entirely and leave no
    trace: no run error, no event, nothing to grep for.
    """
    import logging

    from app.models import Agent, Message, Run

    def _boom(prompt, answer, *, user_id, settings=None):
        raise RuntimeError("provider returned 429")

    monkeypatch.setattr(memory_service, "extract_memories", _boom)
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        agent = Agent(workspace_id=workspace, name="probe", instructions="")
        db.add(agent)
        db.flush()
        run = Run(
            workspace_id=workspace,
            conversation_id=conversation_id,
            agent_id=agent.id,
            created_by=DEV_SEED_USER_ID,
            status="completed",
            prompt="Dana Reyes leads Project Atlas.",
        )
        db.add(run)
        db.flush()
        for role, content in (
            ("user", "Dana Reyes leads Project Atlas."),
            ("assistant", "Noted."),
        ):
            db.add(
                Message(
                    workspace_id=workspace,
                    conversation_id=conversation_id,
                    run_id=run.id,
                    role=role,
                    content=content,
                )
            )
        db.commit()
        run_id = run.id
    finally:
        db.close()

    with caplog.at_level(logging.WARNING, logger="app.services.memory"):
        memory_service.write_conversation_memory(run_id)

    assert [
        record for record in caplog.records if run_id in record.getMessage()
    ], "a dropped memory write left no log line"

    db = SessionLocal()
    try:
        assert (
            db.scalars(
                select(MemoryItem).where(MemoryItem.workspace_id == workspace)
            ).all()
            == []
        )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Supersession: a corrected claim retires the one it replaced
#
# Measured before this existed, on evals/memory_corpus.json: 100% stale-served
# (5/5) — every superseded fact came back beside its own correction, with nothing
# in the injected list to say which was current. Keying on a content hash is why:
# two phrasings of one claim could never collide, so nothing ever retired
# anything. These tests pin the four things that behaviour has to get right.
# --------------------------------------------------------------------------- #


def _write(
    db: Session,
    workspace_id: str,
    conversation_id: Optional[str],
    content: str,
    key: Optional[str] = None,
    *,
    kind: str = "fact",
    settings=None,
) -> MemoryItem:
    """One extraction's worth of memory, through the write path itself."""
    raw: Dict[str, object] = {"kind": kind, "content": content}
    if key is not None:
        raw["normalized_key"] = key
    written = memory_service.apply_extracted_memories(
        db,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        run_id="",
        extracted=[raw],
        message_ids=[],
        settings=settings or get_settings(),
    )
    db.flush()
    return written[0]


def _lexical_only(monkeypatch) -> None:
    """No embedding provider; recall still ranks lexically."""
    monkeypatch.setattr(memory_service, "embed_texts", lambda texts, settings=None: None)


def _active_rows(db: Session, workspace_id: str) -> List[MemoryItem]:
    return list(
        db.scalars(
            select(MemoryItem)
            .where(
                MemoryItem.workspace_id == workspace_id,
                MemoryItem.status == "active",
            )
            .order_by(MemoryItem.created_at)
        )
    )


def test_a_correction_supersedes_the_claim_it_replaces(workspace, monkeypatch):
    _lexical_only(monkeypatch)
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        stale = _write(
            db, workspace, conversation_id, "Nate deploys the API on Fly.io.", "api|deploy_host"
        )
        # Reinforced twice before the correction arrives: this is exactly the
        # shape that used to beat its own correction on score, since importance
        # accrues to whichever phrasing recurs rather than to whichever is true.
        _write(
            db, workspace, conversation_id, "Nate deploys the API on Fly.io.", "api|deploy_host"
        )
        assert stale.importance == 2

        current = _write(
            db,
            workspace,
            conversation_id,
            "Nate deploys the API on Railway.",
            "api|deploy_host",
        )
        assert current.id != stale.id
        assert stale.status == "superseded"
        assert stale.superseded_by == current.id
        assert current.status == "active"
        # Importance is not carried forward: it counts how often a claim recurs,
        # and a stale fact repeated for months must not lend that weight to the
        # correction — nor keep it, which is what made it outrank the correction.
        assert current.importance == 1
        # The retired row vacated the claim key its replacement now owns, and
        # kept it as a prefix so the history stays greppable by claim.
        assert current.normalized_key == "api|deploy_host"
        assert stale.normalized_key.startswith("api|deploy_host")
        assert stale.normalized_key != current.normalized_key
        assert len(stale.normalized_key) <= 200

        assert [row.id for row in _active_rows(db, workspace)] == [current.id]
        found = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query="Where is the API deployed?",
        )
        recalled = [item.content for item in found.items]
        assert "Nate deploys the API on Railway." in recalled
        assert not any("Fly.io" in content for content in recalled)
    finally:
        db.rollback()
        db.close()


def test_a_restatement_reinforces_instead_of_churning_a_new_row(workspace, monkeypatch):
    """Rewording is not a correction.

    Same claim key *and* the same significant tokens means the same value said
    differently; superseding there would spend a row and reset importance for a
    comma. A changed token — a host, a weekday, a version — is a new value.
    """
    _lexical_only(monkeypatch)
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        first = _write(
            db, workspace, conversation_id, "Nate deploys the API on Railway.", "api|deploy_host"
        )
        again = _write(
            db,
            workspace,
            conversation_id,
            "  the api, on RAILWAY: nate deploys!!  ",
            "api|deploy_host",
        )
        assert again.id == first.id
        assert first.status == "active"
        assert first.superseded_by is None
        assert first.importance == 2
        assert len(_active_rows(db, workspace)) == 1
    finally:
        db.rollback()
        db.close()


def test_supersession_never_resurrects_a_forgotten_memory(workspace, monkeypatch):
    """A tombstone outranks a restatement of the value it was written for.

    "Stop knowing this" is a stronger instruction than "this changed", and the
    deleted row is the only thing stopping the extractor from writing the fact
    straight back the next time it is mentioned.
    """
    _lexical_only(monkeypatch)
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        stored = _write(
            db, workspace, conversation_id, "Nate deploys the API on Fly.io.", "api|deploy_host"
        )
        memory_service.forget_memories(
            db, workspace_id=workspace, user_id=DEV_SEED_USER_ID, items=[stored]
        )
        assert stored.status == "deleted"

        returned = _write(
            db,
            workspace,
            conversation_id,
            "Nate deploys the API on Fly.io.",
            "api|deploy_host",
        )
        assert returned.id == stored.id
        assert returned.status == "deleted"
        assert returned.superseded_by is None
        assert _active_rows(db, workspace) == []
    finally:
        db.rollback()
        db.close()


def test_forgetting_one_value_does_not_make_the_whole_claim_unlearnable(
    workspace, monkeypatch
):
    """A tombstone names a value, not the slot that value happened to occupy.

    Regression. While the tombstone kept the claim key, `forget` did not remove
    one fact — it permanently blinded the claim: every later deploy host found
    the deleted row and was dropped, silently, forever. Measured on
    evals/memory_corpus.json by forgetting each item's first fact, overall recall
    went 93.3% -> 60.0% with all five knowledge_update answers unlearnable.
    """
    _lexical_only(monkeypatch)
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        stored = _write(
            db, workspace, conversation_id, "Nate deploys the API on Fly.io.", "api|deploy_host"
        )
        memory_service.forget_memories(
            db, workspace_id=workspace, user_id=DEV_SEED_USER_ID, items=[stored]
        )
        # The tombstone gave up the claim key and took its content hash, which is
        # what the key meant before claim keys existed.
        assert stored.normalized_key == memory_service._content_key(
            "Nate deploys the API on Fly.io."
        )

        first = _write(
            db, workspace, conversation_id, "Nate deploys the API on Railway.", "api|deploy_host"
        )
        assert first.status == "active"
        assert first.normalized_key == "api|deploy_host"
        # ...and the slot keeps working as a slot afterwards.
        second = _write(
            db, workspace, conversation_id, "Nate deploys the API on Render.", "api|deploy_host"
        )
        assert first.status == "superseded"
        assert [row.content for row in _active_rows(db, workspace)] == [
            "Nate deploys the API on Render."
        ]
        assert second.status == "active"

        # The forgotten sentence itself is still refused, however the claim moves.
        blocked = _write(
            db, workspace, conversation_id, "Nate deploys the API on Fly.io.", "api|deploy_host"
        )
        assert blocked.id == stored.id
        assert blocked.status == "deleted"
        assert [row.content for row in _active_rows(db, workspace)] == [
            "Nate deploys the API on Render."
        ]
    finally:
        db.rollback()
        db.close()


def test_a_correction_supersedes_across_a_changed_kind(workspace, monkeypatch):
    """`kind` must not be able to defeat supersession.

    Regression. The lookup was scoped by (workspace, kind, key), but `kind` is
    per-turn model output and nothing pins it per claim — MEMORY_EXTRACTION_
    INSTRUCTIONS lets the same claim come back as `fact` one turn and
    `preference` the next. That put the claim and its own correction back into
    one unlabelled list: relabelling only the correcting turn took the measured
    stale-served rate from 0/5 straight back to 5/5 on evals/memory_corpus.json.
    """
    _lexical_only(monkeypatch)
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        stale = _write(
            db,
            workspace,
            conversation_id,
            "Nate uses Recharts for charts.",
            "nate|chart_library",
            kind="preference",
        )
        current = _write(
            db,
            workspace,
            conversation_id,
            "Nate uses Visx for charts.",
            "nate|chart_library",
            kind="fact",
        )
        assert current.id != stale.id
        assert stale.status == "superseded"
        assert stale.superseded_by == current.id
        assert [row.content for row in _active_rows(db, workspace)] == [
            "Nate uses Visx for charts."
        ]
        # A content hash keeps its old, kind-scoped meaning: it identifies one
        # sentence of one kind, and two kinds of it were always two rows.
        first = _write(db, workspace, conversation_id, "Nate ships on Tuesdays.", kind="fact")
        second = _write(
            db, workspace, conversation_id, "Nate ships on Tuesdays.", kind="preference"
        )
        assert first.id != second.id
        assert first.status == "active" and second.status == "active"
    finally:
        db.rollback()
        db.close()


def test_supersession_cannot_reach_across_a_workspace_boundary(client, monkeypatch):
    """Two tenants can hold the same claim key; neither retires the other's row."""
    _lexical_only(monkeypatch)
    db = SessionLocal()
    try:
        mine = Workspace(name="supersede-mine")
        theirs = Workspace(name="supersede-theirs")
        db.add_all([mine, theirs])
        db.flush()
        their_row = _write(
            db, theirs.id, None, "Their API deploys on Fly.io.", "api|deploy_host"
        )
        my_row = _write(db, mine.id, None, "My API deploys on Fly.io.", "api|deploy_host")

        _write(db, mine.id, None, "My API deploys on Railway.", "api|deploy_host")

        assert my_row.status == "superseded"
        assert their_row.status == "active"
        assert their_row.superseded_by is None
        assert their_row.normalized_key == "api|deploy_host"
        assert [row.content for row in _active_rows(db, theirs.id)] == [
            "Their API deploys on Fly.io."
        ]
    finally:
        db.rollback()
        db.close()


def test_supersession_is_ablatable_and_off_reproduces_the_old_write_path(
    workspace, monkeypatch
):
    """MEMORY_SUPERSESSION=0 has to restore the *measured* baseline exactly.

    Both halves move together: content-hash keys and no retirement. Keeping the
    claim key while refusing to supersede would overwrite the older phrasing
    instead of leaving both rows live, which is a third behaviour no number in
    tasks/todo.md describes.
    """
    _lexical_only(monkeypatch)
    settings = get_settings().model_copy(update={"memory_supersession": False})
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        stale = _write(
            db,
            workspace,
            conversation_id,
            "Nate deploys the API on Fly.io.",
            "api|deploy_host",
            settings=settings,
        )
        current = _write(
            db,
            workspace,
            conversation_id,
            "Nate deploys the API on Railway.",
            "api|deploy_host",
            settings=settings,
        )
        assert current.id != stale.id
        assert stale.status == "active"
        assert stale.normalized_key != "api|deploy_host"
        assert current.normalized_key != "api|deploy_host"
        # The bug itself, reproduced on demand: both the claim and its own
        # contradiction come back in one unlabelled list.
        found = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query="Where is the API deployed?",
            settings=settings,
        )
        recalled = [item.content for item in found.items]
        assert "Nate deploys the API on Railway." in recalled
        assert "Nate deploys the API on Fly.io." in recalled
    finally:
        db.rollback()
        db.close()


def test_a_malformed_claim_key_falls_back_to_the_content_hash(workspace, monkeypatch):
    """Claim keys are model output, so an unusable one must cost supersession
    rather than correctness — the content hash collides with nothing."""
    _lexical_only(monkeypatch)
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        first = _write(
            db,
            workspace,
            conversation_id,
            "Nate deploys the API on Fly.io.",
            "the whole sentence, not a slug",
        )
        second = _write(db, workspace, conversation_id, "Nate deploys the API on Railway.")
        assert first.normalized_key == memory_service._content_key(
            "Nate deploys the API on Fly.io."
        )
        assert second.normalized_key == memory_service._content_key(
            "Nate deploys the API on Railway."
        )
        assert first.status == "active" and second.status == "active"
    finally:
        db.rollback()
        db.close()


def test_claim_keys_are_normalized_before_they_are_trusted():
    from app.services.model import normalize_claim_key

    # Forgiving about surface form: one claim must key the same way however the
    # model spells it, or supersession silently stops firing.
    for spelling in ("nate|deploy_host", "Nate | Deploy-Host", " NATE|deploy host "):
        assert normalize_claim_key(spelling) == "nate|deploy_host"
    # Strict about shape: a key with no relation half, several halves, exotic
    # characters or no length limit either merges unrelated claims or poisons the
    # column every other row is looked up by.
    for rejected in ("", "no_pipe", "a|b|c", "__|__", "ünïcode|key", "x" * 80 + "|y"):
        assert normalize_claim_key(rejected) is None
    assert normalize_claim_key(None) is None
    assert normalize_claim_key(42) is None


def test_the_rolling_summary_grows_in_place_rather_than_being_retired(workspace):
    """Supersession is per call site, not global.

    The conversation summary reuses one key on purpose and its text changes on
    every refresh, so retiring it each time would be pure row churn recording a
    correction that never happened.
    """
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        first = memory_service._upsert_item(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            run_id="",
            kind="summary",
            normalized_key=conversation_id,
            content="Conversation topics so far: deployments",
            entity_names=[],
            message_ids=[],
            supersede=False,
        )
        db.flush()
        second = memory_service._upsert_item(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            run_id="",
            kind="summary",
            normalized_key=conversation_id,
            content="Conversation topics so far: deployments; rollbacks",
            entity_names=[],
            message_ids=[],
            supersede=False,
        )
        db.flush()
        assert second.id == first.id
        assert first.status == "active"
        assert first.content.endswith("rollbacks")
    finally:
        db.rollback()
        db.close()


def test_active_is_the_only_liveness_filter_recall_relies_on():
    """The claim in services/memory.py: supersession needed no scoring change.

    That is only true while `_active()` is the single place a query decides what
    is live. A second, open-coded `status == "active"` anywhere in the module
    would be a query that keeps returning retired claims, and every measurement
    of the stale rate would silently stop covering it.
    """
    import inspect
    import re

    # Every function that reads memory back: recall's three candidate queries,
    # the pinned summary, and what `forget` says it would tombstone.
    readers = (
        memory_service.recall,
        memory_service._pinned_summary,
        memory_service._lexical_candidates,
        memory_service._vector_candidates,
        memory_service.resolve_forget_targets,
    )
    for reader in readers:
        source = inspect.getsource(reader)
        assert not re.search(r"MemoryItem\.status\s*[=!]=", source), (
            f"{reader.__name__} decides liveness for itself instead of via _active()"
        )
        assert "_active(" in source, f"{reader.__name__} does not scope through _active()"
    assert "MemoryItem.status ==" in inspect.getsource(memory_service._active)

    # The write path is allowed exactly one: the probe that stops a tombstoned
    # value being written back. Anything else there would be a second liveness
    # rule for reads to disagree with.
    writes = inspect.getsource(memory_service._upsert_item)
    filters = re.findall(r"MemoryItem\.status\s*[=!]=", writes)
    assert len(filters) == 1, f"expected one status filter, found {len(filters)}"
