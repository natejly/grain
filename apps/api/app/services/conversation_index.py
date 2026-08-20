"""Past conversations, chunked and summarized, so an agent can quote them.

`Message` rows persist every word of every thread, but until this module nothing
could search them across threads — the only cross-conversation channel was the
handful of MemoryItems the extractor distills per run. This is the other half:
verbatim transcript windows (`kind="chunk"`) plus one rolling LLM summary per
thread (`kind="summary"`), stored in `conversation_chunks` and searched by the
`search_conversations` tool. Summaries answer "which past conversation is
relevant"; chunks answer "what exactly was said" — and the agent gets the
words, not a paraphrase.

Two invariants outrank everything else here:

1. **A chunk is exactly as visible as its conversation.** Every read joins
   `Conversation` and applies `_visible`, which mirrors
   `conversations.resolve_visible` clause for clause. A search tool that
   ignored the personal/shared gate would re-open, for transcripts, exactly
   the leak commit ffa0608 closed for memory. `_visible` is the only place
   the rule is spelled, and a structural test asserts every query goes
   through it.

2. **Everything here is derived data.** Indexing is best-effort after a run,
   self-healing at search time (`reconcile`), and rebuildable in bulk
   (scripts/backfill_conversation_index.py). No failure in this module may
   cost a run its answer or a thread its transcript.

Retrieval is memory-recall-shaped rather than document-retrieval-shaped on
purpose: a LIKE prefilter for the lexical arm (no postings table to keep
consistent) and one cosine matmul for the dense arm, fused with RRF. The
corpus per workspace is chat-sized, and memory.py's measurements of the LIKE
scan (~24ms at 100k rows) transfer directly.
"""
from __future__ import annotations

import json
import logging
import operator
from dataclasses import dataclass
from datetime import datetime
from functools import reduce
from typing import List, Optional, Sequence, Tuple

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..database import SessionLocal
from ..models import Conversation, ConversationChunk, Message, Run
from .embeddings import (
    embed_texts,
    query_cache_key,
    query_embedding_cache,
    ranked_cosine_scores,
)
from .ingestion import make_chunks
from .model import summarize_conversation
from .retrieval import query_terms, reciprocal_rank_fusion
from .usage import usage_scope

logger = logging.getLogger(__name__)

#: Target size of one transcript window. Larger than a document chunk (900):
#: chat turns carry their context in the exchange, so a window that holds a
#: question AND its answer quotes far better than either alone.
CHUNK_TARGET_CHARS = 1200
#: A single message longer than this is split by `make_chunks` into windows of
#: its own rather than packed with neighbours — an unbounded assistant answer
#: must not produce an unbounded chunk.
MAX_SINGLE_MESSAGE_CHARS = 2000
#: Refresh the thread summary every this-many new messages — the same cadence
#: memory's naive topics line uses, for the same reason: a summary that lags a
#: few turns is fine, an LLM call per message is not.
SUMMARY_REFRESH_EVERY = 10
#: Same cap and same longest-first heuristic as memory recall's lexical arm.
MAX_LEXICAL_TERMS = 12
#: How many rows the dense arm may add to the fusion. Memory's shortlist value.
VECTOR_SHORTLIST = 64
#: Conversations one search will lazily reindex. Bounded like retrieval's
#: RECONCILE_BATCH: the index is derived data, so anything this pass misses is
#: picked up by the next search rather than lost.
RECONCILE_CONVERSATIONS = 5


@dataclass(frozen=True)
class ConversationHit:
    """One search result: a verbatim quote (or thread summary) with provenance."""

    conversation_id: str
    title: str
    #: "quote" for a transcript window, "summary" for a thread's rolling summary.
    kind: str
    content: str
    #: When the newest quoted message was said — what the agent cites as a date.
    spoken_at: datetime
    score: float


# --- visibility -------------------------------------------------------------


