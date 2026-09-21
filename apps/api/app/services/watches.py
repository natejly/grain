"""Watch-and-brief: a standing question about a source or a space.

The scheduling half is `services/crons.py` with `Watch` in place of `Cron`, and
deliberately so — every safety property is inherited rather than re-argued:

**It is a claim, not a trigger.** `watches.last_dispatched_at` is advanced by a
conditional UPDATE, so a tick replayed, retried or delivered to three instances
at once produces one check per watch per minute.

**It cannot reach into the past.** `CATCHUP` covers the just-missed minute and
nothing older, so a day of downtime does not produce a day of briefs.

**It grants nothing.** A watch reads chunks and writes a memory, a document and
a notification. It runs no tools and starts no turn, so there is no policy
scope for it to escape from.

The work half is new, and three decisions in it are load-bearing:

**No change, no noise.** An unchanged fingerprint writes an observation row
(evidence the watch ran) and stops: no extraction, no memory, no ping, no brief
revision. A watch that pings on no change is a watch people turn off.

**THE OWNER RULE.** `owner_id=SHARED_OWNER` ('') is written ONLY when
`watch.shared` is True. A watch on a personal-scope target writes to its
creator's own shelf, because a memory must be exactly as visible as the thing
it was learned from (ADR 0010). Getting this backwards would publish one
person's reading to the whole workspace, silently.

**The graph is DERIVED, never hand-written.** `rebuild_graph` projects entities
out of chunks and active memory items, so a `GraphEntity` row written here
would be clobbered on the next rebuild. The watch calls `mark_graph_stale` and
lets the projection do its job.

Nothing here commits except `claim`, which must commit to be a claim at all.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple, cast

from sqlalchemy import or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..config import Settings, get_settings
from ..database import SessionLocal
from ..models import (
    SHARED_OWNER,
    Chunk,
    Document,
    DocumentVersion,
    MemoryItem,
    Source,
    Watch,
    WatchObservation,
)
from . import embeddings, graph, memory, model, notifications
from .audit import record_audit
from .model import normalize_claim_key
from .usage import usage_scope
from .workflows import schedule
from .workflows.validate import cron_matches

logger = logging.getLogger(__name__)

#: One minute of grace, the same window the workflow ticker uses.
CATCHUP = schedule.CATCHUP

#: How much of the delta one extraction call reads. A watch fires on a
#: schedule, so an unbounded delta would be an unbounded bill.
MAX_DELTA_CHARS = 8000
#: How many observations the regenerated brief carries.
BRIEF_OBSERVATIONS = 20
#: Field values are one sentence, not an essay.
MAX_FIELD_CHARS = 300

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def claim_key(watch_id: str, field: str) -> str:
    """The claim key one watch's one field owns, exactly as it will be STORED.

    THE CLAIM KEY IS THE SUPERSESSION: one watch's one field is one claim, so
    this week's value retires last week's through `memory._retire`, inside its
    own owner+space scope, and the memory row count is bounded by declared
    fields rather than by how often the watch fires.

    It runs through `normalize_claim_key` here rather than being composed by
    hand, because that is the function `apply_extracted_memories` will run it
    through anyway — a key spelled one way here and stored another way there
    would supersede nothing, and nothing would report it.

    TOTAL, NEVER None. The server composes this key itself, so it must not
    depend on the user's typography: `normalize_claim_key` only maps
    `[\\s\\-.:/]` to `_`, so "price (USD)", "% change" or "CEO's name" fail its
    pattern. Falling back to a content hash there (the model-supplied-key rule)
    would cost supersession on a key we fully control — every fire writing a
    NEW active row, so an hourly watch on "price (USD)" accumulates hundreds of
    mutually contradictory live facts, silently, and the "bounded by fields,
    not by fires" invariant the ten-field cap rests on would be false. So an
    unsluggable field name is digested instead: hex is `[a-z0-9]`, so the
    second normalisation always validates, and the digest is stable across
    reorderings and edits to the watch's other fields (an index would not be).
    """
    key = normalize_claim_key(f"watch:{watch_id}|{field}")
    if key is not None:
        return key
    digest = hashlib.sha1(field.strip().casefold().encode("utf-8")).hexdigest()[:16]
    fallback = normalize_claim_key(f"watch:{watch_id}|f_{digest}")
    # Unreachable: the left half is a uuid and the right half is `f_` + hex.
    assert fallback is not None
    return fallback


def slugifies(field: str) -> bool:
    """Whether this field name keeps its READABLE claim key.

    The boundary uses it to 422 a field that would only ever be addressable as
    a digest, so a person typing "price (USD)" is told while they still hold
    the form rather than finding an unreadable memory key a month later.
    """
    return normalize_claim_key(f"watch:{'0' * 36}|{field}") is not None


@dataclass(frozen=True)
class WatchDelta:
    """What moved under a watch since the last check."""

    added: List[str]
    removed: List[str]
    text: str


# --- scheduling (services/crons.py, transcribed) ----------------------------


def due(watch: Watch, *, moment: datetime) -> bool:
    """Is this watch scheduled to fire in the minute `moment` falls in?"""
    if not watch.enabled:
        return False
    local = schedule.local_moment(moment, watch.schedule_timezone)
    if local is None:
        logger.warning(
            "watch %s has an unknown timezone %r; not dispatching",
            watch.id,
            watch.schedule_timezone,
        )
        return False
    return cron_matches(watch.schedule_cron, local)


def claim(db: Session, watch: Watch, *, minute: datetime) -> bool:
    """Advance `last_dispatched_at` to `minute`, once. True means we won it.

    The whole of the at-most-once guarantee is this one conditional UPDATE — an
    exact mirror of `crons.claim`. The loser of a race gets `rowcount == 0` and
    moves on.
    """
    result = cast(
        "CursorResult[Any]",
        db.execute(
            update(Watch)
            .where(
                Watch.id == watch.id,
                or_(
                    Watch.last_dispatched_at.is_(None),
                    Watch.last_dispatched_at < minute,
                ),
            )
            .values(last_dispatched_at=minute)
        ),
    )
    db.commit()
    return bool((getattr(result, "rowcount", 0) or 0) == 1)


def _firing_minute(watch: Watch, now: datetime) -> Optional[datetime]:
    """The minute this watch is due for, or None. `crons._firing_minute`."""
    for offset in range(int(CATCHUP.total_seconds() // 60) + 1):
        candidate = now - timedelta(minutes=offset)
        if (
            watch.last_dispatched_at is not None
            and candidate <= watch.last_dispatched_at
        ):
            break
        if due(watch, moment=candidate):
            return candidate
    return None


def dispatch_due(
    db: Session, *, moment: Optional[datetime] = None, settings: Optional[Settings] = None
) -> List[str]:
    """CLAIM every enabled watch due now, and return the ids to check.

    Claim inline, check on a background task — the split subscriptions,
    webhook deliveries and digests all make in the same tick, and for the same
    reason (the F5 QA note: no more heavy inline work in the shared tick). The
    claim is what must happen in the request, because it is a conditional
    UPDATE that has to commit to be a claim at all; the check is not, and it is
    the expensive half: `fingerprint` scans every live chunk under the target
    and `extract_watch_fields` is a live provider round-trip with a 60-second
    timeout. Twenty workspaces picking "0 9 * * *" would otherwise serialise
    twenty of each inside one HTTP request and time the external cron out.

    Returns the ids it claimed — the id names a watch this tick is responsible
    for checking, not a verdict. `check_claimed` is the other half; a caller
    that wants both in one place (a test, `run-now`) calls `check` directly.
    """
    settings = settings or get_settings()
    now = schedule.floor_minute(moment or utcnow())
    candidates = list(
        db.scalars(
            select(Watch).where(
                Watch.enabled.is_(True),
                or_(
                    Watch.last_dispatched_at.is_(None),
                    Watch.last_dispatched_at < now,
                ),
            )
        )
    )
    claimed: List[str] = []
    for watch in candidates:
        minute = _firing_minute(watch, now)
        if minute is None:
            continue
        if not claim(db, watch, minute=minute):
            continue
        claimed.append(watch.id)
    db.commit()
    return claimed


def check_claimed(watch_id: str) -> None:
    """Background-task entry point for one claimed watch. Owns its session.

    `digest_service.send_digest`'s shape exactly: the claim already committed
    in the request, so this runs outside it and must open its own Session. A
    watch that raises is logged and swallowed — the claim is spent either way,
    and the next fire is the retry. Re-reads the row rather than taking the
    object across the session boundary, and re-checks `enabled` because a
    person may have turned the watch off between the claim and here.
    """
    db = SessionLocal()
    try:
        watch = db.scalar(select(Watch).where(Watch.id == watch_id))
        if watch is None or not watch.enabled:
            return
        check(db, watch=watch)
        db.commit()
    except Exception:
        logger.warning("watch %s raised while checking", watch_id, exc_info=True)
        db.rollback()
    finally:
        db.close()


# --- the work ---------------------------------------------------------------


def fingerprint(
    db: Session, *, workspace_id: str, target_kind: str, target_id: str
) -> Tuple[str, List[str]]:
    """A digest of the target's live chunks, and their ids in order.

    Deterministic by construction: the ordering is total, and each chunk
    contributes its id and a hash of its own content. Two checks over an
    unchanged target produce the same digest, which is the only reason "no
    change" is a fact rather than a guess.

    A SPACE target scans every live source in the space. That is a full chunk
    scan per fire, bounded by the once-a-minute claim; the fix, if it measures
    badly, is a per-source watermark and never a sampled fingerprint — a sample
    would miss a change, which is the one thing a watch may not do.
    """
    predicates = [
        Chunk.workspace_id == workspace_id,
        Source.workspace_id == workspace_id,
        Source.deleted_at.is_(None),
        Source.status == "ready",
    ]
    if target_kind == "source":
        predicates.append(Chunk.source_id == target_id)
    elif target_kind == "space":
        predicates.append(Source.space_id == target_id)
    else:
        return ("", [])
    rows = list(
        db.execute(
            select(Chunk.id, Chunk.content)
            .join(Source, Source.id == Chunk.source_id)
            .where(*predicates)
            .order_by(Source.id, Chunk.ordinal, Chunk.id)
        )
    )
    digest = hashlib.sha256(
        "\n".join(
            f"{chunk_id}:{embeddings.content_fingerprint(content)}"
            for chunk_id, content in rows
        ).encode("utf-8")
    ).hexdigest()
    return (digest, [str(chunk_id) for chunk_id, _content in rows])


def delta(db: Session, *, watch: Watch, chunk_ids: Sequence[str]) -> WatchDelta:
    """What arrived and what left, and the text of what arrived."""
    try:
        previous = json.loads(watch.last_chunk_ids_json or "[]")
    except ValueError:
        previous = []
    before = {item for item in previous if isinstance(item, str)}
    now = list(chunk_ids)
    added = [chunk_id for chunk_id in now if chunk_id not in before]
    removed = [chunk_id for chunk_id in before if chunk_id not in set(now)]
    text = ""
    if added:
        contents = list(
            db.scalars(
                select(Chunk.content)
                .where(
                    Chunk.workspace_id == watch.workspace_id,
                    Chunk.id.in_(added),
                )
                .order_by(Chunk.source_id, Chunk.ordinal, Chunk.id)
            )
        )
        text = "\n\n".join(contents)[:MAX_DELTA_CHARS]
    return WatchDelta(added=added, removed=sorted(removed), text=text)


def deterministic_extract(delta_text: str, fields: Sequence[str]) -> Dict[str, str]:
    """The scripted path's whole implementation, and a pure function.

    For each declared field, the first sentence that mentions it. Crude on
    purpose: the point is that CI and the eval gate exercise a real extraction
    path with a knowable answer, not that this rivals a model.
    """
    sentences = [
        sentence.strip()
        for sentence in _SENTENCE_SPLIT.split(delta_text or "")
        if sentence.strip()
    ]
    extracted: Dict[str, str] = {}
    for field in fields:
        needle = field.casefold()
        value = ""
        for sentence in sentences:
            if needle in sentence.casefold():
                value = sentence[:MAX_FIELD_CHARS]
                break
        extracted[field] = value
    return extracted


def check(
    db: Session, *, watch: Watch, settings: Optional[Settings] = None
) -> WatchObservation:
    """Run the watch once. Flushes an observation either way; never commits."""
    settings = settings or get_settings()
    moment = utcnow()
    digest, chunk_ids = fingerprint(
        db,
        workspace_id=watch.workspace_id,
        target_kind=watch.target_kind,
        target_id=watch.target_id,
    )
    if digest == watch.last_fingerprint:
        # NO CHANGE, NO NOISE. The row is the evidence that the watch ran and
        # found nothing, which is a different fact from the watch not running.
        observation = WatchObservation(
            workspace_id=watch.workspace_id,
            watch_id=watch.id,
            fingerprint=digest,
            changed=False,
            summary="No change.",
            chunk_ids_json=json.dumps(chunk_ids),
        )
        db.add(observation)
        watch.last_checked_at = moment
        db.flush()
        return observation

    moved = delta(db, watch=watch, chunk_ids=chunk_ids)
    declared = declared_fields(watch)
    extracted: Dict[str, str] = {}
    if declared and moved.text:
        with usage_scope(
            workspace_id=watch.workspace_id, user_id=watch.created_by
        ):
            extracted = model.extract_watch_fields(
                moved.text,
                declared,
                user_id=watch.created_by,
                settings=settings,
            )

    memory_ids: List[str] = []
    if extracted:
        items = memory.apply_extracted_memories(
            db,
            workspace_id=watch.workspace_id,
            conversation_id=None,
            run_id="",
            # ONE WATCH'S ONE FIELD IS ONE CLAIM; see `claim_key`.
            extracted=_memory_items(watch, extracted),
            message_ids=[],
            settings=settings,
            # THE OWNER RULE. '' (SHARED_OWNER) only when the watch was
            # explicitly shared with the workspace.
            owner_id=(SHARED_OWNER if watch.shared else watch.created_by),
            space_id=watch.space_id,
        )
        memory_ids = [item.id for item in items]
        # The KG is a PROJECTION. Writing GraphEntity rows here would be
        # clobbered by the next `rebuild_graph`, which derives entities from
        # chunks and active memory items; marking it stale is how a new fact
        # reaches the graph.
        graph.mark_graph_stale(db, watch.workspace_id)

    summary = (
        f"{len(moved.added)} passage(s) added, {len(moved.removed)} removed."
    )
    observation = WatchObservation(
        workspace_id=watch.workspace_id,
        watch_id=watch.id,
        fingerprint=digest,
        added_chunks=len(moved.added),
        removed_chunks=len(moved.removed),
        changed=True,
        summary=summary,
        fields_json=json.dumps(extracted),
        memory_ids_json=json.dumps(memory_ids),
        chunk_ids_json=json.dumps(chunk_ids),
    )
    db.add(observation)
    db.flush()

    refresh_brief(db, watch=watch)
    notifications.notify(
        db,
        workspace_id=watch.workspace_id,
        kind="watch_change",
        # A shared watch speaks to the room; a personal one speaks to its
        # owner. The same axis the memory write above follows, for the same
        # reason: an announcement must be exactly as wide as what it is about.
        target_user_id=("" if watch.shared else watch.created_by),
        title=f"{watch.name} changed",
        body=summary,
        watch_id=watch.id,
        document_id=watch.brief_document_id,
    )
    record_audit(
        db,
        workspace_id=watch.workspace_id,
        actor_id=watch.created_by,
        action="watch.change_detected",
        resource_type="watch",
        resource_id=watch.id,
        detail={
            "added": len(moved.added),
            "removed": len(moved.removed),
            "fields": sorted(extracted),
        },
    )
    watch.last_fingerprint = digest
    watch.last_chunk_ids_json = json.dumps(chunk_ids)
    watch.last_checked_at = moment
    watch.last_change_at = moment
    db.flush()
    return observation


def _memory_items(watch: Watch, extracted: Dict[str, str]) -> List[Dict[str, object]]:
    """One extraction item per non-empty field, each carrying its claim key."""
    items: List[Dict[str, object]] = []
    for field, value in sorted(extracted.items()):
        if not value:
            continue
        items.append(
            {
                "kind": "fact",
                "content": f"{field}: {value}",
                "entities": [],
                # Always present: `claim_key` is total, so every field
                # supersedes its own previous value rather than falling back to
                # a content hash and stacking a new active row per fire.
                "normalized_key": claim_key(watch.id, field),
            }
        )
    return items


def declared_fields(watch: Watch) -> List[str]:
    """This watch's declared extraction fields, as stored."""
    try:
        parsed = json.loads(watch.extraction_schema_json or "[]")
    except ValueError:
        return []
    return [str(field) for field in parsed if str(field).strip()]


