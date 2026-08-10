"""The query-embedding cache in front of recall().

The embedding round-trip is what a recall costs now that scoring is local, so
repeated query text should pay for it once. Everything here is about the two
ways a cache can be wrong rather than slow: serving a vector that belongs to a
different question, and serving one that belongs to a different model.
"""
from __future__ import annotations

import hashlib
from typing import Dict, List, Optional, Sequence, Tuple

import pytest
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.auth import DEV_SEED_USER_ID
from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models import Conversation, MemoryItem, Workspace
from app.services import memory as memory_service
from app.services.embeddings import (
    QUERY_CACHE_MAX_ENTRIES,
    QueryEmbeddingCache,
    pack_vector,
    query_cache_key,
    query_embedding_cache,
)
from app.services.memory import MemoryContext, recall
from app.services.retrieval import tokenize

EMBED_DIM = 8


def _fake_vector(text: str, dim: int = EMBED_DIM) -> bytes:
    """Deterministic stand-in for an embedding model; see test_memory_depth."""
    values = [0.0] * dim
    for token in tokenize(text):
        bucket = int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % dim
        values[bucket] += 1.0
    norm = sum(value * value for value in values) ** 0.5
    if norm:
        values = [value / norm for value in values]
    return pack_vector(values)


class _CountingEmbedder:
    """An `embed_texts` double that records every call it is asked to make.

    The call count is the whole point: a cache hit is only a hit if the provider
    was never reached, and no assertion on the returned vector can show that.
    """

    def __init__(self, vectors: Optional[Dict[str, bytes]] = None) -> None:
        #: Optional model -> vector override, for the model-key test.
        self._by_model = vectors or {}
        self.calls: List[Tuple[str, str]] = []

    def __call__(
        self, texts: Sequence[str], settings: Optional[Settings] = None
    ) -> List[bytes]:
        model = settings.openai_embedding_model if settings else ""
        self.calls.append((texts[0], model))
        override = self._by_model.get(model)
        if override is not None:
            return [override for _ in texts]
        return [_fake_vector(text) for text in texts]


@pytest.fixture
def workspace(client) -> str:
    db = SessionLocal()
    try:
        row = Workspace(name="embedding-cache")
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
    embedding: Optional[bytes] = None,
) -> MemoryItem:
    item = MemoryItem(
        workspace_id=workspace_id,
        kind="fact",
        content=content,
        normalized_key=hashlib.sha256(content.encode()).hexdigest()[:40],
        embedding=embedding if embedding is not None else _fake_vector(content),
    )
    db.add(item)
    db.flush()
    return item


def _shape(context: MemoryContext) -> Tuple[List[Tuple[str, str, str]], List[str]]:
    """Everything about a MemoryContext that reaches the prompt."""
    return (
        [(item.id, item.kind, item.content) for item in context.items],
        list(context.graph_digest),
    )


# --------------------------------------------------------------------------- #
# 1. A hit and a miss must be indistinguishable downstream.
# --------------------------------------------------------------------------- #


def test_cache_hit_returns_the_same_memory_context_as_the_miss(workspace, monkeypatch):
    embedder = _CountingEmbedder()
    monkeypatch.setattr(memory_service, "embed_texts", embedder)
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        for content in (
            "Atlas ring rotation is measured every launch window.",
            "The mailer service retries three times before paging.",
            "Quarterly planning happens in the first week of March.",
        ):
            _seed(db, workspace, content)
        db.commit()

        query = "how is atlas ring rotation measured"
        cold = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query=query,
            settings=get_settings(),
        )
        warm = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query=query,
            settings=get_settings(),
        )

        assert cold.items, "the fixture must actually recall something"
        assert _shape(warm) == _shape(cold)
        assert len(embedder.calls) == 1, "the warm recall re-embedded the query"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 2. The embedding model is part of the identity of a vector.
# --------------------------------------------------------------------------- #


