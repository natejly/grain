from __future__ import annotations

import hashlib
import json
import logging
import operator
from dataclasses import dataclass, field
from functools import reduce, wraps
from typing import (
    Any,
    Callable,
    Concatenate,
    Dict,
    List,
    Optional,
    ParamSpec,
    Sequence,
    Tuple,
    TypeVar,
)

from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer

from ..clock import utcnow
from ..config import Settings, get_settings
from ..database import SessionLocal
from ..models import (
    SHARED_OWNER,
    Conversation,
    EmbeddingGeneration,
    EmbeddingVector,
    GraphEdge,
    GraphEntity,
    MemoryItem,
    Message,
    Run,
    new_id,
)
from . import embedding_generations as generations
from .audit import record_audit
from .embeddings import (
    DEFAULT_DTYPE,
    content_fingerprint,
    embed_batch,
    query_cache_key,
    query_embedding_cache,
    ranked_cosine_scores,
)
from .graph import extract_entities, mark_graph_stale, name_candidates
from .model import extract_memories, normalize_claim_key
from .retrieval import tokenize
from .usage import usage_scope

logger = logging.getLogger(__name__)

MAX_GRAPH_DIGEST_LINES = 10
SUMMARY_REFRESH_EVERY = 10

# How many vector candidates survive the matmul into the final rescoring pass.
VECTOR_SHORTLIST = 64
# A long prompt can tokenize to hundreds of terms; both SQLite and Postgres plan
# a several-hundred-clause OR badly. The longest tokens are the most selective,
# so they are the ones worth spending clauses on.
MAX_LEXICAL_TERMS = 12

_SelectT = TypeVar("_SelectT", bound=Select[Any])


@dataclass
class MemoryContext:
    items: List[MemoryItem] = field(default_factory=list)
    graph_digest: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.items and not self.graph_digest


def _content_key(content: str) -> str:
    digest = hashlib.sha256(content.casefold().encode()).hexdigest()
    return digest[:40]


# MemoryItem.normalized_key is String(200); Postgres enforces that and SQLite
# does not, so both keys and retirement suffixes are cut to fit here.
MAX_NORMALIZED_KEY = 200


def _claim_key(raw_key: object, content: str, *, supersede: bool) -> Tuple[str, bool]:
    """The row identity for one extracted memory, and whether it names a claim.

    A validated `subject|relation` claim key when the extractor supplied one, so
    that every phrasing of one claim lands on one row; a content hash otherwise,
    which is what every memory was keyed on before claim keys existed and is
    still the right fallback — it collides with nothing, so a missing key costs
    supersession rather than correctness.

    The flag matters downstream because the two kinds of key have different
    scopes: a content hash identifies one sentence *of one kind*, while a claim
    key identifies a slot whose value the extractor may well label `fact` one
    turn and `preference` the next.
    """
    key = normalize_claim_key(raw_key) if supersede else None
    if key is None:
        return _content_key(content)[:MAX_NORMALIZED_KEY], False
    return key[:MAX_NORMALIZED_KEY], True


# What a retired row's `normalized_key` becomes. The unique constraint is
# (workspace, kind, normalized_key) and it is worth keeping — it is what stops
# two concurrent runs from leaving two live rows for one claim — so a superseded
# row has to vacate the key its replacement now owns. Its own id is appended
# rather than the key being thrown away: the history stays greppable by claim,
# and `superseded_by` already makes the chain walkable without it.
RETIRED_KEY_MARKER = "~superseded~"
# The same trick for a tombstone that cannot take its content hash, because some
# other row of the same kind already holds it.
FORGOTTEN_KEY_MARKER = "~forgotten~"


def _retired_key(normalized_key: str, item_id: str, marker: str = RETIRED_KEY_MARKER) -> str:
    head = MAX_NORMALIZED_KEY - len(marker) - len(item_id)
    return f"{normalized_key[:head]}{marker}{item_id}"


def tombstone_key(db: Session, item: MemoryItem) -> str:
    """Where a forgotten memory's key goes.

    A tombstone is about one *value*, not about the claim slot that value
    happened to occupy. While memories were keyed on their content that
    distinction did not exist, but a claim key spans every future value of the
    slot: leaving the tombstone on `api|deploy_host` makes the next deploy host,
    and the one after that, permanently unlearnable — measured at 93.3% -> 60.0%
    overall recall on evals/memory_corpus.json once each item's first fact was
    forgotten.

    So the row moves back to its own content hash, which is exactly what the key
    meant before claim keys existed: the forgotten sentence still cannot be
    written back (`_upsert_item` probes for it by content), and the claim key is
    free for the correction that replaces it.
    """
    key = _content_key(item.content)
    taken = db.scalar(
        select(MemoryItem.id).where(
            MemoryItem.workspace_id == item.workspace_id,
            # The owner is part of the unique key, so only a row with the same
            # owner can be holding this hash against us. Matching more widely
            # would re-key this tombstone because of somebody else's row.
            MemoryItem.owner_id == item.owner_id,
            MemoryItem.kind == item.kind,
            MemoryItem.normalized_key == key,
            MemoryItem.id != item.id,
        )
    )
    # Another row of this kind already holds the hash — two identical sentences
    # stored through different paths, say. Uniqueness wins over discoverability:
    # this tombstone stops blocking rewrites, which is the pre-existing behaviour
    # for that (impossible to hit through one path) case, rather than crashing.
    if taken is None:
        return key
    return _retired_key(key, item.id, FORGOTTEN_KEY_MARKER)


def _is_restatement(old_content: str, new_content: str) -> bool:
    """Is the new content the same claim *value* as the old one, just reworded?

    "Materially different" is decided on the significant-token multiset, so
    casing, punctuation, stopwords and word order can all move without churning
    a row ("Nate deploys the API on Railway." vs "The API is deployed on
    Railway by Nate."), while any change to a content-bearing word is material —
    a host, a name, a weekday, a version number. Nothing weaker would do: the
    claim key has already asserted these two rows are the same subject and the
    same relation, so a differing token *is* a differing value for that slot, and
    a similarity threshold would have to call Fly.io/Railway (one token apart in
    a seven-token sentence) a rewording.
    """
    return sorted(tokenize(old_content)) == sorted(tokenize(new_content))


