"""Pages: a thread published as a document whose evidence is pinned.

THE SNAPSHOT DECISION, stated once so nobody has to infer it.
`services/share_links.py`'s header says a share link is a window and not a
snapshot, and every other shared kind honours that — a dashboard re-runs its
query, a document serves its current text, a conversation serves the transcript
as it stands. A page inverts that deliberately, because its entire value is
that a reader following a marker sees the passage the answer was actually
written from. Serving newer text under an old marker would quietly turn a
citation into a claim nobody checked.

Staleness is therefore TOLD rather than hidden: the sweep re-hashes every
frozen citation against the live chunk, sets each one's verdict, flips the
page's own status, and notifies the publisher once on the edge. It never
rewrites the body — a page whose evidence moved is a document somebody shared,
and its readers must keep seeing what its publisher saw.

Two hashes, deliberately different texts:

* `frozen_excerpt` is `chunk.content` and NEVER `retrieval.indexed_text(chunk)`.
  The context prefix is a retrieval aid; the excerpt is provenance, and belongs
  to the author (retrieval.py's stated invariant).
* `content_hash` covers `indexed_text(chunk)`, because that is the text
  `chunks_needing_embedding` compares. A page and a re-embed then agree on what
  "changed" means, and editing only a prefix — which changes what the passage
  retrieves as — is reported rather than silently tolerated.

Nothing here commits. Routes own transactions.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple, cast

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..models import Chunk, Conversation, Message, Page, PageCitation, User
from . import conversations as conversations_service
from . import embedding_generations as generations
from . import embeddings, notifications, retrieval
from .audit import record_audit
from .citations import validate_citations

logger = logging.getLogger(__name__)

#: How stale a page's verdict may get before the sweep re-asks. A day, because
#: drift is a fact about somebody else's editing and nobody is waiting on it.
PAGE_REVALIDATE_INTERVAL = timedelta(hours=24)
#: How many pages one tick re-validates. The tick is shared; a workspace with a
#: thousand pages must not own it.
SWEEP_BATCH = 50
#: The freeze ceiling: how many citations get a stored passage and a drift
#: hash. The body keeps every marker either way — a citation past the cap
#: freezes as "missing" rather than vanishing, so the page never claims
#: provenance it does not hold. See `_over_cap_citation`.
MAX_PAGE_CITATIONS = 200


class PageError(RuntimeError):
    """A publish the route turns into a 404 or a 409, with this message."""

    def __init__(self, message: str, *, status_code: int = 404) -> None:
        super().__init__(message)
        self.status_code = status_code


def publish(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    conversation_id: str,
    title: str,
) -> Page:
    """Freeze a thread into a page. Flushes; the caller commits.

    THE PERSONAL-THREAD DOCTRINE, unchanged: `conversations.resolve_visible` is
    the one within-workspace visibility rule, so a personal thread is
    publishable by its creator alone and a foreign workspace 404s — there is no
    second rule here to disagree with it. The two 409s are verbatim the ones
    `api/share_links._resolve_resource` already makes, for the same reasons.
    """
    conversation = conversations_service.resolve_visible(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise PageError("Conversation not found", status_code=404)
    if conversation.subject_id:
        raise PageError(
            "A subject thread belongs to its subject — publish the document "
            "or dashboard instead",
            status_code=409,
        )
    if conversation.incognito:
        raise PageError("A temporary chat cannot be published", status_code=409)

    messages = list(
        db.scalars(
            select(Message)
            .where(
                Message.workspace_id == workspace_id,
                Message.conversation_id == conversation.id,
            )
            .order_by(Message.created_at, Message.id)
        )
    )
    frozen, renumbered = _freeze(db, workspace_id=workspace_id, messages=messages)
    generation = generations.active_generation(db)
    page = Page(
        workspace_id=workspace_id,
        conversation_id=conversation.id,
        title=title[:200],
        body_md=_body(db, conversation, messages, renumbered),
        published_by=user_id,
        generation_id=generation.id if generation is not None else "",
        status="published",
        drift_count=0,
    )
    db.add(page)
    db.flush()
    for citation in frozen:
        db.add(
            PageCitation(
                workspace_id=workspace_id,
                page_id=page.id,
                marker=citation["marker"],
                chunk_id=citation["chunk_id"],
                source_id=citation["source_id"],
                filename=citation["filename"],
                ordinal=citation["ordinal"],
                frozen_excerpt=citation["frozen_excerpt"],
                content_hash=citation["content_hash"],
                generation_id=page.generation_id,
                status=citation["status"],
            )
        )
    db.flush()
    return page


def _body(
    db: Session,
    conversation: Conversation,
    messages: Sequence[Message],
    renumbered: Dict[str, str],
) -> str:
    """The thread as Markdown, through the EXPORT's renderer rather than a copy.

    `api/chat._render_markdown` already decides how a transcript reads — who is
    labelled what, and that a "/btw" aside says so rather than reading as an
    unanswered ask. A second renderer here would be a second answer to that,
    and the two would drift.

    The assistant messages arrive with their markers already renumbered
    page-wide, on detached copies, so the stored rows are untouched.
    """
    # Deferred: api.chat imports this module's router family at app assembly.
    from ..api.chat import _render_markdown

    sender_ids = {message.created_by for message in messages if message.created_by}
    names: Dict[str, str] = {}
    if sender_ids:
        names = {
            user_id: name
            for user_id, name in db.execute(
                select(User.id, User.name).where(User.id.in_(sender_ids))
            )
        }
    rendered: List[Message] = []
    for message in messages:
        body = renumbered.get(message.id, message.content)
        if body == message.content:
            rendered.append(message)
            continue
        # A transient, never added to the session: the stored transcript keeps
        # its own numbering, which is what a reopened thread has to show.
        copy = Message(
            id=message.id,
            workspace_id=message.workspace_id,
            conversation_id=message.conversation_id,
            run_id=message.run_id,
            created_by=message.created_by,
            role=message.role,
            content=body,
            citations_json=message.citations_json,
        )
        copy.created_at = message.created_at
        rendered.append(copy)
    return _render_markdown(conversation, rendered, names)


def _freeze(
    db: Session, *, workspace_id: str, messages: Sequence[Message]
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Page-wide markers, their frozen passages, and the rewritten bodies.

    Markers are assigned 1..N in first-appearance order across the whole
    thread, because a page is one document and two answers each numbered [1]
    would make the same marker mean two things.

    THE SPLICE IS RIGHT-TO-LEFT and only over IN-RANGE markers. The validator
    deliberately reads any bracketed number in prose as a citation — interval
    notation like "scores in the range [0, 100]" included — so rewriting one of
    those would corrupt the body. Out-of-range and malformed markers are left
    exactly as the author wrote them.
    """
    frozen: List[Dict[str, Any]] = []
    renumbered: Dict[str, str] = {}
    assigned: Dict[str, int] = {}
    for message in messages:
        if message.role != "assistant":
            continue
        try:
            citations = json.loads(message.citations_json or "[]")
        except ValueError:
            citations = []
        if not isinstance(citations, list) or not citations:
            continue
        # This message's own local number -> the page-wide number.
        mapping: Dict[int, int] = {}
        for index, citation in enumerate(citations, start=1):
            if not isinstance(citation, dict):
                continue
            chunk_id = str(citation.get("chunk_id") or "")
            key = chunk_id or f"{message.id}:{index}"
            marker = assigned.get(key)
            if marker is None:
                marker = len(assigned) + 1
                assigned[key] = marker
                frozen.append(
                    _over_cap_citation(marker, citation)
                    if marker > MAX_PAGE_CITATIONS
                    else _frozen_citation(
                        db,
                        workspace_id=workspace_id,
                        marker=marker,
                        citation=citation,
                    )
                )
            mapping[index] = marker
        body = _rewrite_markers(message.content, citations, mapping)
        if body != message.content:
            renumbered[message.id] = body
    return frozen, renumbered