def _visible(stmt, workspace_id: str, viewer_id: str):  # type: ignore[no-untyped-def]
    """The one visibility rule for conversation chunks, in one place.

    Mirrors `conversations.resolve_visible` clause for clause: within the
    caller's workspace — that filter is NEVER removed, on either table — a
    chunk is readable when its conversation is the viewer's own, OR shared, OR
    a subject thread. The chokepoint pattern is memory's `_active`, and like
    there a structural test asserts this is the module's only gate, so a new
    query path cannot quietly skip the join.
    """
    return stmt.join(
        Conversation, Conversation.id == ConversationChunk.conversation_id
    ).where(
        ConversationChunk.workspace_id == workspace_id,
        Conversation.workspace_id == workspace_id,  # NEVER removed
        or_(
            Conversation.created_by == viewer_id,
            Conversation.shared.is_(True),
            Conversation.subject_id != "",
        ),
    )


# --- indexing ---------------------------------------------------------------


def _speaker(role: str) -> str:
    return "User" if role == "user" else "Assistant"


def _windows(
    messages: Sequence[Message],
) -> List[Tuple[str, List[str], datetime, int]]:
    """Pack messages into transcript windows: (content, message_ids, last_at, count).

    Whole messages, in order, packed to `CHUNK_TARGET_CHARS` — a window is a
    slice of the exchange, so a message never spans two windows unless it alone
    exceeds `MAX_SINGLE_MESSAGE_CHARS`, in which case `make_chunks` splits it
    into windows that all carry that one message's id. The final window is
    written even when short: messages are immutable and already-covered ones
    are never repacked, so a partial window now beats re-chunking the thread
    on every run.
    """
    windows: List[Tuple[str, List[str], datetime, int]] = []
    parts: List[str] = []
    part_ids: List[str] = []
    part_last: Optional[datetime] = None
    part_count = 0

    def flush() -> None:
        nonlocal parts, part_ids, part_last, part_count
        if parts and part_last is not None:
            windows.append(("\n".join(parts), part_ids, part_last, part_count))
        parts, part_ids, part_last, part_count = [], [], None, 0

    for message in messages:
        body = message.content.strip()
        if not body:
            continue
        text = f"{_speaker(message.role)}: {body}"
        if len(text) > MAX_SINGLE_MESSAGE_CHARS:
            flush()
            for _start, _end, piece in make_chunks(
                text, target_chars=CHUNK_TARGET_CHARS
            ):
                windows.append((piece, [message.id], message.created_at, 1))
            continue
        if parts and sum(len(part) for part in parts) + len(text) > CHUNK_TARGET_CHARS:
            flush()
        parts.append(text)
        part_ids.append(message.id)
        part_last = message.created_at
        part_count += 1
    flush()
    return windows


def _naive_summary(messages: Sequence[Message]) -> str:
    """The offline summary: the thread's user lines, as memory's topics row.

    This is what `summarize_conversation` degrades to — scripted mode, a
    provider outage, an empty answer — so a thread always HAS a summary row and
    the difference a real model makes stays measurable against a fixed
    baseline rather than against nothing.
    """
    user_lines = [
        " ".join(message.content.split())[:120]
        for message in messages
        if message.role == "user"
    ][:8]
    return ("Conversation topics so far: " + "; ".join(user_lines))[:900]


def _refresh_summary(
    db: Session,
    conversation: Conversation,
    messages: Sequence[Message],
    summary: Optional[ConversationChunk],
    settings: Settings,
) -> Optional[ConversationChunk]:
    """Bring the thread's one summary row up to date; return it if it changed."""
    total = len(messages)
    if total < SUMMARY_REFRESH_EVERY:
        return None
    if summary is not None and total - summary.message_count < SUMMARY_REFRESH_EVERY:
        return None
    transcript = "\n".join(
        f"{_speaker(message.role)}: {' '.join(message.content.split())[:400]}"
        for message in messages
        if message.content.strip()
    )
    with usage_scope(workspace_id=conversation.workspace_id):
        content = summarize_conversation(
            transcript, summary.content if summary is not None else "", settings=settings
        )
    if not content:
        content = _naive_summary(messages)
    if summary is None:
        summary = ConversationChunk(
            workspace_id=conversation.workspace_id,
            conversation_id=conversation.id,
            kind="summary",
            ordinal=0,
        )
        db.add(summary)
    summary.content = content
    summary.message_count = total
    summary.last_message_at = messages[-1].created_at
    summary.message_ids_json = json.dumps([message.id for message in messages[-4:]])
    # The old vector describes the old text; better none than a stale one.
    summary.embedding = None
    summary.embedding_model = ""
    return summary


