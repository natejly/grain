"""The lifecycle of an embedding contract: build beside, verify, flip, roll back.

Changing the embedding model used to be an unmonitored outage. Vectors were
matched to the running configuration by `embedding_model == settings....`, so
editing that setting did not migrate anything — it made every stored vector fail
the filter at once. The dense arm returned nothing, hybrid search quietly became
lexical search, and because degrading to lexical is a *designed* behaviour rather
than an error, nothing logged, alerted, or failed. Quality dropped and the system
reported itself healthy until someone re-embedded the corpus.

A generation makes that transition explicit and reversible:

    building -> (write vectors) -> verify coverage -> active   (prior -> retired)

Nothing reads a `building` generation, so a half-written corpus cannot be
searched. Activation refuses unless every row that had a vector under another
generation has one under this one, so a partial build cannot become the live
index. And the generation it displaces keeps its vectors, so rollback is an
UPDATE of two rows rather than a re-embed performed under pressure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Type

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..config import Settings, get_settings
from ..models import (
    Chunk,
    ConversationChunk,
    EmbeddingGeneration,
    EmbeddingVector,
    MemoryItem,
)
from .embeddings import DEFAULT_DTYPE, DTYPE_WIDTHS, truncate_vector

logger = logging.getLogger(__name__)

CHUNK = "chunk"
MEMORY_ITEM = "memory_item"
CONVERSATION_CHUNK = "conversation_chunk"

#: {owning model: its `owner_kind` discriminator}. All three share one contract on
#: purpose: memory recall, conversation search and document retrieval each run
#: `ranked_cosine_scores` against a vector built from the same query, so a
#: generation covering only documents would leave the other two comparing across
#: contracts — the exact incoherence this exists to stop.
OWNER_KINDS: Dict[Type, str] = {
    Chunk: CHUNK,
    MemoryItem: MEMORY_ITEM,
    ConversationChunk: CONVERSATION_CHUNK,
}
VECTOR_TABLES: Tuple[Type, ...] = tuple(OWNER_KINDS)

#: Dense floors calibrated per dimension against `evals/corpus.json`.
#:
#: These are measurements, not preferences. Cosine similarity between unrelated
#: vectors grows as dimensionality falls, so one floor cannot serve two widths:
#: 0.30 admits 11.5% of query-document pairs at 1536 dimensions and 24.4% at 256,
#: which pushes junk passages per query from 1.54 to 4.36 while rescuing zero
#: additional true answers. Each value below is the floor that reproduces the
#: 1536/0.30 selectivity at that width, with all 28 corpus answers still retained.
#:
#: Note that the tempting closed form is wrong. Noise scales as 1/sqrt(d), so
#: scaling the floor by sqrt(1536/256) looks principled and gives 0.735 — which
#: discards 27 of the 28 true answers, because genuinely aligned pairs do not
#: scale the way near-orthogonal ones do. Measure; do not derive.
CALIBRATED_DENSE_FLOORS: Dict[int, float] = {
    1536: 0.30,
    1024: 0.32,
    512: 0.33,
    256: 0.35,
    128: 0.38,
}


def effective_floor(
    generation: EmbeddingGeneration, settings: Optional[Settings] = None
) -> float:
    """The dense floor to apply: an explicit override, else the generation's.

    The generation owns the floor because the floor is geometry — it does not
    survive a change of width — but owning it must not mean confiscating it. An
    operator who sets `RETRIEVAL_DENSE_FLOOR`, or a test that overrides it to 0.0
    to show what the floor was suppressing, is making a deliberate statement about
    this deployment, and silently ignoring it would be the more surprising
    behaviour by far: the knob would still be in the config, still documented, and
    no longer connected to anything.

    "Explicit" is `model_fields_set`, which Pydantic populates from the
    environment and from `model_copy(update=...)` but not from a field default.
    So an untouched configuration defers to the calibrated value, and a touched
    one wins.
    """
    settings = settings or get_settings()
    if "retrieval_dense_floor" in settings.model_fields_set:
        return settings.retrieval_dense_floor
    return generation.dense_floor


def calibrated_floor(dimensions: int, fallback: float) -> float:
    """The measured floor for this width, or the caller's default if unmeasured.

    An unmeasured width gets the fallback rather than an interpolation: the shape
    of the curve is not something to guess at, and a wrong floor is silent.
    """
    return CALIBRATED_DENSE_FLOORS.get(dimensions, fallback)


@dataclass(frozen=True)
class TableCoverage:
    table: str
    covered: int
    pending: int
    unembedded: int


@dataclass(frozen=True)
class Coverage:
    """What fraction of the corpus this generation actually holds.

    `pending` is the number that matters, and it counts only rows that hold a
    vector under some *other* generation. A row nothing has ever embedded — a
    chunk whose provider call failed, a memory written while the key was missing —
    is equally absent from every generation, so counting it would make activation
    impossible forever rather than describing a regression. Those are reported as
    `unembedded` so they stay visible without becoming a gate.
    """

    tables: List[TableCoverage]

    @property
    def pending(self) -> int:
        return sum(row.pending for row in self.tables)

    @property
    def covered(self) -> int:
        return sum(row.covered for row in self.tables)

    @property
    def unembedded(self) -> int:
        return sum(row.unembedded for row in self.tables)

    @property
    def complete(self) -> bool:
        return self.pending == 0

    def describe(self) -> str:
        parts = [
            f"{row.table}: {row.covered} covered, {row.pending} pending"
            + (f", {row.unembedded} never embedded" if row.unembedded else "")
            for row in self.tables
        ]
        return "; ".join(parts)


def active_generation(db: Session) -> Optional[EmbeddingGeneration]:
    """The contract readers must use, or None before one has been established."""
    return db.execute(
        select(EmbeddingGeneration).where(EmbeddingGeneration.status == "active")
    ).scalar_one_or_none()


def generation_by_id(db: Session, generation_id: str) -> Optional[EmbeddingGeneration]:
    return db.get(EmbeddingGeneration, generation_id)


def list_generations(db: Session) -> List[EmbeddingGeneration]:
    return list(
        db.execute(
            select(EmbeddingGeneration).order_by(
                EmbeddingGeneration.created_at.desc(), EmbeddingGeneration.id
            )
        ).scalars()
    )


def create_generation(
    db: Session,
    *,
    model: str,
    dimensions: int,
    revision: str = "",
    storage_dtype: str = DEFAULT_DTYPE,
    normalization: str = "l2",
    input_format: str = "v1",
    dense_floor: Optional[float] = None,
    note: str = "",
    settings: Optional[Settings] = None,
) -> EmbeddingGeneration:
    """Register a new contract in `building`. The caller owns the commit.

    Validated here rather than at activation because a generation whose dtype is
    a typo would otherwise be discovered only after a corpus had been written
    under it, and every one of those vectors would have to be thrown away.
    """
    settings = settings or get_settings()
    if storage_dtype not in DTYPE_WIDTHS:
        raise ValueError(f"unknown storage dtype: {storage_dtype!r}")
    if dimensions <= 0:
        raise ValueError(f"dimensions must be positive, got {dimensions}")
    if normalization not in ("l2", "none"):
        raise ValueError(f"unknown normalization: {normalization!r}")
    floor = (
        dense_floor
        if dense_floor is not None
        else calibrated_floor(dimensions, settings.retrieval_dense_floor)
    )
    generation = EmbeddingGeneration(
        model=model,
        revision=revision,
        dimensions=dimensions,
        storage_dtype=storage_dtype,
        normalization=normalization,
        input_format=input_format,
        dense_floor=floor,
        status="building",
        note=note,
    )
    db.add(generation)
    db.flush()
    return generation


def configured_contract(settings: Optional[Settings] = None) -> Tuple[str, int, str]:
    """(model, dimensions, dtype) this process is configured to write."""
    settings = settings or get_settings()
    return (
        settings.openai_embedding_model,
        settings.openai_embedding_dimensions,
        settings.embedding_storage_dtype,
    )


def writable_generation(
    db: Session, settings: Optional[Settings] = None
) -> EmbeddingGeneration:
    """The generation new vectors belong in. The caller owns the commit.

    This is where a configuration change stops being destructive. Previously,
    editing the embedding model orphaned the entire corpus the moment the process
    restarted, because the reader's filter was the configuration itself. Here, a
    configuration that does not match the active contract opens a *new* generation
    in `building` and writes there, while readers carry on with the active one.
    Retrieval quality is untouched until someone finishes the build and flips it.

    The exception is a database with no active generation at all — a fresh
    install, or the first embed after a migration that found nothing to describe.
    There is no corpus to protect there, so the new generation is activated
    immediately and the first vector written is readable.
    """
    settings = settings or get_settings()
    model, dimensions, dtype = configured_contract(settings)
    current = active_generation(db)
    if (
        current is not None
        and current.model == model
        and current.dimensions == dimensions
        and current.storage_dtype == dtype
    ):
        return current
    building = db.execute(
        select(EmbeddingGeneration)
        .where(
            EmbeddingGeneration.status == "building",
            EmbeddingGeneration.model == model,
            EmbeddingGeneration.dimensions == dimensions,
            EmbeddingGeneration.storage_dtype == dtype,
        )
        .order_by(EmbeddingGeneration.created_at.desc(), EmbeddingGeneration.id)
    ).scalars().first()
    if building is not None:
        return building
    generation = create_generation(
        db,
        model=model,
        dimensions=dimensions,
        storage_dtype=dtype,
        note=(
            "Opened automatically: the configured contract did not match the "
            "active generation."
            if current is not None
            else "First generation for this deployment."
        ),
        settings=settings,
    )
    if current is None:
        # Nothing to migrate from, so nothing to protect. `activate` still runs
        # its coverage check, which is trivially satisfied.
        activate(db, generation)
    else:
        logger.info(
            "configuration (%s/%d/%s) differs from active generation %s "
            "(%s/%d/%s); opened %s for building — retrieval continues on the "
            "active generation until it is backfilled and activated",
            model,
            dimensions,
            dtype,
            current.id,
            current.model,
            current.dimensions,
            current.storage_dtype,
            generation.id,
        )
    return generation


def coverage(db: Session, generation: EmbeddingGeneration) -> Coverage:
    """How much of the corpus this generation holds, per table."""
    rows: List[TableCoverage] = []
    for table, kind in OWNER_KINDS.items():
        covered = int(
            db.execute(
                select(func.count(EmbeddingVector.id)).where(
                    EmbeddingVector.generation_id == generation.id,
                    EmbeddingVector.owner_kind == kind,
                )
            ).scalar_one()
        )
        # Owners holding a vector under *any* contract. The ones missing from
        # `covered` hold one under a different generation, which is real work
        # outstanding; see `Coverage` for why rows nothing has ever embedded are
        # counted separately instead.
        embedded_anywhere = int(
            db.execute(
                select(func.count(func.distinct(EmbeddingVector.owner_id))).where(
                    EmbeddingVector.owner_kind == kind
                )
            ).scalar_one()
        )
        total = int(db.execute(select(func.count(table.id))).scalar_one())
        rows.append(
            TableCoverage(
                table=table.__tablename__,
                covered=covered,
                pending=max(0, embedded_anywhere - covered),
                unembedded=max(0, total - embedded_anywhere),
            )
        )
    return Coverage(tables=rows)


def store_vector(
    db: Session,
    *,
    generation: EmbeddingGeneration,
    owner_kind: str,
    owner_id: str,
    workspace_id: str,
    vector: bytes,
    content_hash: str = "",
) -> None:
    """Write one vector into a generation, replacing any it already held.

    The caller owns the commit. Replace-in-place rather than insert-only because
    every writer here is re-runnable by design — a reconcile pass, a retried
    ingest, a backfill resumed after an interruption — and the unique constraint
    would turn "we already did this one" into a crash instead of a no-op.
    """
    existing = db.execute(
        select(EmbeddingVector).where(
            EmbeddingVector.generation_id == generation.id,
            EmbeddingVector.owner_kind == owner_kind,
            EmbeddingVector.owner_id == owner_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.vector = vector
        existing.content_hash = content_hash
        existing.workspace_id = workspace_id
        return
    db.add(
        EmbeddingVector(
            generation_id=generation.id,
            owner_kind=owner_kind,
            owner_id=owner_id,
            workspace_id=workspace_id,
            vector=vector,
            content_hash=content_hash,
        )
    )


def drop_vectors(db: Session, *, owner_kind: str, owner_ids: List[str]) -> None:
    """Delete an owner's vectors across every generation. Caller owns the commit.

    Across *every* generation, not just the active one: a deleted chunk is
    deleted, and leaving its vector in a retired generation would resurrect it
    the moment anyone rolled back.
    """
    if not owner_ids:
        return
    db.execute(
        delete(EmbeddingVector).where(
            EmbeddingVector.owner_kind == owner_kind,
            EmbeddingVector.owner_id.in_(owner_ids),
        )
    )


def materialize_by_truncation(
    db: Session,
    *,
    source: EmbeddingGeneration,
    target: EmbeddingGeneration,
    batch_size: int = 500,
) -> int:
    """Build `target` from `source` by re-cutting its vectors. Returns rows written.

    The cheap path, and the reason a smaller embedding is worth adopting at all.
    Matryoshka models put the most information in the leading dimensions, so a
    256-dim vector is a renormalised prefix of the 1536-dim one already stored —
    measured against the provider's own `dimensions=256` output, the mean cosine
    between the two is 0.999997. A generation that would otherwise cost a
    corpus-wide re-embed (rate limits, hours, real money, and a bill that scales
    with how much someone has uploaded) costs a pass over rows we already have,
    with no provider involved.

    It follows that this only works *downward*, and only between contracts that
    differ in width or dtype. Re-cutting cannot invent dimensions the source
    never had, and it cannot change which model's geometry the numbers live in.
    A different model needs a real re-embed.
    """
    if target.model != source.model:
        raise ValueError(
            f"cannot truncate across models: {source.model!r} -> {target.model!r}; "
            "a different model requires re-embedding"
        )
    if target.dimensions > source.dimensions:
        raise ValueError(
            f"cannot truncate {source.dimensions} dimensions up to {target.dimensions}"
        )
    written = 0
    skipped = 0
    last_id = ""
    while True:
        rows = list(
            db.execute(
                select(EmbeddingVector)
                .where(
                    EmbeddingVector.generation_id == source.id,
                    EmbeddingVector.id > last_id,
                )
                .order_by(EmbeddingVector.id)
                .limit(batch_size)
            ).scalars()
        )
        if not rows:
            break
        for row in rows:
            last_id = row.id
            cut = truncate_vector(
                row.vector,
                dimensions=target.dimensions,
                source_dtype=source.storage_dtype,
                dtype=target.storage_dtype,
            )
            if cut is None:
                # Too short to cut, or an all-zero prefix. Skipped rather than
                # stored: a vector that cannot be re-cut honestly is one this
                # generation does not cover, and `activate` should see that.
                skipped += 1
                continue
            store_vector(
                db,
                generation=target,
                owner_kind=row.owner_kind,
                owner_id=row.owner_id,
                workspace_id=row.workspace_id,
                vector=cut,
                # Carried across unchanged. The text did not change — only how
                # many of its dimensions we keep — so the source's judgement about
                # freshness is still the right one.
                content_hash=row.content_hash,
            )
            written += 1
        db.flush()
    if skipped:
        logger.warning(
            "truncating %s -> %s skipped %d vector(s) that could not be re-cut",
            source.id,
            target.id,
            skipped,
        )
    return written


def activate(
    db: Session, generation: EmbeddingGeneration, *, force: bool = False
) -> EmbeddingGeneration:
    """Make this the contract readers use. The caller owns the commit.

    Refuses an incomplete generation, because activating one is not a partial
    improvement: the rows it does not cover drop out of the dense arm entirely,
    so a 90%-built generation is a 10% silent recall loss that looks exactly like
    a corpus with nothing to say. `force` exists for the deliberate case — a
    corpus whose stragglers are known-dead rows — and says so in the log.

    The retired generation keeps every vector it wrote. That is the whole point:
    rollback has to be cheaper than the failure it undoes.
    """
    if generation.status == "active":
        return generation
    report = coverage(db, generation)
    if not report.complete:
        if not force:
            raise ValueError(
                f"generation {generation.id} is incomplete and cannot be activated "
                f"({report.pending} rows still on another contract) — "
                f"{report.describe()}"
            )
        logger.warning(
            "activating incomplete embedding generation %s under force: %s",
            generation.id,
            report.describe(),
        )
    now = utcnow()
    current = active_generation(db)
    if current is not None and current.id != generation.id:
        current.status = "retired"
        current.retired_at = now
        # Flushed before the new row claims `active` so the partial unique index
        # sees one active generation rather than two mid-statement.
        db.flush()
    generation.status = "active"
    generation.activated_at = now
    generation.retired_at = None
    db.flush()
    logger.info(
        "embedding generation %s active (model=%s dims=%d dtype=%s floor=%.4f): %s",
        generation.id,
        generation.model,
        generation.dimensions,
        generation.storage_dtype,
        generation.dense_floor,
        report.describe(),
    )
    return generation


def rollback(db: Session) -> Optional[EmbeddingGeneration]:
    """Reinstate the most recently retired generation. The caller owns the commit.

    Cheap by construction: the vectors never left, so this is two status columns
    and no inference at all.
    """
    previous = db.execute(
        select(EmbeddingGeneration)
        .where(EmbeddingGeneration.status == "retired")
        .order_by(EmbeddingGeneration.retired_at.desc(), EmbeddingGeneration.id)
    ).scalars().first()
    if previous is None:
        return None
    current = active_generation(db)
    if current is not None:
        current.status = "retired"
        current.retired_at = utcnow()
        db.flush()
    previous.status = "active"
    previous.activated_at = utcnow()
    previous.retired_at = None
    db.flush()
    logger.warning(
        "rolled back to embedding generation %s (model=%s dims=%d)",
        previous.id,
        previous.model,
        previous.dimensions,
    )
    return previous