def test_changing_the_embedding_model_does_not_serve_the_old_model_vector(
    workspace, monkeypatch
):
    """Two models, two rankings — with the model out of the key, one wins twice.

    This is the silent one: dimensions often match between model versions, so a
    stale vector does not crash, it just answers a different question.
    """
    alpha_vector = _fake_vector("alpha helix folding")
    beta_vector = _fake_vector("beta sheet stacking")
    embedder = _CountingEmbedder(
        {
            "text-embedding-3-small": alpha_vector,
            "text-embedding-3-large": beta_vector,
        }
    )
    monkeypatch.setattr(memory_service, "embed_texts", embedder)
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        alpha = _seed(db, workspace, "alpha helix folding", embedding=alpha_vector)
        beta = _seed(db, workspace, "beta sheet stacking", embedding=beta_vector)
        db.commit()

        base = get_settings()
        # No lexical overlap with either memory, so ranking is purely semantic.
        query = "which structure question"
        small = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query=query,
            settings=base.model_copy(
                update={"openai_embedding_model": "text-embedding-3-small"}
            ),
        )
        large = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query=query,
            settings=base.model_copy(
                update={"openai_embedding_model": "text-embedding-3-large"}
            ),
        )

        assert [item.id for item in small.items] == [alpha.id]
        assert [item.id for item in large.items] == [beta.id]
        assert len(embedder.calls) == 2
        assert embedder.calls[1][1] == "text-embedding-3-large"
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 3. Normalisation: fold what recall already folds, and nothing more.
# --------------------------------------------------------------------------- #


def test_case_and_whitespace_variants_share_one_cache_entry(workspace, monkeypatch):
    embedder = _CountingEmbedder()
    monkeypatch.setattr(memory_service, "embed_texts", embedder)
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        _seed(db, workspace, "Atlas ring rotation is measured every launch window.")
        db.commit()

        first = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query="Atlas Ring Rotation",
            settings=get_settings(),
        )
        second = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query="  atlas   ring\n rotation ",
            settings=get_settings(),
        )

        assert len(embedder.calls) == 1
        assert [item.id for item in first.items] == [item.id for item in second.items]
    finally:
        db.close()


def test_semantically_different_queries_never_share_an_entry():
    """The queries an over-eager normaliser would collide.

    Stripping stopwords merges a sentence with its negation; sorting or
    deduplicating terms merges sentences with the same words in a different
    order. Both read as different questions to an embedding model, so both must
    key differently.
    """
    model = "text-embedding-3-small"
    for left, right in (
        ("deploy the release on friday", "do not deploy the release on friday"),
        ("does the mailer retry the queue", "does the queue retry the mailer"),
        ("atlas ring rotation", "atlas ring rotation speed"),
    ):
        assert query_cache_key(left, model, "openai") != query_cache_key(
            right, model, "openai"
        ), f"{left!r} and {right!r} must not share a cache entry"
    assert query_cache_key("Atlas  Ring", model, "openai") == query_cache_key(
        "atlas ring", model, "openai"
    )


def test_negated_query_reaches_the_provider_again(workspace, monkeypatch):
    embedder = _CountingEmbedder()
    monkeypatch.setattr(memory_service, "embed_texts", embedder)
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        _seed(db, workspace, "Deploys are frozen on fridays.")
        db.commit()

        for query in ("deploy the release on friday", "do not deploy the release on friday"):
            recall(
                db,
                workspace_id=workspace,
                conversation_id=conversation_id,
                query=query,
                settings=get_settings(),
            )
        assert len(embedder.calls) == 2
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 4. Bounded: the key is user-typed text, so growth is user-driven.
# --------------------------------------------------------------------------- #


def test_cache_evicts_least_recently_used():
    cache = QueryEmbeddingCache(max_entries=3)
    for name in ("a", "b", "c"):
        cache.put(name, name.encode())
    cache.get("a")  # "b" is now the least recently used.
    cache.put("d", b"d")

    assert len(cache) == 3
    assert cache.get("b") is None
    assert cache.get("a") == b"a"
    assert cache.get("d") == b"d"


def test_global_cache_stops_growing_at_its_ceiling():
    for index in range(QUERY_CACHE_MAX_ENTRIES * 2):
        query_embedding_cache.put(
            query_cache_key(
                f"query number {index}", "text-embedding-3-small", "openai"
            ),
            b"\x00\x00\x80\x3f",
        )
    assert len(query_embedding_cache) == QUERY_CACHE_MAX_ENTRIES


# --------------------------------------------------------------------------- #
# 5. A keyless recall must stay lexical, and must not poison a configured one.
# --------------------------------------------------------------------------- #


def test_missing_vector_is_not_cached_and_a_later_run_still_embeds(workspace, monkeypatch):
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        semantic_only = _seed(db, workspace, "alpha helix folding")
        db.commit()
        query = "which structure question"

        # No key to embed with: embed_texts returns None, recall degrades to lexical.
        monkeypatch.setattr(
            memory_service, "embed_texts", lambda texts, settings=None: None
        )
        lexical_only = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query=query,
            settings=get_settings(),
        )
        assert lexical_only.items == []
        assert len(query_embedding_cache) == 0, "a missing vector was cached"

        # Same process, same query, now with a working provider.
        embedder = _CountingEmbedder({"text-embedding-3-small": _fake_vector(
            "alpha helix folding"
        )})
        monkeypatch.setattr(memory_service, "embed_texts", embedder)
        online = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query=query,
            settings=get_settings(),
        )
        assert len(embedder.calls) == 1
        assert [item.id for item in online.items] == [semantic_only.id]
    finally:
        db.close()