def _embed_pending(rows: Sequence[ConversationChunk], settings: Settings) -> None:
    """Attach vectors, best-effort: rows stay lexically searchable regardless."""
    pending = [row for row in rows if row.embedding is None and row.content.strip()]
    if not pending:
        return
    try:
        with usage_scope(workspace_id=pending[0].workspace_id):
            vectors = embed_texts([row.content for row in pending], settings)
    except Exception:
        logger.warning("conversation chunk embedding failed; search stays lexical", exc_info=True)
        return
    if vectors is None:
        return
    # strict=False: a short response from an external API should embed fewer
    # rows, not lose them all.
    for row, vector in zip(pending, vectors, strict=False):
        row.embedding = vector
        row.embedding_model = settings.openai_embedding_model


def index_conversation(
    db: Session, conversation: Conversation, settings: Optional[Settings] = None
) -> int:
    """Index this conversation's uncovered messages; returns new windows written.

    Incremental by construction: messages are immutable and append-only, so a
    message inside any existing window is covered forever and only the
    uncovered tail is packed. Flushes rather than commits — the caller owns
    the transaction, like every other indexer here.
    """
    settings = settings or get_settings()
    if not settings.conversation_index_enabled:
        return 0
    messages = list(
        db.scalars(
            select(Message)
            .where(
                Message.workspace_id == conversation.workspace_id,
                Message.conversation_id == conversation.id,
            )
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
    )
    if not messages:
        return 0
    existing = list(
        db.scalars(
            select(ConversationChunk).where(
                ConversationChunk.workspace_id == conversation.workspace_id,
                ConversationChunk.conversation_id == conversation.id,
            )
        )
    )
    summary = next((row for row in existing if row.kind == "summary"), None)
    covered = {
        message_id
        for row in existing
        if row.kind == "chunk"
        for message_id in json.loads(row.message_ids_json)
    }
    next_ordinal = max(
        (row.ordinal for row in existing if row.kind == "chunk"), default=-1
    ) + 1

    touched: List[ConversationChunk] = []
    pending = [message for message in messages if message.id not in covered]
    for content, message_ids, last_at, count in _windows(pending):
        row = ConversationChunk(
            workspace_id=conversation.workspace_id,
            conversation_id=conversation.id,
            kind="chunk",
            ordinal=next_ordinal,
            content=content,
            message_ids_json=json.dumps(message_ids),
            message_count=count,
            last_message_at=last_at,
        )
        db.add(row)
        touched.append(row)
        next_ordinal += 1

    refreshed = _refresh_summary(db, conversation, messages, summary, settings)
    if refreshed is not None:
        touched.append(refreshed)

    if touched:
        db.flush()
        _embed_pending(touched, settings)
    return sum(1 for row in touched if row.kind == "chunk")


def update_conversation_index(run_id: str) -> None:
    """Index a completed run's conversation. Best-effort by design.

    The sibling of `write_conversation_memory`, with the same contract: it runs
    after the answer is already delivered, so nothing it does may fail the run
    — but a silent failure is how a workspace quietly stops being searchable,
    so it logs on the way down.
    """
    settings = get_settings()
    if not settings.conversation_index_enabled:
        return
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if run is None or not run.conversation_id:
            return
        conversation = db.get(Conversation, run.conversation_id)
        if conversation is None or conversation.workspace_id != run.workspace_id:
            return
        index_conversation(db, conversation, settings)
        db.commit()
    except Exception:
        logger.warning(
            "conversation index was not updated for run %s", run_id, exc_info=True
        )
        db.rollback()
    finally:
        db.close()


def reconcile(
    db: Session,
    *,
    workspace_id: str,
    settings: Optional[Settings] = None,
    limit: int = RECONCILE_CONVERSATIONS,
) -> int:
    """Index conversations whose transcript has outgrown their chunks.

    The self-healing pass that makes the post-run hook an optimisation rather
    than a requirement: threads written by paths without the hook (crons, the
    /tool endpoints, anything that predates the feature) become searchable on
    the next search instead of never. Newest-first, bounded, flushes only.

    The staleness test compares timestamps (`max(Message.created_at)` against
    `max(last_message_at)`), which could in principle miss a second message
    sharing the newest one's timestamp — but `index_conversation` itself
    covers by message id, exactly, so a timestamp tie can only delay a
    conversation's turn here, never mis-index it.

    Indexing is deliberately viewer-blind: writing chunks for another member's
    personal thread stores derived data under that thread's own visibility;
    `_visible` decides who reads it back.
    """
    settings = settings or get_settings()
    if not settings.conversation_index_enabled:
        return 0
    newest_message = (
        select(
            Message.conversation_id.label("conversation_id"),
            func.max(Message.created_at).label("newest"),
        )
        .where(Message.workspace_id == workspace_id)
        .group_by(Message.conversation_id)
        .subquery()
    )
    newest_chunk = (
        select(
            ConversationChunk.conversation_id.label("conversation_id"),
            func.max(ConversationChunk.last_message_at).label("indexed"),
        )
        .where(ConversationChunk.workspace_id == workspace_id)
        .group_by(ConversationChunk.conversation_id)
        .subquery()
    )
    stale = list(
        db.scalars(
            select(Conversation)
            .join(newest_message, newest_message.c.conversation_id == Conversation.id)
            .outerjoin(newest_chunk, newest_chunk.c.conversation_id == Conversation.id)
            .where(
                Conversation.workspace_id == workspace_id,
                or_(
                    newest_chunk.c.indexed.is_(None),
                    newest_chunk.c.indexed < newest_message.c.newest,
                ),
            )
            .order_by(newest_message.c.newest.desc())
            .limit(limit)
        )
    )
    written = 0
    for conversation in stale:
        written += index_conversation(db, conversation, settings)
    if written:
        db.flush()
    return written


# --- search -----------------------------------------------------------------


def _like_pattern(term: str) -> str:
    r"""A LIKE pattern matching `term` literally — `_` and `%` escaped, as in
    memory's lexical arm, so `read_only` cannot match `readXonly`."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _lexical_ranking(
    db: Session,
    *,
    workspace_id: str,
    viewer_id: str,
    terms: Sequence[str],
    settings: Settings,
) -> List[Tuple[str, float]]:
    """Chunk ids containing query terms, most-overlapping first.

    memory.py's prefilter, retargeted: LIKE is a C-level scan on both engines
    and `lower()` keeps SQLite and Postgres returning the same rows. The
    truncation orders by the score's own dominant term (overlap), so what the
    limit keeps is what the fusion would have ranked highest anyway.
    """
    if not terms:
        return []
    hits = [
        func.lower(ConversationChunk.content).like(_like_pattern(term), escape="\\")
        for term in terms
    ]
    overlap = reduce(operator.add, (case((hit, 1), else_=0) for hit in hits))
    stmt = (
        _visible(
            select(ConversationChunk.id, overlap.label("overlap")),
            workspace_id,
            viewer_id,
        )
        .where(or_(*hits))
        .order_by(
            overlap.desc(),
            ConversationChunk.last_message_at.desc(),
            ConversationChunk.id,
        )
        .limit(settings.conversation_lexical_candidate_limit)
    )
    return [(str(row_id), float(count)) for row_id, count in db.execute(stmt).all()]


def _embed_query(query: str, settings: Settings) -> Optional[bytes]:
    """This search's query vector, via the process-wide LRU both other
    retrieval paths share — the same prompt text is often embedded by memory
    recall and document retrieval in the same turn."""
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


def _dense_ranking(
    db: Session,
    *,
    workspace_id: str,
    viewer_id: str,
    query: str,
    settings: Settings,
) -> List[Tuple[str, float]]:
    """Cosine ranking over chunk vectors, floored, or empty — and empty must
    leave the caller ranking lexically, never ranking nothing."""
    try:
        with usage_scope(workspace_id=workspace_id):
            query_blob = _embed_query(query, settings)
    except Exception:
        logger.warning(
            "conversation search degraded to lexical-only: embedding failed",
            exc_info=True,
        )
        return []
    if not query_blob:
        return []
    stmt = _visible(
        select(ConversationChunk.id, ConversationChunk.embedding),
        workspace_id,
        viewer_id,
    ).where(
        ConversationChunk.embedding.is_not(None),
        # Same-width vectors from a different model are the case the length
        # guard cannot catch; exclude them in SQL as document retrieval does.
        ConversationChunk.embedding_model == settings.openai_embedding_model,
    )
    cap = settings.conversation_vector_candidate_cap
    if cap > 0:
        stmt = stmt.order_by(ConversationChunk.last_message_at.desc()).limit(cap)
    rows = [(str(row_id), blob) for row_id, blob in db.execute(stmt).all()]
    ranked = ranked_cosine_scores(rows, query_blob)[:VECTOR_SHORTLIST]
    floor = settings.retrieval_dense_floor
    # Sorted descending, so the first score below the floor ends the ranking.
    for position, (_row_id, score) in enumerate(ranked):
        if score < floor:
            return ranked[:position]
    return ranked


def search_conversation_chunks(
    db: Session,
    *,
    workspace_id: str,
    viewer_id: str,
    query: str,
    limit: Optional[int] = None,
    settings: Optional[Settings] = None,
) -> List[ConversationHit]:
    """The past-conversation passages worth quoting for this query, best first."""
    settings = settings or get_settings()
    if not settings.conversation_index_enabled:
        return []
    if limit is None:
        limit = settings.conversation_search_limit
    all_terms = query_terms(query)
    if not all_terms:
        # Nothing but stopwords: no term to match and nothing a vector could
        # mean — the same refusal `search_evidence` makes, for the same reason.
        return []
    # Anything unindexed ranks nowhere at all, which is a worse failure than a
    # slower first search.
    reconcile(db, workspace_id=workspace_id, settings=settings)
    terms = sorted(all_terms, key=lambda term: (-len(term), term))[:MAX_LEXICAL_TERMS]
    lexical = _lexical_ranking(
        db,
        workspace_id=workspace_id,
        viewer_id=viewer_id,
        terms=terms,
        settings=settings,
    )
    dense = _dense_ranking(
        db,
        workspace_id=workspace_id,
        viewer_id=viewer_id,
        query=query,
        settings=settings,
    )
    fused = reciprocal_rank_fusion(
        [lexical, dense],
        k=settings.retrieval_rrf_k,
        depth=settings.retrieval_fusion_depth,
    )
    if not fused:
        return []
    candidates = {chunk_id: score for chunk_id, score in fused[: max(limit * 4, 20)]}
    rows = db.execute(
        # Visibility re-asserted rather than trusted: the ids arrive from two
        # arms and a fusion, and the gate is not something to infer.
        _visible(
            select(ConversationChunk, Conversation.title), workspace_id, viewer_id
        ).where(ConversationChunk.id.in_(list(candidates)))
    ).all()
    ordered = sorted(
        rows,
        key=lambda row: (-candidates[row[0].id], row[0].conversation_id, row[0].ordinal),
    )
    hits: List[ConversationHit] = []
    for chunk, title in ordered[:limit]:
        hits.append(
            ConversationHit(
                conversation_id=chunk.conversation_id,
                title=str(title),
                kind="summary" if chunk.kind == "summary" else "quote",
                content=chunk.content,
                spoken_at=chunk.last_message_at,
                score=round(candidates[chunk.id], 6),
            )
        )
    return hits