def _retire(db: Session, rows: Sequence[MemoryItem], replacement_id: str) -> None:
    """Mark rows superseded and make them vacate the claim key they hold."""
    if not rows:
        return
    for row in rows:
        row.status = "superseded"
        row.superseded_by = replacement_id
        row.normalized_key = _retired_key(row.normalized_key, row.id)
        row.updated_at = utcnow()
    # Flushed before the replacement is inserted: one flush would otherwise be
    # free to send the INSERT before the UPDATE, and the unique constraint would
    # see two rows on one claim key.
    db.flush()


_ClaimArgs = ParamSpec("_ClaimArgs")


def _retry_on_claim_collision(
    write: Callable[Concatenate[Session, _ClaimArgs], MemoryItem],
) -> Callable[Concatenate[Session, _ClaimArgs], MemoryItem]:
    """Commit one claim on its own, and do it again if someone else got there.

    The write it wraps is read-then-insert against
    `UniqueConstraint('workspace_id', 'kind', 'normalized_key')`: two runs that
    extract the same claim at the same moment both find no live row holding it
    and both insert one. No lock prevents that — the row does not exist yet, so
    there is nothing to lock — which leaves noticing the collision and doing the
    work again as the only answer that is correct on Postgres and on SQLite
    alike. The second pass finds the winner's row live, so the ordinary
    supersession path applies: the older value is retired and the newer one
    replaces it, which is what would have happened had the two turns arrived one
    after the other.

    Committing per claim is what makes the retry possible at all — the loser has
    to be able to *see* the winner's row, and an uncommitted row is invisible to
    it. It is also what keeps one contested claim from costing a turn everything
    else it learned: `write_conversation_memory` rolls the session back when a
    write raises, so a single collision used to discard the whole batch and say
    so only in a log line.
    """

    @wraps(write)
    def guarded(
        db: Session, *args: _ClaimArgs.args, **kwargs: _ClaimArgs.kwargs
    ) -> MemoryItem:
        for last_attempt in (False, True):
            try:
                item = write(db, *args, **kwargs)
                db.commit()
                return item
            except IntegrityError:
                db.rollback()
                if last_attempt:
                    raise
        raise AssertionError("the retry loop returns or raises on both passes")

    return guarded


#: The `space_id` of a memory the whole workspace recalls, as opposed to one
#: space's. The second scope axis beside `SHARED_OWNER`, same shape for the
#: same ADR 0010 reasons: a sentinel, never NULL, because it joins the unique
#: key. "" collapses every predicate below to the pre-spaces behaviour.
SHARED_SPACE = ""

#: Explicit "every shelf" for `_active`'s space axis — the memory admin surface
#: (`GET /api/memory`) lists and tombstones across spaces, because every space
#: is workspace-visible (a space is a relevance scope, not a privacy scope; the
#: owner axis is the privacy one and is never widened this way). A *constant*
#: rather than a widened default so the collapse property holds: a caller that
#: says nothing still gets the global shelf alone, and widening is a decision
#: spelled out at the call site.
ALL_SPACES: Optional[str] = None


def memory_space(db: Session, conversation_id: Optional[str]) -> str:
    """Which space's shelf a memory learned in this conversation lands on.

    The space twin of `memory_owner`, and the same rule: taken from the
    conversation, never from a caller's field — an argument the model could
    set is a scope prompt-injected content could set. A run with no
    conversation is automation, which is workspace-global; a conversation that
    has gone missing answers "" for the same reason `memory_owner` answers
    personal — the narrow reading is the safe one, and here the narrow *write*
    is the global shelf every member could already see.
    """
    if not conversation_id:
        return SHARED_SPACE
    return (
        db.scalar(
            select(Conversation.space_id).where(Conversation.id == conversation_id)
        )
        or SHARED_SPACE
    )


