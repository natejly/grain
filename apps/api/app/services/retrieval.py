"""Document retrieval: a BM25 arm, a dense arm, and RRF over the two.

Three stages, each switchable on its own (`retrieval_bm25`, `retrieval_hybrid`,
`retrieval_contextual`) so each can be ablated and measured rather than believed.
Every stage degrades downward instead of failing: with no term index the
pre-BM25 scorer still ranks, with no vectors the lexical arm still ranks alone,
and with no situating blurb the author's own text is what gets indexed.

The one invariant that outranks all of it: an `Evidence.excerpt` is always
`Chunk.content`. The context prefix is indexed and embedded but never quoted,
because a citation has to show the author's words rather than our summary of them.
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from ..config import Settings, get_settings
from ..models import Chunk, ChunkTerm, Source
from .embeddings import (
    embed_texts,
    query_cache_key,
    query_embedding_cache,
    ranked_cosine_scores,
)

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,}")
STOP_WORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "can",
    "could",
    "for",
    "from",
    "have",
    "how",
    "into",
    "its",
    "more",
    "that",
    "the",
    "their",
    "this",
    "was",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
    "you",
    "your",
}

# `chunk_terms.term` is VARCHAR(64); a token longer than that is truncated on the
# way in, so the query path has to truncate identically or a long token would be
# looked up under a spelling the index never stored.
MAX_TERM_CHARS = 64
# A long prompt tokenizes to hundreds of terms and both engines plan a
# several-hundred-value IN badly, so one query only gets to look up this many.
# *Which* ones is the whole question: see `_selective_terms`.
MAX_QUERY_TERMS = 12
# Okapi BM25's standard parameters. k1 bounds how much repeating a term can help,
# b how hard length normalisation bites; these are the values every hybrid
# baseline in the literature uses, and moving them is its own experiment.
BM25_K1 = 1.2
BM25_B = 0.75
# How many chunks one call will lazily index. The index is derived data, so a
# batch that missed gets picked up by the next search rather than lost.
RECONCILE_BATCH = 500
# Chunks per embedding request. Small enough that one provider hiccup costs one
# batch, large enough that a 500-chunk document is a handful of round-trips.
EMBED_BATCH = 128


@dataclass(frozen=True)
class Evidence:
    chunk_id: str
    source_id: str
    filename: str
    ordinal: int
    excerpt: str
    score: float


@dataclass(frozen=True)
class ArmRankings:
    """What each arm ranked, before fusion. Kept separable for measurement.

    If both arms find a passage, fusion taught you nothing; the interesting
    questions are the ones exactly one arm found, and that is only visible if the
    rankings survive the fusion that combines them (RESEARCH.md §4.3).
    """

    lexical: List[Tuple[str, float]] = field(default_factory=list)
    dense: List[Tuple[str, float]] = field(default_factory=list)


def tokenize(value: str) -> List[str]:
    return [
        token.lower()
        for token in TOKEN_RE.findall(value)
        if token.lower() not in STOP_WORDS
    ]


def index_terms(value: str) -> Counter[str]:
    """Term frequencies as they are stored, i.e. after column-width truncation."""
    return Counter(token[:MAX_TERM_CHARS] for token in tokenize(value))


def query_terms(query: str) -> List[str]:
    """Every distinct content term in a query, truncated as the index stores them.

    Uncapped. The cap belongs to `bm25_ranking`, which is the only caller that has
    a corpus to decide *which* terms are worth keeping; a cap applied here would
    have to guess, and the guess was wrong (see `_selective_terms`).
    """
    return list(index_terms(query))


def indexed_text(chunk: Chunk) -> str:
    """The text retrieval matches against: the situating blurb plus the content.

    Not what gets cited. Contextual Retrieval works by giving a chunk back the
    document context that chunking took away, and that only helps if the blurb is
    in the indexed and embedded text — but it must stay out of the excerpt, which
    is provenance and belongs to the author.
    """
    prefix = (chunk.context_prefix or "").strip()
    return f"{prefix}\n\n{chunk.content}" if prefix else chunk.content


# --- indexing ---------------------------------------------------------------


def index_chunks(db: Session, chunks: Sequence[Chunk]) -> None:
    """Rewrite the term postings for these chunks. The caller owns the commit.

    Deletes first because this runs on re-ingest and on backfill, where a chunk
    already has postings describing text it no longer holds.
    """
    if not chunks:
        return
    # Ids are assigned at flush, and a chunk created moments ago in this session
    # has none yet — the postings would then reference None.
    db.flush()
    chunk_ids = [chunk.id for chunk in chunks]
    db.execute(delete(ChunkTerm).where(ChunkTerm.chunk_id.in_(chunk_ids)))
    for chunk in chunks:
        counts = index_terms(indexed_text(chunk))
        # Length in *indexed* terms, not words: BM25's b normalisation compares
        # this against the corpus average, so it has to count the same units the
        # scorer sums over. 0 is a legitimate value (a chunk of pure stopwords);
        # None is the "never indexed" watermark and must not be left behind.
        chunk.lexical_length = sum(counts.values())
        for term, tf in counts.items():
            db.add(
                ChunkTerm(
                    workspace_id=chunk.workspace_id,
                    chunk_id=chunk.id,
                    term=term,
                    tf=tf,
                )
            )


def clear_source_postings(db: Session, source_id: str) -> None:
    """Drop the postings of every chunk of a source. The caller owns the commit.

    Postings reference chunks, so anything that deletes chunks has to delete these
    first — on SQLite an orphan is a slow storage leak, and on Postgres the
    foreign key makes it an outright failure to delete the source.
    """
    db.execute(
        delete(ChunkTerm).where(
            ChunkTerm.chunk_id.in_(select(Chunk.id).where(Chunk.source_id == source_id))
        )
    )


def reconcile_index(
    db: Session, *, workspace_id: str, limit: int = RECONCILE_BATCH
) -> int:
    """Index chunks that have never been indexed. Returns how many were done.

    The term index is derived data, and this is what makes that true rather than
    aspirational: chunks written before the index existed, and chunks written by
    any path that forgets to call `index_chunks`, become searchable on the next
    search instead of silently ranking nowhere. In steady state this costs one
    indexed lookup returning no rows.

    Flushes rather than commits. The caller's transaction carries the postings —
    they are visible to the queries that follow either way — and a commit here
    would land whatever else that caller had in flight.
    """
    pending = list(
        db.scalars(
            select(Chunk)
            .where(Chunk.workspace_id == workspace_id, Chunk.lexical_length.is_(None))
            .order_by(Chunk.created_at, Chunk.id)
            .limit(limit)
        )
    )
    if not pending:
        return 0
    index_chunks(db, pending)
    db.flush()
    return len(pending)


def embed_chunks(
    db: Session, chunks: Sequence[Chunk], settings: Optional[Settings] = None
) -> int:
    """Attach vectors to chunks. Best-effort by design; returns how many landed.

    A chunk is retrievable the moment it is written, and it must stay retrievable
    in the window before this runs and if the provider is down — `embed_texts`
    answers None when there is no key to reach it with, and neither that nor an
    exception is a reason to fail an ingest or lose a document.
    """
    settings = settings or get_settings()
    pending = [chunk for chunk in chunks if chunk.content.strip()]
    if not pending:
        return 0
    model = settings.openai_embedding_model
    embedded = 0
    for start in range(0, len(pending), EMBED_BATCH):
        batch = pending[start : start + EMBED_BATCH]
        try:
            vectors = embed_texts([indexed_text(chunk) for chunk in batch], settings)
        except Exception:
            logger.warning("chunk embedding failed; retrieval stays lexical", exc_info=True)
            continue
        if not vectors:
            # None means "no provider configured", which is a mode, not a failure,
            # and will not improve on the next batch.
            break
        # strict=False: a short response from an external API should embed fewer
        # chunks, not lose the whole batch.
        for chunk, vector in zip(batch, vectors, strict=False):
            chunk.embedding = vector
            chunk.embedding_model = model
            embedded += 1
    return embedded


# --- the two arms -----------------------------------------------------------


def _live_sources() -> Tuple[ColumnElement[bool], ...]:
    """The filter every arm shares: only ready, undeleted sources are citable."""
    return (Source.deleted_at.is_(None), Source.status == "ready")


def legacy_lexical_ranking(
    db: Session, *, workspace_id: str, query: str
) -> List[Tuple[str, float]]:
    """The pre-BM25 scorer, kept as the ablation arm for `RETRIEVAL_BM25=0`.

    Term frequency with no IDF — a match on a rare proper noun and a match on
    "storage" weigh the same — and it re-tokenizes every chunk in the workspace in
    Python per query. It is here to be measured against, not to be used.
    """
    terms = Counter(tokenize(query))
    if not terms:
        return []
    rows = db.execute(
        select(Chunk)
        .join(Source, Source.id == Chunk.source_id)
        .where(
            Chunk.workspace_id == workspace_id,
            Source.workspace_id == workspace_id,
            *_live_sources(),
        )
    ).all()
    scored: List[Tuple[str, float]] = []
    for (chunk,) in rows:
        chunk_terms = Counter(tokenize(indexed_text(chunk)))
        overlap = set(terms) & set(chunk_terms)
        if not overlap:
            continue
        score = sum(
            (1.0 + math.log(1 + chunk_terms[term])) * (1.0 + terms[term] * 0.25)
            for term in overlap
        )
        score /= max(1.0, math.sqrt(len(chunk_terms)))
        scored.append((chunk.id, score))
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored


def _document_frequencies(
    db: Session,
    *,
    workspace_id: str,
    terms: Sequence[str],
    live: Tuple[ColumnElement[bool], ...],
) -> Dict[str, int]:
    """How many live chunks contain each term. Index-only on (workspace_id, term)."""
    return {
        str(term): int(count)
        for term, count in db.execute(
            select(ChunkTerm.term, func.count(func.distinct(ChunkTerm.chunk_id)))
            .join(Chunk, Chunk.id == ChunkTerm.chunk_id)
            .join(Source, Source.id == Chunk.source_id)
            .where(ChunkTerm.workspace_id == workspace_id, ChunkTerm.term.in_(terms), *live)
            .group_by(ChunkTerm.term)
        ).all()
    }


def _selective_terms(
    db: Session,
    *,
    workspace_id: str,
    terms: Sequence[str],
    live: Tuple[ColumnElement[bool], ...],
) -> Tuple[List[str], Optional[Dict[str, int]]]:
    """The `MAX_QUERY_TERMS` rarest query terms, and their exact document frequency.

    Selectivity is a property of the corpus, not of the string. The first version
    of this kept the *longest* terms on the theory that a long token is a rare one,
    and that is simply not true of English: a prompt carrying "infrastructure",
    "documentation" and "authentication" alongside "kestrel" keeps the three
    boilerplate words and drops the proper noun, so BM25 never sees the one term
    its IDF would have weighted highest. Measured on a nine-chunk corpus, that put
    the answer outside the top five while the pre-BM25 scorer it replaced — which
    had no cap and so used every term — ranked it first.

    So ask the index instead. The aggregate is an index-only group-by on
    (workspace_id, term), and it hands back the exact document frequency for the
    terms it selects, which `bm25_ranking` needs anyway — the extra query pays for
    itself by removing the one that would otherwise follow.

    Only runs when the query is over the cap; a short query keeps the fast path.
    """
    if len(terms) <= MAX_QUERY_TERMS:
        return list(terms), None
    frequencies = _document_frequencies(
        db, workspace_id=workspace_id, terms=terms, live=live
    )
    # A term nothing contains contributes no postings and no score, so it must not
    # occupy a slot despite being, technically, the rarest term in the query.
    present = [term for term in terms if frequencies.get(term)]
    # Rarest first; length then spelling only to make ties deterministic across
    # backends, which is what keeps two runs of the eval comparable.
    present.sort(key=lambda term: (frequencies[term], -len(term), term))
    kept = present[:MAX_QUERY_TERMS]
    return kept, {term: frequencies[term] for term in kept}


def bm25_ranking(
    db: Session, *, workspace_id: str, query: str, settings: Optional[Settings] = None
) -> List[Tuple[str, float]]:
    """Okapi BM25 over the portable inverted index.

    Three things BM25 needs that the old scorer had none of: IDF, so a rare term
    outweighs a common one; saturating tf, so ten mentions are not ten times one;
    and length normalisation against the corpus average rather than a bare sqrt.

    The SQL is a term-restricted join, so the work is proportional to how many
    chunks contain the query's terms rather than to the size of the workspace.
    """
    settings = settings or get_settings()
    terms = query_terms(query)
    if not terms:
        return []

    live = (
        Chunk.workspace_id == workspace_id,
        Source.workspace_id == workspace_id,
        *_live_sources(),
    )
    totals = db.execute(
        select(func.count(Chunk.id), func.coalesce(func.sum(Chunk.lexical_length), 0))
        .join(Source, Source.id == Chunk.source_id)
        .where(*live, Chunk.lexical_length.is_not(None))
    ).one()
    doc_count = int(totals[0] or 0)
    if doc_count == 0:
        return []
    # 1.0 rather than 0 when the corpus indexes to nothing: it only ever divides.
    average_length = float(totals[1] or 0) / doc_count or 1.0

    terms, exact_frequency = _selective_terms(
        db, workspace_id=workspace_id, terms=terms, live=live
    )
    if not terms:
        return []

    postings = db.execute(
        select(ChunkTerm.chunk_id, ChunkTerm.term, ChunkTerm.tf, Chunk.lexical_length)
        .join(Chunk, Chunk.id == ChunkTerm.chunk_id)
        .join(Source, Source.id == Chunk.source_id)
        .where(ChunkTerm.workspace_id == workspace_id, ChunkTerm.term.in_(terms), *live)
        # Highest tf first so that if the cap does bite, what survives is the rows
        # that would have scored highest anyway.
        .order_by(ChunkTerm.tf.desc(), ChunkTerm.chunk_id)
        .limit(settings.retrieval_posting_limit)
    ).all()
    if not postings:
        return []

    if exact_frequency is not None:
        # Selecting the terms already cost an exact count; reuse it.
        document_frequency: Dict[str, int] = exact_frequency
    elif len(postings) < settings.retrieval_posting_limit:
        # Uncapped: every posting for these terms is in hand, so document
        # frequency is exact without asking the database a second time.
        document_frequency = Counter(term for _, term, _, _ in postings)
    else:
        # Capped: counting the rows we kept would overstate every IDF, so pay for
        # the aggregate. Only reachable on a term matching 20k+ chunks.
        document_frequency = _document_frequencies(
            db, workspace_id=workspace_id, terms=terms, live=live
        )

    idf = {
        term: math.log(
            1.0 + (doc_count - count + 0.5) / (count + 0.5)
        )
        for term, count in document_frequency.items()
    }
    scores: Dict[str, float] = defaultdict(float)
    for chunk_id, term, tf, length in postings:
        weight = idf.get(term)
        if weight is None:
            continue
        norm = BM25_K1 * (1.0 - BM25_B + BM25_B * (float(length or 0) / average_length))
        scores[str(chunk_id)] += weight * (tf * (BM25_K1 + 1.0)) / (tf + norm)
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return ranked


def _embed_query(query: str, settings: Settings) -> Optional[bytes]:
    """This turn's query vector, from the LRU when we already paid for it.

    Shares the process-wide cache with memory recall on purpose: `search_evidence`
    runs on the raw prompt before every turn *and* again whenever the agent calls
    `search_sources`, and recall embeds that same prompt. One round-trip of
    ~50-200ms is the entire added latency of the dense arm, so paying it three
    times for one turn's identical text is the whole cost, tripled, for nothing.
    """
    if not query.strip():
        return None
    key = query_cache_key(
        query, settings.openai_embedding_model, settings.active_model_provider
    )
    cached = query_embedding_cache.get(key)
    if cached is not None:
        return cached
    vectors = embed_texts([query], settings)
    if not vectors:
        return None
    query_embedding_cache.put(key, vectors[0])
    return vectors[0]


def dense_ranking(
    db: Session, *, workspace_id: str, query: str, settings: Optional[Settings] = None
) -> List[Tuple[str, float]]:
    """Cosine ranking over chunk vectors, or an empty ranking when there are none.

    Empty is the honest answer in four different situations — no provider, a
    provider that failed, a corpus not embedded yet, and a query nothing in the
    corpus is actually close to — and all of them must leave the caller ranking
    lexically rather than ranking nothing.

    That last one is what `retrieval_dense_floor` is for. Cosine is almost never
    exactly zero, so an unfiltered dense arm ranks the whole workspace for every
    query and "we have nothing on that" stops being an outcome retrieval can
    reach. For a product whose answers are citations, five weak passages are
    worse than none.
    """
    settings = settings or get_settings()
    try:
        query_blob = _embed_query(query, settings)
    except Exception:
        logger.warning("retrieval degraded to lexical-only: embedding failed", exc_info=True)
        return []
    if not query_blob:
        return []
    rows = db.execute(
        select(Chunk.id, Chunk.embedding)
        .join(Source, Source.id == Chunk.source_id)
        .where(
            Chunk.workspace_id == workspace_id,
            Source.workspace_id == workspace_id,
            *_live_sources(),
            Chunk.embedding.is_not(None),
            # Vectors from two embedding models are not comparable, and same-width
            # vectors from different models are the case the length guard cannot
            # catch. Cheaper to exclude them in SQL than to score them wrongly.
            Chunk.embedding_model == settings.openai_embedding_model,
        )
        .order_by(Chunk.created_at.desc(), Chunk.id)
        .limit(settings.retrieval_vector_candidate_cap)
    ).all()
    ranked = ranked_cosine_scores(
        [(str(row_id), blob) for row_id, blob in rows], query_blob
    )
    floor = settings.retrieval_dense_floor
    # Sorted descending, so the first score below the floor ends the ranking.
    for position, (_chunk_id, score) in enumerate(ranked):
        if score < floor:
            return ranked[:position]
    return ranked


def rank_arms(
    db: Session, *, workspace_id: str, query: str, settings: Optional[Settings] = None
) -> ArmRankings:
    """Both arms, unfused. The eval harness calls this for per-arm attribution."""
    settings = settings or get_settings()
    if settings.retrieval_bm25:
        # Anything unindexed ranks nowhere at all, which is a worse failure than a
        # slow first query.
        reconcile_index(db, workspace_id=workspace_id)
        lexical = bm25_ranking(
            db, workspace_id=workspace_id, query=query, settings=settings
        )
    else:
        lexical = legacy_lexical_ranking(db, workspace_id=workspace_id, query=query)
    dense = (
        dense_ranking(db, workspace_id=workspace_id, query=query, settings=settings)
        if settings.retrieval_hybrid
        else []
    )
    return ArmRankings(lexical=lexical, dense=dense)


# --- fusion -----------------------------------------------------------------


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[Tuple[str, float]]], *, k: int, depth: int
) -> List[Tuple[str, float]]:
    """RRF: score = Σ 1/(k + rank), over each arm's own ranking.

    Rank-only, and that is the entire point. A BM25 score is an unbounded sum of
    IDF terms and a cosine is bounded in [0, 1]; any weighted blend of the two is
    really a hidden guess about their relative scales, and the guess changes with
    corpus size. Ranks have no scale to get wrong.

    With one non-empty arm this reproduces that arm's order exactly, so the
    ablations stay honest: switching the dense arm off changes what is retrieved
    only by removing what the dense arm contributed.
    """
    fused: Dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, (item_id, _score) in enumerate(ranking[:depth], start=1):
            fused[item_id] += 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda item: (-item[1], item[0]))


def search_evidence(
    db: Session,
    *,
    workspace_id: str,
    query: str,
    limit: int = 5,
    token_budget: int = 1200,
    settings: Optional[Settings] = None,
) -> List[Evidence]:
    """The passages worth citing for this query, best first."""
    settings = settings or get_settings()
    if not query_terms(query):
        # Nothing but stopwords: no term to look up and nothing a vector could
        # mean either. The dense arm would happily rank the whole corpus by
        # similarity to "what is the", and cite the top five — evidence pinned to
        # a prompt that asked for none is worse than no evidence at all.
        return []
    arms = rank_arms(db, workspace_id=workspace_id, query=query, settings=settings)
    fused = reciprocal_rank_fusion(
        [arms.lexical, arms.dense],
        k=settings.retrieval_rrf_k,
        depth=settings.retrieval_fusion_depth,
    )
    if not fused:
        return []

    # Enough candidates that the (source, ordinal) tiebreak below has something to
    # sort, without materialising a ranking the caller cannot use.
    candidates = {chunk_id: score for chunk_id, score in fused[: max(limit * 4, 20)]}
    rows = db.execute(
        select(Chunk, Source)
        .join(Source, Source.id == Chunk.source_id)
        .where(
            # Re-asserted rather than trusted: the ids arrive from two arms and a
            # fusion, and workspace scoping is not something to infer.
            Chunk.workspace_id == workspace_id,
            Source.workspace_id == workspace_id,
            Chunk.id.in_(list(candidates)),
            *_live_sources(),
        )
    ).all()
    ordered = sorted(
        rows,
        key=lambda row: (-candidates[row[0].id], row[0].source_id, row[0].ordinal),
    )

    evidence: List[Evidence] = []
    used_tokens = 0
    for chunk, source in ordered:
        if len(evidence) >= limit:
            break
        remaining = token_budget - used_tokens
        if remaining <= 0:
            break
        # `content`, never `indexed_text`: the excerpt is what the answer quotes.
        words = chunk.content.split()
        excerpt = " ".join(words[:remaining])
        used_tokens += len(excerpt.split())
        evidence.append(
            Evidence(
                chunk_id=chunk.id,
                source_id=source.id,
                filename=source.filename,
                ordinal=chunk.ordinal,
                excerpt=excerpt,
                score=round(candidates[chunk.id], 6),
            )
        )
    return evidence