def _over_cap_citation(marker: int, citation: Dict[str, Any]) -> Dict[str, Any]:
    """A marker past the freeze ceiling, recorded as `missing` rather than cut.

    THE CEILING BOUNDS STORED PASSAGES, NOT THE PAGE'S HONESTY. The body keeps
    every marker — they are renumbered page-wide before any cap could apply —
    so dropping the row behind [237] would leave the page asserting provenance
    it does not hold, which is the exact failure the frozen-evidence design
    exists to prevent. "Missing" is the status `_frozen_citation` already uses
    for a vanished chunk, `revalidate` already handles it, and the row carries
    the stored citation payload so a reader still learns which file it named.
    No excerpt, no hash, and no per-citation `Chunk` SELECT: the ceiling still
    does its job of bounding excerpt storage and query fan-out.
    """
    return {
        "marker": marker,
        "chunk_id": str(citation.get("chunk_id") or ""),
        "source_id": str(citation.get("source_id") or ""),
        "filename": str(citation.get("filename") or "")[:255],
        "ordinal": int(citation.get("ordinal") or 0),
        "frozen_excerpt": "",
        "content_hash": "",
        "status": "missing",
    }


def _rewrite_markers(
    answer: str, citations: Sequence[object], mapping: Dict[int, int]
) -> str:
    """Renumber this answer's in-range [n] markers to the page's numbering."""
    report = validate_citations(answer, citations)
    body = answer
    for marker in sorted(report.markers, key=lambda item: item.start, reverse=True):
        if marker.malformed or not marker.numbers:
            continue
        if any(number not in mapping for number in marker.numbers):
            # Out of range for this message's own citation list: prose, not a
            # citation. Leave it exactly as written.
            continue
        replacement = "".join(f"[{mapping[number]}]" for number in marker.numbers)
        body = body[: marker.start] + replacement + body[marker.end :]
    return body