@_retry_on_claim_collision
def _upsert_item(
    db: Session,
    *,
    workspace_id: str,
    conversation_id: Optional[str],
    run_id: str,
    kind: str,
    normalized_key: str,
    content: str,
    entity_names: List[str],
    message_ids: List[str],
    supersede: bool,
    claim_keyed: bool = False,
    owner_id: str = SHARED_OWNER,
    space_id: str = SHARED_SPACE,
) -> MemoryItem:
    """Store one claim, retiring the older value of that claim when there is one.

    `supersede` is per call site, not per settings, because only extracted claims
    have a value that can be corrected: the rolling conversation summary reuses
    one key on purpose and grows in place, so retiring it on every refresh would
    be pure row churn with no correction to record.

    Every lookup below matches `owner_id` — and `space_id` — **exactly**, and
    that is what keeps supersession inside a scope: correcting my value for a
    claim must find my row and only mine, or `_retire` marks your value
    superseded and rewrites its key so it can never be recalled again. Along
    the space axis the same rule means a correction made inside a space
    retires the space's row and leaves the workspace-global one standing. This
    is deliberately not the `_active()` predicate, which is the *reader's*
    rule (shared plus my own, this space plus global) — a writer is not
    choosing what to look at, it is choosing what it owns.
    """
    item_id = new_id()

    # A tombstone blocks the *value* it was written for, wherever that value
    # tries to enter — looked up by content hash, because `forget` re-keys the
    # row to it precisely so that the claim key stays available to corrections.
    forgotten = db.scalar(
        select(MemoryItem).where(
            MemoryItem.workspace_id == workspace_id,
            MemoryItem.owner_id == owner_id,
            MemoryItem.space_id == space_id,
            MemoryItem.kind == kind,
            MemoryItem.normalized_key == _content_key(content),
            MemoryItem.status == "deleted",
        )
    )
    if forgotten is not None:
        # "Stop knowing this" is a stronger instruction than "this changed", and
        # the row is the only thing keeping the extractor from writing the fact
        # straight back the next time it comes up.
        return forgotten

    scope = [
        MemoryItem.workspace_id == workspace_id,
        MemoryItem.owner_id == owner_id,
        MemoryItem.space_id == space_id,
        MemoryItem.normalized_key == normalized_key,
    ]
    if not claim_keyed:
        # A content hash identifies one sentence of one kind, so it is scoped the
        # way it always was. A claim key identifies the slot itself, and `kind`
        # is per-turn model output with nothing pinning it per claim: scoping the
        # lookup by it let "Nate prefers Recharts" (preference) and "Nate
        # switched to Visx" (fact) sit side by side under one claim key, which
        # put the stale-served rate straight back to 100% (5/5) on
        # evals/memory_corpus.json.
        scope.append(MemoryItem.kind == kind)
    rows = list(
        db.scalars(select(MemoryItem).where(*scope).order_by(MemoryItem.created_at))
    )
    tombstoned = [row for row in rows if row.status == "deleted"]
    if tombstoned:
        # A tombstone still sitting on a claim key — left by an earlier build, or
        # by a call site that is not claim-keyed. It blocks every future value of
        # the slot, so it is moved to its content hash the way `forget` does now.
        # The sentence it was written for stays blocked by the probe above; what
        # is released is the claim key, which belongs to the current value.
        for row in tombstoned:
            row.normalized_key = tombstone_key(db, row)
        db.flush()
        rows = [row for row in rows if row.status != "deleted"]
    # Retired rows have vacated their key too, so everything left here is live:
    # one row per kind at most.
    if rows:
        existing: Optional[MemoryItem] = (
            next((row for row in rows if _is_restatement(row.content, content)), None)
            if supersede
            # Ablated, there is nothing to compare: the key is a content hash, so
            # the row it found already holds this exact sentence.
            else rows[0]
        )
        if existing is not None:
            existing.content = content
            existing.importance += 1
            merged = set(json.loads(existing.message_ids_json)) | set(message_ids)
            existing.message_ids_json = json.dumps(sorted(merged)[:50])
            existing.entity_names_json = json.dumps(
                sorted(set(json.loads(existing.entity_names_json)) | set(entity_names))[:16]
            )
            existing.embedding = None
            existing.updated_at = utcnow()
            # Any other kind holding this claim key states a different value for
            # a slot that now has one, so it is retired rather than left live —
            # pointed at the row that survived, not at the id of a replacement
            # this branch never inserts.
            _retire(db, [row for row in rows if row is not existing], existing.id)
            return existing
        # A materially different value for the same claim. The old rows are
        # retired rather than overwritten so the history stays auditable, and
        # they drop out of recall through the same `_active()` chokepoint
        # deletions use — no scoring code learns about supersession.
        _retire(db, rows, item_id)
    item = MemoryItem(
        # Assigned here rather than at flush so the retired row above can point
        # at its replacement without a second round trip.
        id=item_id,
        workspace_id=workspace_id,
        owner_id=owner_id,
        space_id=space_id,
        conversation_id=conversation_id,
        run_id=run_id,
        kind=kind,
        content=content,
        normalized_key=normalized_key,
        entity_names_json=json.dumps(entity_names[:16]),
        message_ids_json=json.dumps(message_ids[:50]),
    )
    # Deliberately no importance carried over from the retired row: importance
    # counts how often a claim recurs, and a stale fact repeated for months must
    # not lend its weight to the correction that replaced it — nor keep any of
    # its own, which is what let it outrank that correction in the first place.
    db.add(item)
    return item


def _embed_pending(
    db: Session, items: Sequence[MemoryItem], settings: Settings
) -> None:
    """Attach vectors to items that have none. Best-effort by design.

    An exception here means the provider is down, and `embed_batch` answers None
    when there is no key to reach it with — neither is a reason to lose the
    memory itself, which stays lexically recallable either way.
    """
    pending = [item for item in items if item.status == "active" and item.embedding is None]
    if not pending:
        return
    generation = generations.writable_generation(db, settings)
    try:
        # The rows carry the tenant, so this is attributed whether the caller was
        # the post-run writer (which also knows the run) or the `remember` tool.
        with usage_scope(workspace_id=pending[0].workspace_id):
            result = embed_batch(
                [item.content for item in pending],
                settings,
                dimensions=generation.dimensions,
                dtype=generation.storage_dtype,
            )
    except Exception:
        result = None
    if result is None:
        return
    if result.revision and not generation.revision:
        generation.revision = result.revision
    # strict=False: vectors come back from an external embedding API, so a short
    # response should embed fewer items, not lose them all.
    for item, vector in zip(pending, result.blobs, strict=False):
        generations.store_vector(
            db,
            generation=generation,
            owner_kind=generations.MEMORY_ITEM,
            owner_id=item.id,
            workspace_id=item.workspace_id,
            vector=vector,
            content_hash=content_fingerprint(item.content),
        )
        item.embedding = vector
        item.embedding_model = settings.openai_embedding_model


def _refresh_summary(
    db: Session,
    run: Run,
    settings: Settings,
) -> None:
    messages = list(
        db.scalars(
            select(Message)
            .where(
                Message.workspace_id == run.workspace_id,
                Message.conversation_id == run.conversation_id,
            )
            .order_by(Message.created_at.asc())
        )
    )
    if len(messages) < SUMMARY_REFRESH_EVERY:
        return
    user_lines = [
        " ".join(message.content.split())[:120]
        for message in messages
        if message.role == "user"
    ][:8]
    content = "Conversation topics so far: " + "; ".join(user_lines)
    _upsert_item(
        db,
        workspace_id=run.workspace_id,
        conversation_id=run.conversation_id,
        run_id=run.id,
        kind="summary",
        normalized_key=run.conversation_id,
        content=content[:900],
        entity_names=[],
        message_ids=[message.id for message in messages[-4:]],
        supersede=False,
        # Shared, and the one memory that is regardless of its thread. The
        # summary is keyed on the conversation id and `_pinned_summary` only ever
        # matches the conversation a turn is running in, so it is already scoped
        # by something stronger than an owner; adding one would give the row two
        # visibility rules that could disagree.
        owner_id=SHARED_OWNER,
        # The space, though, is stamped: `_pinned_summary` reads through
        # `_active`, whose space predicate must keep matching this row from
        # inside the conversation's own space.
        space_id=memory_space(db, run.conversation_id),
    )