def test_scripted_mode_never_serves_a_vector_an_openai_run_cached(
    workspace, monkeypatch
):
    """Lexical-only is a promise, and a cache is not allowed to break it.

    `embed_texts` answers None for any provider but openai, so a scripted recall
    must score lexically — see the test below, which holds it to that against a
    real key. If the cache key
    ignored the provider, that recall would read the vector an earlier openai-mode
    call left under the same query text and start ranking semantically — a mode
    silently behaving like the other one, which is worse than either.
    """
    only_vector = _fake_vector("alpha helix folding")
    monkeypatch.setattr(
        memory_service, "embed_texts", lambda texts, settings=None: [only_vector]
    )
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        semantic_only = _seed(db, workspace, "alpha helix folding")
        db.commit()
        base = get_settings()
        # No lexical overlap: any hit at all can only have come from a vector.
        query = "which structure question"

        online = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query=query,
            settings=base.model_copy(
                update={"model_provider": "openai", "openai_api_key": "sk-test"}
            ),
        )
        assert [item.id for item in online.items] == [semantic_only.id]

        # Same process, same query text, a provider that cannot embed at all.
        monkeypatch.setattr(
            memory_service, "embed_texts", lambda texts, settings=None: None
        )
        lexical_only = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query=query,
            settings=base.model_copy(update={"model_provider": "scripted"}),
        )
        assert lexical_only.items == [], "a lexical-only recall was served a cached vector"
    finally:
        db.close()


def test_scripted_mode_never_reaches_the_embedding_api_even_with_a_key(monkeypatch):
    """The test double must not make a network call, key present or not.

    This is the real `embed_texts`, not a stand-in: every other test in this file
    patches it out, so nothing else here can see what it actually does. Gating it
    on `has_openai_key` instead of the provider looked equivalent — Settings
    refuse to boot `openai` without a key, so "no key" and "not openai" seemed to
    coincide. They do not. A developer running MODEL_PROVIDER=scripted still has
    OPENAI_API_KEY in .env, so the key-only gate sent the entire suite (and the
    browser e2e server, which runs the same double) to the live embeddings API:
    billed, non-deterministic, and broken on a plane.
    """
    import openai

    from app.services import embeddings as embeddings_service

    def _explode(*args, **kwargs):
        raise AssertionError("the scripted double tried to construct an OpenAI client")

    monkeypatch.setattr(openai, "OpenAI", _explode)
    scripted = get_settings().model_copy(
        update={
            "model_provider": "scripted",
            "openai_api_key": SecretStr("sk-a-real-looking-key"),
        }
    )
    assert scripted.has_openai_key, "the fixture must carry a key, or it proves nothing"
    assert embeddings_service.embed_texts(["anything at all"], scripted) is None


def test_provider_failure_is_not_cached(workspace, monkeypatch):
    """A timeout is a fact about the network, not about the query text."""

    def _boom(texts, settings=None):
        raise RuntimeError("provider down")

    monkeypatch.setattr(memory_service, "embed_texts", _boom)
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        _seed(db, workspace, "Atlas ring rotation is measured every launch window.")
        db.commit()
        found = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query="atlas ring rotation",
            settings=get_settings(),
        )
        assert [item.content for item in found.items], "lexical recall must survive"
        assert len(query_embedding_cache) == 0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 6. A blank prompt never had anything to embed.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("query", ["", "   \n\t "])
def test_blank_query_skips_the_embedding_round_trip(workspace, monkeypatch, query):
    embedder = _CountingEmbedder()
    monkeypatch.setattr(memory_service, "embed_texts", embedder)
    db = SessionLocal()
    try:
        conversation_id = _conversation(db, workspace)
        _seed(db, workspace, "Atlas ring rotation is measured every launch window.")
        db.commit()

        found = recall(
            db,
            workspace_id=workspace,
            conversation_id=conversation_id,
            query=query,
            settings=get_settings(),
        )
        assert embedder.calls == [], "a blank prompt paid for an embedding"
        assert found.items == []
    finally:
        db.close()