def _frozen_citation(
    db: Session, *, workspace_id: str, marker: int, citation: Dict[str, Any]
) -> Dict[str, Any]:
    """One pinned passage: the author's words, and the hash that detects drift."""
    chunk_id = str(citation.get("chunk_id") or "")
    chunk = (
        db.scalar(
            select(Chunk).where(
                Chunk.id == chunk_id, Chunk.workspace_id == workspace_id
            )
        )
        if chunk_id
        else None
    )
    if chunk is None:
        # A web passage, or a chunk already gone. The stored citation payload is
        # all the provenance that survives, and saying "missing" is more honest
        # than dropping the marker the body still carries.
        return {
            "marker": marker,
            "chunk_id": chunk_id,
            "source_id": str(citation.get("source_id") or ""),
            "filename": str(citation.get("filename") or "")[:255],
            "ordinal": int(citation.get("ordinal") or 0),
            "frozen_excerpt": str(citation.get("excerpt") or ""),
            "content_hash": "",
            "status": "missing",
        }
    return {
        "marker": marker,
        "chunk_id": chunk.id,
        "source_id": chunk.source_id,
        "filename": str(citation.get("filename") or "")[:255],
        "ordinal": chunk.ordinal,
        # The AUTHOR'S words. Never `indexed_text`, whose situating prefix is
        # ours and was never part of what a reader was shown.
        "frozen_excerpt": chunk.content,
        # ...but hashed over the indexed text, which is what a re-embed
        # compares, so a page and the embedder agree on "changed".
        "content_hash": embeddings.content_fingerprint(retrieval.indexed_text(chunk)),
        "status": "frozen",
    }


def revalidate(db: Session, *, page: Page) -> Tuple[int, int, int]:
    """Re-check every pinned citation against the live corpus. (frozen, changed, missing).

    Sets each citation's verdict and the page's own status and count. Writes no
    notification — the sweep owns the edge, because an unconditional ping would
    arrive every day for a page that drifted once.
    """
    moment = utcnow()
    rows = list(
        db.scalars(
            select(PageCitation)
            .where(
                PageCitation.page_id == page.id,
                PageCitation.workspace_id == page.workspace_id,
            )
            .order_by(PageCitation.marker)
        )
    )
    frozen = changed = missing = 0
    for row in rows:
        row.checked_at = moment
        chunk = (
            db.scalar(
                select(Chunk).where(
                    Chunk.id == row.chunk_id,
                    Chunk.workspace_id == page.workspace_id,
                )
            )
            if row.chunk_id
            else None
        )
        if chunk is None:
            row.status = "missing"
            missing += 1
            continue
        digest = embeddings.content_fingerprint(retrieval.indexed_text(chunk))
        if row.content_hash and digest != row.content_hash:
            row.status = "changed"
            changed += 1
            continue
        row.status = "frozen"
        frozen += 1
    page.drift_count = changed + missing
    page.status = "drifted" if page.drift_count else "published"
    page.drift_checked_at = moment
    return (frozen, changed, missing)