def apply_extracted_memories(
    db: Session,
    *,
    workspace_id: str,
    conversation_id: Optional[str],
    run_id: str,
    extracted: Sequence[Dict[str, object]],
    message_ids: Sequence[str],
    settings: Settings,
    owner_id: str = SHARED_OWNER,
    space_id: str = SHARED_SPACE,
) -> List[MemoryItem]:
    """Store one extraction's worth of memories; return the rows it touched.

    Split out of `write_conversation_memory` so scripts/evaluate_memory.py can
    seed a workspace through the write path itself rather than a transcription
    of it. A harness that reimplements the code it grades measures the
    transcription — the mistake evaluate_retrieval.py was rebuilt to undo.

    `owner_id` is passed in rather than derived here on purpose. Deciding whose a
    memory is means reading the conversation's `shared` flag, which is a
    visibility *policy* and belongs with the caller that has the run; this
    function is storage. It also keeps the eval measuring scoring rather than
    policy — it seeds shared memories and recalls them with no viewer, which is
    the same arrangement it has always measured.
    """
    # The ablation switch. Off, this is exactly the write path that predates
    # claim keys: every memory is keyed on a hash of its content, so two
    # phrasings of one claim are two rows and neither retires the other. Both
    # halves have to move together — keeping the claim key while refusing to
    # supersede would silently *overwrite* the older phrasing, a third behaviour
    # that has never been measured.
    supersede = settings.memory_supersession
    touched: List[MemoryItem] = []
    for raw in extracted[: settings.memory_max_items_per_run]:
        content = str(raw.get("content") or "").strip()
        if not content:
            continue
        kind = str(raw.get("kind") or "fact")
        raw_entities = raw.get("entities")
        entities = (
            [str(name) for name in raw_entities]
            if isinstance(raw_entities, list)
            else []
        )
        normalized_key, claim_keyed = _claim_key(
            raw.get("normalized_key"), content, supersede=supersede
        )
        touched.append(
            _upsert_item(
                db,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                run_id=run_id,
                kind=kind,
                normalized_key=normalized_key,
                content=content,
                entity_names=entities,
                message_ids=list(message_ids),
                supersede=supersede,
                claim_keyed=claim_keyed,
                owner_id=owner_id,
                space_id=space_id,
            )
        )
    return touched


def memory_owner(db: Session, conversation_id: Optional[str], author_id: str) -> str:
    """Whose a memory learned in this conversation is.

    ADR 0010's rule, in one place: a memory is exactly as visible as the thread
    it came from. `Conversation.shared` is already the workspace's answer to who
    may read this thread — the API refuses to let a member so much as decide a
    tool call parked on someone else's personal one — so a memory extracted from
    it inherits that answer rather than inventing a second one.

    Nothing else was usable. `kind` was the tempting alternative (preference is
    personal, fact is shared) and `_upsert_item` already documents why it is not:
    it is per-turn model output with nothing pinning it per claim, so one claim
    would land personal on Tuesday and shared on Wednesday.

    A run with no conversation is automation, which belongs to the workspace.
    """
    if not conversation_id:
        return SHARED_OWNER
    shared = db.scalar(
        select(Conversation.shared).where(Conversation.id == conversation_id)
    )
    # A conversation that has gone missing is treated as personal: the narrow
    # answer is the safe one, and an unreadable memory beats a leaked one.
    return SHARED_OWNER if shared else author_id


def write_conversation_memory(run_id: str) -> None:
    """Persist durable memories after a completed run. Best-effort by design."""
    settings = get_settings()
    if not settings.memory_enabled:
        return
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if run is None or run.status != "completed":
            return
        messages = list(
            db.scalars(
                select(Message)
                .where(Message.run_id == run.id)
                .order_by(Message.created_at.asc())
            )
        )
        answer = next(
            (message.content for message in reversed(messages) if message.role == "assistant"),
            "",
        )
        message_ids = [message.id for message in messages]

        with usage_scope(
            workspace_id=run.workspace_id,
            run_id=run.id,
            conversation_id=run.conversation_id,
            user_id=run.created_by,
        ):
            extracted = extract_memories(
                run.prompt, answer, user_id=run.created_by, settings=settings
            )

        touched = apply_extracted_memories(
            db,
            workspace_id=run.workspace_id,
            conversation_id=run.conversation_id,
            run_id=run.id,
            extracted=extracted,
            message_ids=message_ids,
            settings=settings,
            # The leak this closes: a personal thread is visible only to its
            # creator, and until now everything the extractor learned from one
            # was written workspace-wide and recalled into every member's turn.
            owner_id=memory_owner(db, run.conversation_id, run.created_by),
            # And the shelf: learned in a space's thread, recalled in that
            # space's threads — plus the global shelf everywhere.
            space_id=memory_space(db, run.conversation_id),
        )
        _refresh_summary(db, run, settings)
        db.flush()

        _embed_pending(db, touched, settings)

        if touched:
            mark_graph_stale(db, run.workspace_id)
            record_audit(
                db,
                workspace_id=run.workspace_id,
                actor_id=run.created_by,
                action="memory.updated",
                resource_type="run",
                resource_id=run.id,
                detail={"items": len(touched)},
            )
        db.commit()
    except Exception:
        # Writing memory must never fail a run that already answered, but
        # swallowing it without a word is how a workspace quietly stops learning:
        # extraction is a model call now that there is no offline path around it,
        # so one rate-limited provider drops the turn's memories with no run
        # error, no event, and nothing in the log to find later. Same treatment
        # as the degraded-recall warning below.
        logger.warning(
            "conversation memory was not written for run %s", run_id, exc_info=True
        )
        db.rollback()
    finally:
        db.close()