def retire_claims(db: Session, *, watch: Watch, owner_id: str) -> int:
    """Retire every fact this watch wrote under `owner_id`. Returns the count.

    Called when a watch's `shared` flag flips, because the flip MOVES the
    scope this watch writes into: `check` picks `owner_id` from the flag at
    fire time, and `memory._upsert_item` matches owner AND space EXACTLY. The
    rows left behind in the old scope are therefore unreachable by every future
    supersession — a shared→personal flip would leave last week's price on the
    workspace shelf, `active`, recalled into every member's turns, with no
    watch claiming it and no fire able to correct it, forever.

    Retiring rather than re-owning is the honest move: the row was published to
    a scope that has just been revoked, and `_retire` preserves it for audit
    while taking it out of recall. No replacement id — nothing replaced it.
    """
    fields = declared_fields(watch)
    if not fields:
        return 0
    keys = [claim_key(watch.id, field) for field in fields]
    rows = list(
        db.scalars(
            select(MemoryItem).where(
                MemoryItem.workspace_id == watch.workspace_id,
                MemoryItem.owner_id == owner_id,
                MemoryItem.space_id == watch.space_id,
                MemoryItem.status == "active",
                MemoryItem.normalized_key.in_(keys),
            )
        )
    )
    memory.retire_items(db, rows)
    return len(rows)