def _claim(
    db: Session, page: Page, *, cutoff: datetime, moment: datetime
) -> bool:
    """Take this page's drift check for this sweep, once. True means we won it.

    `watches.claim` transcribed: a conditional UPDATE whose WHERE repeats the
    staleness test the SELECT made, so the loser of a race gets `rowcount == 0`
    and skips the page rather than re-doing it and re-announcing it. It commits
    — a claim that rolled back with the rest of the tick would not be one.
    """
    result = cast(
        "CursorResult[Any]",
        db.execute(
            update(Page)
            .where(
                Page.id == page.id,
                or_(
                    Page.drift_checked_at.is_(None),
                    Page.drift_checked_at < cutoff,
                ),
            )
            .values(drift_checked_at=moment)
        ),
    )
    db.commit()
    won = bool((getattr(result, "rowcount", 0) or 0) == 1)
    if won:
        # The UPDATE went around the ORM, so the in-session copy still holds the
        # old timestamp; `revalidate` overwrites it with its own moment.
        db.refresh(page)
    return won


def sweep(db: Session, *, moment: Optional[datetime] = None) -> List[str]:
    """Re-validate the stalest pages; notify once on each published→drifted edge.

    EDGE-TRIGGERED, exactly as `Monitor.last_state` is: only a page crossing
    from published to drifted writes a notification. A sweep that spoke every
    time would send the same sentence every day until somebody republished, and
    a notification nobody can clear is one people learn to ignore.

    AND THE EDGE IS CLAIMED, `watches.claim`'s idiom exactly. The tick is an
    ordinary HTTP endpoint driven by an external scheduler, so two ticks
    overlap whenever one runs longer than the interval. Without a claim both
    read the same page as `published`, both compute the same crossing, and the
    publisher gets the same notice twice for one edge — the edge trigger
    defeated by the thing it was supposed to make exact. `_claim` is a
    conditional UPDATE on `drift_checked_at`: the winner revalidates (which
    re-stamps it anyway, so the pre-stamp is only a token), the loser moves on.

    Commits nothing except the claim, which must commit to be a claim at all.
    """
    now = moment or utcnow()
    cutoff = now - PAGE_REVALIDATE_INTERVAL
    pages = list(
        db.scalars(
            select(Page)
            .where(
                (Page.drift_checked_at.is_(None))
                | (Page.drift_checked_at < cutoff)
            )
            .order_by(Page.drift_checked_at.asc().nullsfirst(), Page.id)
            .limit(SWEEP_BATCH)
        )
    )
    drifted: List[str] = []
    for page in pages:
        if not _claim(db, page, cutoff=cutoff, moment=now):
            continue
        was_drifted = page.status == "drifted"
        _frozen, changed, missing = revalidate(db, page=page)
        if page.status != "drifted" or was_drifted:
            continue
        drifted.append(page.id)
        notifications.notify(
            db,
            workspace_id=page.workspace_id,
            kind="page_drift",
            # The publisher, not the workspace: they are the one person who can
            # decide whether the page should be republished or withdrawn.
            target_user_id=page.published_by,
            title=f"Evidence changed under “{page.title}”",
            body=(
                f"{changed} cited passage(s) were edited and {missing} are gone. "
                "The page still shows what you published."
            ),
            page_id=page.id,
        )
        record_audit(
            db,
            workspace_id=page.workspace_id,
            actor_id=page.published_by,
            action="page.drift_detected",
            resource_type="page",
            resource_id=page.id,
            detail={"changed": changed, "missing": missing},
        )
    return drifted