def _graph_digest(db: Session, workspace_id: str, query: str) -> List[str]:
    # Both spellings of an article-merged name: the rebuild folds 'the atlas'
    # into 'atlas', so a question about "The Atlas" has to reach the node that
    # survived or the digest silently goes empty.
    names = sorted(
        {
            candidate
            # drop_calendar=False: this is a lookup against nodes that already
            # exist, so the calendar guess can only lose a match here.
            for normalized, _display in extract_entities(query, drop_calendar=False)
            for candidate in name_candidates(normalized)
        }
    )
    if not names:
        return []
    entities = list(
        db.scalars(
            select(GraphEntity).where(
                GraphEntity.workspace_id == workspace_id,
                GraphEntity.normalized_name.in_(names),
            )
        )
    )
    if not entities:
        return []
    by_id = {entity.id: entity for entity in entities}
    edges = list(
        db.scalars(
            select(GraphEdge)
            .where(
                GraphEdge.workspace_id == workspace_id,
                (
                    GraphEdge.from_entity_id.in_(by_id.keys())
                    | GraphEdge.to_entity_id.in_(by_id.keys())
                ),
            )
            .order_by(GraphEdge.weight.desc())
            .limit(MAX_GRAPH_DIGEST_LINES * 2)
        )
    )
    neighbor_ids = {edge.from_entity_id for edge in edges} | {
        edge.to_entity_id for edge in edges
    }
    missing = neighbor_ids - set(by_id.keys())
    if missing:
        # Filtered on the workspace as well as the ids: see the same fix in
        # llm_tools._graph_lookup. The ids come from workspace-scoped edges, but
        # an edge's endpoints are not constrained to its own workspace, and this
        # is the query that would put a foreign entity name into the recalled
        # graph digest.
        for entity in db.scalars(
            select(GraphEntity).where(
                GraphEntity.workspace_id == workspace_id,
                GraphEntity.id.in_(missing),
            )
        ):
            by_id[entity.id] = entity
    digest: List[str] = []
    for edge in edges[:MAX_GRAPH_DIGEST_LINES]:
        left = by_id.get(edge.from_entity_id)
        right = by_id.get(edge.to_entity_id)
        if left is None or right is None:
            continue
        digest.append(f"{left.name} —{edge.relation}({edge.weight})— {right.name}")
    return digest


def _active(
    stmt: _SelectT,
    workspace_id: str,
    viewer_id: str = SHARED_OWNER,
    space_id: Optional[str] = SHARED_SPACE,
) -> _SelectT:
    """Workspace + liveness + ownership scoping, applied through one chokepoint.

    recall() issues three independent candidate queries; forgetting the scope on
    any one of them leaks another workspace's memories into an answer, so none of
    them spells the predicate out for itself.

    `status == "active"` is also the whole of how supersession reaches recall: a
    retired claim is neither a lexical candidate, nor a vector candidate, nor a
    pinned summary, because all three go through here. That is why representing
    supersession needed no scoring change — see test_memory_depth.py, which
    asserts this function is the only status filter in the module.

    Ownership (ADR 0010) is routed through the same door for the same reason, and
    now there are two axes to forget rather than one: the queries that leak
    another *workspace* are exactly the queries that would leak another *person*.

    `viewer_id` is who is asking. They see the workspace's shared memories plus
    their own and nobody else's. `space_id` is where they are asking *from*:
    a turn in a space sees that space's shelf plus the global one, a turn
    outside sees the global shelf alone, and another space's rows are never
    candidates. Both axes share the collapse property described below — the
    default is the narrow answer. The predicate has one property worth naming,
    because it removed the need for a second mode: with the default `SHARED_OWNER` it
    collapses to `owner_id = ''`, which is precisely "shared only". So the graph
    projection — one row set per workspace, read by every member, and therefore
    only ever buildable from shared memory — calls this with no viewer and cannot
    be handed one by accident. A call site that forgets to say who is asking gets
    less, never more.
    """
    stmt = stmt.where(
        MemoryItem.workspace_id == workspace_id,
        MemoryItem.status == "active",
        MemoryItem.owner_id.in_({SHARED_OWNER, viewer_id}),
    )
    if space_id is not ALL_SPACES:
        stmt = stmt.where(MemoryItem.space_id.in_({SHARED_SPACE, space_id}))
    return stmt


def _like_pattern(term: str) -> str:
    r"""A LIKE pattern that matches `term` literally.

    TOKEN_RE admits `_`, which is LIKE's single-character wildcard, so tokens are
    escaped rather than interpolated raw — `read_only` must not match `readXonly`.
    """
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _pinned_summary(
    db: Session,
    *,
    workspace_id: str,
    conversation_id: str,
    viewer_id: str = SHARED_OWNER,
    space_id: str = SHARED_SPACE,
) -> Optional[MemoryItem]:
    """The rolling summary of the current conversation, which is always relevant."""
    return db.scalar(
        _active(select(MemoryItem), workspace_id, viewer_id, space_id).where(
            MemoryItem.conversation_id == conversation_id,
            MemoryItem.kind == "summary",
        )
    )


def _lexical_candidates(
    db: Session,
    *,
    workspace_id: str,
    terms: Sequence[str],
    exclude_id: Optional[str],
    limit: int,
    viewer_id: str = SHARED_OWNER,
    space_id: str = SHARED_SPACE,
) -> List[str]:
    """Ids of memories containing at least one query term.

    This prefilter is not an optimisation of the vector path — it is the whole
    lexical path. Tokenizing every memory in the workspace costs ~940ms at 100k
    rows, which no vector index would fix. LIKE is a C-level scan in both engines
    (~24ms at 100k) and `lower()` is mandatory: SQLite's LIKE is ASCII
    case-insensitive by default and Postgres's is not, so a bare LIKE would
    silently return different rows on the two backends.
    """
    if not terms:
        return []
    hits = [
        func.lower(MemoryItem.content).like(_like_pattern(term), escape="\\")
        for term in terms
    ]
    # How many distinct query terms this row contains — the numerator of the
    # `lexical` score, recomputed in SQL. Without an ORDER BY the LIMIT keeps an
    # arbitrary (in practice: oldest) slice of the matches, which differs by
    # backend and by plan; ordering by `importance` instead keeps the *most
    # reinforced* rows, but importance contributes at most 0.25 to a score whose
    # lexical term spans a full 1.0, so a highly relevant memory in a large
    # workspace was being truncated away in favour of a barely relevant popular
    # one. Ranking the truncation by the score's own dominant term keeps the rows
    # that actually win the rescoring pass.
    overlap = reduce(operator.add, (case((hit, 1), else_=0) for hit in hits))
    stmt = _active(select(MemoryItem.id), workspace_id, viewer_id, space_id).where(
        or_(*hits)
    )
    if exclude_id is not None:
        stmt = stmt.where(MemoryItem.id != exclude_id)
    stmt = stmt.order_by(
        overlap.desc(), MemoryItem.importance.desc(), MemoryItem.updated_at.desc()
    )
    return [str(row) for row in db.scalars(stmt.limit(limit))]