def refresh_brief(db: Session, *, watch: Watch) -> Document:
    """Rewrite the watch's standing brief from its newest observations.

    REGENERATED WHOLE rather than appended to, because a brief assembled by
    appending drifts from the observations it claims to summarise the first
    time one is corrected. A `DocumentVersion` snapshot is taken before every
    rewrite, through the same seam an agent edit uses, so the brief's history
    is undoable like any other document's.
    """
    observations = list(
        db.scalars(
            select(WatchObservation)
            .where(
                WatchObservation.watch_id == watch.id,
                WatchObservation.workspace_id == watch.workspace_id,
            )
            .order_by(
                WatchObservation.created_at.desc(), WatchObservation.id.desc()
            )
            .limit(BRIEF_OBSERVATIONS)
        )
    )
    body = _brief_body(watch, observations)
    document: Optional[Document] = None
    if watch.brief_document_id:
        document = db.scalar(
            select(Document).where(
                Document.id == watch.brief_document_id,
                Document.workspace_id == watch.workspace_id,
            )
        )
    if document is None:
        document = Document(
            workspace_id=watch.workspace_id,
            title=f"Brief: {watch.name}"[:200],
            kind="markdown",
            content=body,
            folder_id="",
            created_by=watch.created_by,
        )
        db.add(document)
        db.flush()
        watch.brief_document_id = document.id
        return document
    db.add(
        DocumentVersion(
            workspace_id=watch.workspace_id,
            document_id=document.id,
            content=document.content,
            summary=f"Watch “{watch.name}” refreshed the brief"[:300],
            created_by=watch.created_by,
        )
    )
    document.content = body
    db.flush()
    return document


def _brief_body(watch: Watch, observations: Sequence[WatchObservation]) -> str:
    """The brief, from a fixed template. Pure, so a test can pin it."""
    last_change = watch.last_change_at.isoformat() + "Z" if watch.last_change_at else "—"
    lines = [
        f"# {watch.name}",
        "",
        f"_Watching {watch.target_kind} — last change {last_change}_",
    ]
    for observation in observations:
        lines.append("")
        lines.append(f"## {observation.created_at.isoformat()}Z")
        lines.append("")
        lines.append(observation.summary)
        try:
            fields = json.loads(observation.fields_json or "{}")
        except ValueError:
            fields = {}
        if isinstance(fields, dict) and fields:
            lines.append("")
            lines.append("| Field | Value |")
            lines.append("| --- | --- |")
            for field, value in sorted(fields.items()):
                lines.append(f"| {field} | {value} |")
    return "\n".join(lines) + "\n"