def _vector_scores(
    rows: Sequence[Tuple[str, Optional[bytes]]],
    query_blob: bytes,
    dtype: str = DEFAULT_DTYPE,
) -> Tuple[Dict[str, float], List[str]]:
    """Cosine similarity in one matmul: (score for every row, shortlist of ids).

    Two different jobs, which is why two values come back. The shortlist bounds
    how many rows the *vector* path is allowed to add to the candidate set. The
    score map must cover every row scanned, because a row can also arrive through
    the lexical prefilter — and the original full scan gave that row its real
    semantic term. Returning only the shortlist scored such a row as 0.0 and
    silently dropped it out of the top-k.

    The matmul, the length guard and the ordering live in `embeddings` because
    document retrieval needs exactly the same three things over `Chunk.embedding`;
    what stays here is the shortlist policy, which is memory's own.
    """
    ranked = ranked_cosine_scores(rows, query_blob, dtype)
    return dict(ranked), [row_id for row_id, _ in ranked[:VECTOR_SHORTLIST]]


def _vector_candidates(
    db: Session,
    *,
    workspace_id: str,
    query_blob: bytes,
    exclude_id: Optional[str],
    settings: Settings,
    generation: EmbeddingGeneration,
    viewer_id: str = SHARED_OWNER,
    space_id: str = SHARED_SPACE,
) -> Tuple[Dict[str, float], List[str]]:
    stmt = _active(
        select(EmbeddingVector.owner_id, EmbeddingVector.vector).join(
            MemoryItem, MemoryItem.id == EmbeddingVector.owner_id
        ),
        workspace_id,
        viewer_id,
        space_id,
    ).where(
        # Scored within one contract, so every vector in this matmul came from one
        # model at one width. See `EmbeddingGeneration`.
        EmbeddingVector.generation_id == generation.id,
        EmbeddingVector.owner_kind == generations.MEMORY_ITEM,
    )
    if exclude_id is not None:
        stmt = stmt.where(MemoryItem.id != exclude_id)
    cap = settings.memory_recall_candidate_cap
    if cap > 0:
        # The cost of the cap: past `cap` memories in one workspace, an old row is
        # reachable only through the lexical prefilter, not the vector scan. Two
        # things keep that survivable — `importance` and `updated_at` are both
        # bumped on every re-touch, so anything referenced repeatedly stays inside
        # the window by construction, and exact term matches always come back via
        # _lexical_candidates. Raise the cap before reaching for an ANN index; the
        # measured curve is 8ms at 2k, 21ms at 5k, 103ms at 20k. Above ~20k switch
        # this fetch to .partitions(4096) with a running top-k: a single uncapped
        # 100k scan peaks at +1.4GB RSS, which OOMs a small container on one turn.
        stmt = stmt.order_by(MemoryItem.updated_at.desc()).limit(cap)
    rows = [(str(row_id), blob) for row_id, blob in db.execute(stmt).all()]
    return _vector_scores(rows, query_blob, generation.storage_dtype)


def _embed_query(
    query: str, settings: Settings, generation: EmbeddingGeneration
) -> Optional[bytes]:
    """This turn's query vector, reusing one we already paid for when we can.

    With scoring now local and bounded (~78ms at 10k memories), the embedding
    round-trip is what recall costs: ~50-200ms of network, 5-20x the work that
    follows it. Nothing about (query text, embedding model) -> vector changes
    between calls, so a repeat — a retried or resumed run, the agent's
    `search_memory` echoing the user's phrasing, the same question asked twice —
    should not pay for it twice.

    An in-process LRU rather than a table: this app runs as a single uvicorn
    process (scripts/dev.sh, Makefile), infra/compose.yaml ships no API service
    and there is no Dockerfile, so the cross-worker sharing a table buys does not
    exist to be bought. What a table would add is a migration, a row per distinct
    free-text query with a reaper to keep it from growing forever, and a database
    round-trip in place of a ~1us dict lookup. The durable half of this problem
    is already solved where it belongs: MemoryItem.embedding persists the vectors
    for memory *contents*, which are finite and reused. Queries are neither.

    Misses go through the module-global `embed_texts` on purpose: it stays the
    one place that knows how to reach a provider, and the seam tests patch.
    """
    if not query.strip():
        # A blank prompt has no terms to match and nothing to embed. This was a
        # guaranteed-wasted round-trip before — worse than wasted, since OpenAI
        # rejects an empty input outright, so it cost latency and then raised
        # into the caller's degrade-to-lexical path.
        return None
    key = query_cache_key(
        query,
        generation.model,
        settings.active_model_provider,
        generation.id,
    )
    cached = query_embedding_cache.get(key)
    if cached is not None:
        return cached
    result = embed_batch(
        [query],
        settings,
        model=generation.model,
        dimensions=generation.dimensions,
        dtype=generation.storage_dtype,
    )
    if result is None or not result.blobs:
        # None is "no key to embed with"; [] is a provider that answered with
        # nothing. `put` refuses either, but returning early keeps that explicit.
        return None
    query_embedding_cache.put(key, result.blobs[0])
    return result.blobs[0]


def _personal_shadows_shared(items: Sequence[MemoryItem]) -> List[MemoryItem]:
    """Drop the workspace's value for any claim the viewer holds their own value of.

    ADR 0010's "mine wins", and deliberately not a scoring bonus. A bonus ranks
    my value above the workspace's while still handing the model both — a claim
    and a contradicting claim, in one unlabelled list, with nothing to tell them
    apart. That is exactly the failure evaluate_memory.py measures and names
    STALE-SERVED, and it does not become acceptable because the two rows disagree
    across people rather than across time.

    `normalized_key` is the right identity to shadow on because it already *is*
    the identity of a claim slot — the same key supersession retires on — so
    "my version of this claim" needs no new concept to express.
    """
    mine = {item.normalized_key for item in items if item.owner_id != SHARED_OWNER}
    if not mine:
        return list(items)
    return [
        item
        for item in items
        if item.owner_id != SHARED_OWNER or item.normalized_key not in mine
    ]


def _space_shadows_global(items: Sequence[MemoryItem]) -> List[MemoryItem]:
    """Drop the global value for any claim this space holds its own value of.

    The space axis of `_personal_shadows_shared`, same argument: specific
    beats general *by omission*, never by scoring, because handing the model a
    claim and a contradicting claim in one unlabelled list is the stale-served
    failure whichever axis the two rows disagree across. Rows from *other*
    spaces never reach here — `_active` excluded them — so "space row" below
    can only mean the space this turn is in. Runs after the owner pass and
    composes with it: my personal global note still beats the workspace's
    global note, and the space's row beats both on the shelf axis.
    """
    here = {item.normalized_key for item in items if item.space_id != SHARED_SPACE}
    if not here:
        return list(items)
    return [
        item
        for item in items
        if item.space_id != SHARED_SPACE or item.normalized_key not in here
    ]


def recall(
    db: Session,
    *,
    workspace_id: str,
    conversation_id: str,
    query: str,
    viewer_id: str = SHARED_OWNER,
    settings: Optional[Settings] = None,
) -> MemoryContext:
    """Select the memories worth injecting into this turn's prompt.

    `viewer_id` is whose turn this is. They get the workspace's shared memories
    plus their own, with their own winning any claim both hold — see ADR 0010.
    It defaults to shared-only rather than being required, because unlike
    `resolve_policy`'s `scope` there is a genuinely correct narrow answer here:
    "the workspace's own memories" is a real, safe answer to give a caller that
    does not know who is asking, and it can only ever return less.

    Scoring itself matches the original full-scan implementation, but the
    candidate set is bounded and that is a real trade, not a free one: lexical
    matches come from a SQL prefilter, while semantic matches are scored over
    the newest `memory_recall_candidate_cap` rows only. A memory older than that
    window is invisible to semantic scoring however similar it is — the cap is a
    recency window wearing a performance hat. It is set high enough that the
    ordering matches an unbounded scan on any corpus this app realistically
    reaches; shrink it and old-but-relevant memories start disappearing.
    """
    settings = settings or get_settings()
    if not settings.memory_enabled:
        return MemoryContext()
    # Where this turn is asking from. Derived here, from the conversation,
    # rather than accepted as a parameter, for the same reason the write path
    # derives it: a caller cannot forget to pass what it never had to pass,
    # and read and write scope cannot disagree. A conversationless caller —
    # the eval harness, a workflow — collapses to the global shelf.
    space_id = memory_space(db, conversation_id)
    # Cheap existence probe so an empty workspace never pays for an embedding
    # round-trip, which is what the previous full scan bought by accident.
    if (
        db.scalar(
            _active(select(MemoryItem.id), workspace_id, viewer_id, space_id).limit(1)
        )
        is None
    ):
        return MemoryContext(graph_digest=_graph_digest(db, workspace_id, query))

    summary = _pinned_summary(
        db,
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        viewer_id=viewer_id,
        space_id=space_id,
    )
    summary_id = summary.id if summary is not None else None

    query_terms = set(tokenize(query))
    # The score divides by the *full* term count; only the SQL prefilter is capped.
    terms = sorted(query_terms, key=lambda term: (-len(term), term))[:MAX_LEXICAL_TERMS]
    candidate_ids = set(
        _lexical_candidates(
            db,
            workspace_id=workspace_id,
            terms=terms,
            exclude_id=summary_id,
            limit=settings.memory_lexical_candidate_limit,
            viewer_id=viewer_id,
            space_id=space_id,
        )
    )

    semantic_by_id: Dict[str, float] = {}
    # Recall reads the active contract, never the configured one — during a
    # migration those differ, and a query embedded at the width being built would
    # match nothing that is currently readable.
    generation = generations.active_generation(db)
    query_blob = None
    if generation is not None:
        try:
            with usage_scope(workspace_id=workspace_id, conversation_id=conversation_id):
                query_blob = _embed_query(query, settings, generation)
        except Exception:
            # A missing key returns None without raising; reaching here means the
            # provider actually failed, which silently degrades recall to
            # lexical-only.
            logger.warning(
                "memory recall degraded to lexical-only: embedding failed", exc_info=True
            )
            query_blob = None
    if query_blob and generation is not None:
        # Every scanned row keeps its similarity, but only the shortlist joins the
        # candidate set: a lexical hit that is not in the vector top-k still needs
        # its real semantic term, or its score silently loses up to 1.0.
        semantic_by_id, shortlist = _vector_candidates(
            db,
            workspace_id=workspace_id,
            query_blob=query_blob,
            exclude_id=summary_id,
            settings=settings,
            generation=generation,
            viewer_id=viewer_id,
            space_id=space_id,
        )
        candidate_ids |= set(shortlist)

    items: List[MemoryItem] = []
    if candidate_ids:
        items = list(
            db.scalars(
                _active(select(MemoryItem), workspace_id, viewer_id, space_id)
                # The embedding blob is 6KB a row and already consumed above.
                .options(defer(MemoryItem.embedding))
                .where(MemoryItem.id.in_(candidate_ids))
            )
        )

    scored: List[Tuple[float, MemoryItem]] = []
    for item in items:
        item_terms = set(tokenize(item.content))
        lexical = len(query_terms & item_terms) / max(1, len(query_terms))
        semantic = semantic_by_id.get(item.id, 0.0)
        score = lexical + semantic + min(item.importance, 5) * 0.05
        if lexical > 0 or semantic > 0.3:
            scored.append((score, item))
    # Ties broke on database scan order before, which differs between backends;
    # the id keeps the injected context reproducible for the same corpus.
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    # Shadowed after scoring rather than before: a shared row the viewer overrides
    # should not be able to consume one of `memory_recall_limit` slots either.
    # Owner pass first, then the space pass over its survivors — see
    # `_space_shadows_global` for why the order is safe to state this simply.
    selected = _space_shadows_global(
        _personal_shadows_shared([item for _score, item in scored])
    )
    if summary is not None:
        selected.insert(0, summary)
    return MemoryContext(
        items=selected[: settings.memory_recall_limit],
        graph_digest=_graph_digest(db, workspace_id, query),
    )


def render_memory_context(context: MemoryContext) -> str:
    lines: List[str] = []
    for item in context.items:
        lines.append(f"- ({item.kind}) {item.content}")
    if context.graph_digest:
        lines.append("Known entities & relations:")
        lines.extend("- " + line for line in context.graph_digest)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agentic memory: writes the model makes on purpose, rather than whatever the
# post-run extractor happened to notice.
# ---------------------------------------------------------------------------

REMEMBER_KINDS = ("fact", "preference")
MAX_MEMORY_CONTENT = 900
# A `forget` that matches half the workspace is a mistake, not an instruction.
MAX_FORGET_MATCHES = 10


@dataclass(frozen=True)
class RememberResult:
    item: MemoryItem
    #: created | updated | restored — what the dedupe check decided.
    outcome: str


def normalize_memory_content(content: str) -> str:
    return " ".join(content.split())[:MAX_MEMORY_CONTENT]


def remember_memory(
    db: Session,
    *,
    workspace_id: str,
    conversation_id: Optional[str],
    user_id: str,
    content: str,
    kind: str = "fact",
    entities: Optional[Sequence[str]] = None,
    settings: Optional[Settings] = None,
) -> RememberResult:
    """Store a durable memory now, deduplicating on content.

    Flushes before returning so the memory is recallable in the same conversation
    turn that stored it — the agent loop commits after each tool call, and a
    later `recall()` in the same session must see this row.

    Whose it is comes from the conversation, exactly as the post-run extractor's
    does: "remember this" said in a personal thread is a personal memory. There
    is deliberately no argument for the model to set — an owner the model chooses
    is an owner prompt-injected content can choose.
    """
    settings = settings or get_settings()
    content = normalize_memory_content(content)
    names = [str(name).strip() for name in (entities or []) if str(name).strip()][:16]
    normalized_key = _content_key(content)
    owner_id = memory_owner(db, conversation_id, user_id)
    space_id = memory_space(db, conversation_id)
    existing = db.scalar(
        select(MemoryItem).where(
            MemoryItem.workspace_id == workspace_id,
            MemoryItem.owner_id == owner_id,
            MemoryItem.space_id == space_id,
            MemoryItem.kind == kind,
            MemoryItem.normalized_key == normalized_key,
        )
    )
    if existing is not None:
        outcome = "updated"
        if existing.status != "active":
            # A tombstone stops the *extractor* from resurrecting something the
            # user threw away. "Remember this" is the user asking for precisely
            # that resurrection, so an explicit write overrides the tombstone.
            existing.status = "active"
            outcome = "restored"
        existing.content = content
        existing.importance += 1
        existing.entity_names_json = json.dumps(
            sorted(set(json.loads(existing.entity_names_json)) | set(names))[:16]
        )
        if existing.conversation_id is None:
            existing.conversation_id = conversation_id
        existing.embedding = None
        existing.updated_at = utcnow()
        item = existing
    else:
        item = MemoryItem(
            workspace_id=workspace_id,
            owner_id=owner_id,
            space_id=space_id,
            conversation_id=conversation_id,
            run_id="",
            kind=kind,
            content=content,
            normalized_key=normalized_key,
            entity_names_json=json.dumps(names),
            message_ids_json="[]",
        )
        db.add(item)
        outcome = "created"
    db.flush()
    _embed_pending(db, [item], settings)
    mark_graph_stale(db, workspace_id)
    record_audit(
        db,
        workspace_id=workspace_id,
        actor_id=user_id,
        action="memory.remembered",
        resource_type="memory_item",
        resource_id=item.id,
        detail={"kind": item.kind, "outcome": outcome},
    )
    db.flush()
    return RememberResult(item=item, outcome=outcome)


def resolve_forget_targets(
    db: Session,
    *,
    workspace_id: str,
    memory_id: Optional[str] = None,
    content: Optional[str] = None,
    viewer_id: str = SHARED_OWNER,
    space_id: str = SHARED_SPACE,
) -> Tuple[List[MemoryItem], str]:
    """Find what a `forget` would tombstone. Returns (matches, error message).

    Shared by the preview and the executor so the approval card cannot describe a
    different set of memories from the one that actually gets forgotten.

    Scoped to what the viewer can see, which makes "forget everything about X"
    reach the workspace's memories and my own and stop there. Forgetting is a
    write, but it is a write to a set chosen by *reading*, so it takes the
    reader's predicate: you cannot tombstone a memory you were never able to
    recall.
    """
    if memory_id:
        item = db.scalar(
            _active(select(MemoryItem), workspace_id, viewer_id, space_id).where(
                MemoryItem.id == memory_id
            )
        )
        if item is None:
            return [], f"No active memory with id {memory_id} in this workspace."
        return [item], ""
    needle = " ".join((content or "").split())
    if not needle:
        return [], "Provide either memory_id or content to match."
    matches: List[MemoryItem] = list(
        db.scalars(
            _active(select(MemoryItem), workspace_id, viewer_id, space_id)
            .where(
                func.lower(MemoryItem.content).like(
                    _like_pattern(needle.lower()), escape="\\"
                )
            )
            .order_by(MemoryItem.updated_at.desc())
            .limit(MAX_FORGET_MATCHES + 1)
        )
    )
    if not matches:
        return [], f"No active memory contains “{needle}”."
    if len(matches) > MAX_FORGET_MATCHES:
        return [], (
            f"“{needle}” matches more than {MAX_FORGET_MATCHES} memories. "
            "Narrow the text, or forget them one id at a time."
        )
    return matches, ""


def forget_memories(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    items: Sequence[MemoryItem],
) -> None:
    """Tombstone memories. Never a hard delete: the row is what stops the
    post-run extractor from writing the same fact straight back.

    The row also gives up whatever claim key it held — see `_tombstone_key`. A
    tombstone names a value, not the slot that value occupied, and leaving it on
    `api|deploy_host` makes every later deploy host unlearnable.
    """
    for item in items:
        item.status = "deleted"
        item.normalized_key = tombstone_key(db, item)
        item.updated_at = utcnow()
        record_audit(
            db,
            workspace_id=workspace_id,
            actor_id=user_id,
            action="memory.forgotten",
            resource_type="memory_item",
            resource_id=item.id,
            detail={"kind": item.kind},
        )
    db.flush()
