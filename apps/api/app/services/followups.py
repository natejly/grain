"""Corpus-grounded follow-up chips: the next questions this corpus can answer.

Three rules make this feature worth having rather than decorative, and all
three are structural rather than stylistic:

**The candidates are derived, not written.** They come from the knowledge
graph's neighbours of the entities in the cited passages, and from the answer's
own headings — both of them facts about text we already have, produced by pure
functions over an ordering that is total. Two runs over one corpus produce a
byte-identical chip list, which is the only reason an eval gate can measure
this at all.

**Every candidate is probed before it is offered.** A chip is a promise that
the workspace has something to say; offering one the corpus cannot answer is
worse than offering nothing, because the person spends a turn finding out. The
probe is the dense arm at the generation's OWN floor — read from
`embedding_generations.effective_floor`, never derived. Cosine between
unrelated vectors rises as dimensionality falls, and the tempting closed form
(scale by sqrt(d)) discards 27 of 28 true answers; see
`CALIBRATED_DENSE_FLOORS`' note, "Measure; do not derive".

**The probe sees exactly what the turn saw.** Both scope axes — the thread's
space and the thread itself — are passed through to `dense_ranking`, because a
probe that forgot an axis would happily offer a chip about a file attached to
somebody else's chat.

The optional polish pass may only REWRITE an admitted candidate, never add,
reorder or drop one, and every rewrite is re-probed with the original as the
fallback. Under the scripted provider it is the identity function, so CI never
reaches a live provider and the eval gate measures the deterministic path.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from itertools import chain
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import GraphEntity
from . import embedding_generations as generations
from . import graph, retrieval
from .retrieval import Evidence

logger = logging.getLogger(__name__)

#: At most three chips. A shelf of suggestions is a menu, and a menu is what
#: people stop reading.
MAX_FOLLOWUPS = 3
#: The hard probe ceiling for one run. Each probe is one `dense_ranking` call
#: and, on a cache miss, one embedding round-trip of 50-200ms; this cap is the
#: whole of the latency budget.
MAX_CANDIDATES = 8
#: How many chunk ids a chip carries as its evidence that it is answerable.
PROBE_LIMIT = 3
#: How many CONTENT terms a candidate's own text must contribute. Applied to
#: the heading or the entity pair, NOT to the rendered question: the fixed
#: template already contains "workspace", so a check on the finished string
#: would pass every candidate and the guard would be decoration. A heading of
#: pure stopwords would otherwise make the dense arm rank the entire corpus by
#: similarity to "what is the" — `search_evidence`'s own stated failure.
MIN_QUERY_TERMS = 1
MAX_HEADINGS = 8
MAX_KG_ENTITIES = 6
MAX_KG_NEIGHBORS = 6
#: How many DISTINCT cited chunks the graph half consults. Each one costs an
#: unindexable LIKE scan over the workspace's entity projection plus a BFS per
#: entity it finds, and that work used to scale with the evidence list — which
#: a plan-execute or council run fills with dozens of passages — while the
#: probe cap bounded only what happened after. The first few cited passages are
#: where an answer's subject lives; the tail rarely adds a new entity, and it
#: cannot add more than `MAX_CANDIDATES` probes' worth of value.
MAX_CITED_CHUNKS = 4

_ATX_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_BOLD_HEADING = re.compile(r"^\*\*(.+?)\*\*\s*$")
#: Trailing punctuation a heading carries and a question should not.
_TRAILING_PUNCTUATION = " \t:;,.-—–·*"


@dataclass(frozen=True)
class Followup:
    """One admitted chip: what to ask, where it came from, what answers it.

    `origin` is "kg" (a graph neighbour — a claim about the corpus) or
    "heading" (a claim about the answer). `chunk_ids` are the passages the
    probe found, kept so a later reader can see the chip was grounded rather
    than trust that it was.
    """

    text: str
    origin: str
    probe_score: float
    chunk_ids: Tuple[str, ...]


def heading_candidates(answer: str) -> List[str]:
    """One question per heading in the answer, in document order.

    ATX headings and whole-line bold runs both count: models reach for either
    when they structure a long answer, and a feature that only understood `#`
    would go quiet on exactly the answers worth following up.
    """
    seen: Dict[str, str] = {}
    for line in answer.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = _ATX_HEADING.match(stripped) or _BOLD_HEADING.match(stripped)
        if match is None:
            continue
        heading = match.group(1).strip().strip(_TRAILING_PUNCTUATION).strip()
        if not heading:
            continue
        if len(retrieval.query_terms(heading)) < MIN_QUERY_TERMS:
            # A heading of pure stopwords contributes nothing to look up.
            continue
        key = heading.casefold()
        if key in seen:
            continue
        # The ORIGINAL casing is kept; the casefold is only the dedupe key. A
        # chip that lower-cased a proper noun reads as a different question.
        seen[key] = heading
        if len(seen) >= MAX_HEADINGS:
            break
    # One fixed template, so the chip text is a function of the heading and
    # nothing else — the determinism the eval gate rests on.
    return [
        f"What does the workspace say about {heading}?" for heading in seen.values()
    ]


def kg_candidates(
    db: Session, *, workspace_id: str, evidence: Sequence[Evidence]
) -> List[str]:
    """One question per (cited entity, graph neighbour) pair, deterministically.

    The ordering is total — entity mention_count desc, entity name asc,
    neighbour weight desc, neighbour name asc — so two runs over one corpus
    produce the same list in the same order. An empty list is a perfectly good
    answer: the projection may be stale or may never have been built, and chips
    are an enhancement rather than a promise.

    The whole list, for callers that want it (the eval gate, tests). `suggest`
    consumes the generator instead, so on the hot path the scans stop as soon
    as it has enough chips.
    """
    return list(kg_candidate_stream(db, workspace_id=workspace_id, evidence=evidence))


def kg_candidate_stream(
    db: Session, *, workspace_id: str, evidence: Sequence[Evidence]
) -> Iterator[str]:
    """`kg_candidates`, yielded — same order, same values, bounded work.

    LAZY ON PURPOSE. The per-chunk entity scan and the per-entity BFS used to
    run to completion before the caller admitted its first chip, so the cost
    was evidence-count x MAX_KG_ENTITIES whatever the probe cap said. Yielding
    lets `suggest` stop the scans the moment it has `MAX_FOLLOWUPS` chips or
    has spent `MAX_CANDIDATES` probes.

    Cited chunks are deduplicated and capped (`MAX_CITED_CHUNKS`) before the
    first query: the same chunk cited twice used to repeat the identical scan.
    """
    seen_text: set[str] = set()
    seen_entities: set[str] = set()
    chunk_ids: List[str] = []
    for item in evidence:
        if item.chunk_id and item.chunk_id not in chunk_ids:
            chunk_ids.append(item.chunk_id)
        if len(chunk_ids) >= MAX_CITED_CHUNKS:
            break
    for chunk_id in chunk_ids:
        entities = list(
            db.scalars(
                select(GraphEntity)
                .where(
                    GraphEntity.workspace_id == workspace_id,
                    # A LIKE-shaped containment over the projection's own id
                    # list: a scan of this workspace's entities per cited
                    # chunk. Bounded two ways — at most MAX_CITED_CHUNKS
                    # distinct chunks, and the generator stops as soon as the
                    # caller has its chips — and an empty result is valid.
                    GraphEntity.chunk_ids_json.contains(chunk_id),
                )
                .order_by(GraphEntity.mention_count.desc(), GraphEntity.name.asc())
                .limit(MAX_KG_ENTITIES)
            )
        )
        for entity in entities:
            if entity.id in seen_entities:
                continue
            seen_entities.add(entity.id)
            found, _truncated = graph.neighbors(
                db, workspace_id, entity, max_hops=1, limit=MAX_KG_NEIGHBORS
            )
            for neighbor in sorted(
                found, key=lambda item: (-item.weight, item.name)
            ):
                if neighbor.name.casefold() == entity.name.casefold():
                    continue
                text = f"How does {entity.name} relate to {neighbor.name}?"
                key = text.casefold()
                if key in seen_text:
                    continue
                seen_text.add(key)
                yield text


def probe(
    db: Session,
    *,
    workspace_id: str,
    query: str,
    space_id: str = "",
    conversation_id: str = "",
    settings: Optional[Settings] = None,
) -> Tuple[float, List[str]]:
    """Does this corpus have anything above the floor to say about `query`?

    THE FLOOR IS READ, NEVER DERIVED. `effective_floor` returns the generation's
    own calibrated value unless this deployment set `RETRIEVAL_DENSE_FLOOR`
    explicitly, in which case the operator's statement wins. Each calibrated
    value reproduces the 1536/0.30 selectivity at that width; the closed form
    that looks principled — scaling by sqrt(1536/d) — discards 27 of the 28
    corpus answers, because aligned pairs do not scale the way near-orthogonal
    ones do (`CALIBRATED_DENSE_FLOORS`: "Measure; do not derive").

    `dense_ranking` already truncates at that floor, so a non-empty ranking is
    above it by construction. The explicit re-check below is deliberate
    belt-and-braces: a future change inside the dense arm must not be able to
    start admitting noise here silently.
    """
    settings = settings or get_settings()
    generation = generations.active_generation(db)
    if generation is None:
        return (0.0, [])
    floor = generations.effective_floor(generation, settings)
    ranked = retrieval.dense_ranking(
        db,
        workspace_id=workspace_id,
        query=query,
        space_id=space_id,
        conversation_id=conversation_id,
        settings=settings,
    )
    if not ranked:
        return (0.0, [])
    if ranked[0][1] < floor:
        return (0.0, [])
    return (ranked[0][1], [chunk_id for chunk_id, _score in ranked[:PROBE_LIMIT]])


def suggest(
    db: Session,
    *,
    workspace_id: str,
    answer: str,
    evidence: Sequence[Evidence],
    space_id: str = "",
    conversation_id: str = "",
    settings: Optional[Settings] = None,
) -> List[Followup]:
    """The chips this answer earns: at most three, each probed and grounded.

    KG candidates come first because a graph neighbour is a claim about the
    corpus, while a heading is a claim about the answer — and an answer's
    headings can name things the corpus never covered.

    THE CANDIDATE BUILD IS LAZY, and it has to be: this runs synchronously
    inside `_finish_run`, before `message.completed` is emitted, so every
    query here is tail latency on an answer the user has already watched
    stream in. Consuming the generators means the graph scans stop at the same
    moment the probes do, and the whole pass is bounded by `MAX_CANDIDATES`
    rather than by how much evidence the turn happened to accumulate.
    """
    settings = settings or get_settings()
    origins: Iterator[Tuple[str, str]] = chain(
        (
            (text, "kg")
            for text in kg_candidate_stream(
                db, workspace_id=workspace_id, evidence=evidence
            )
        ),
        # Pure and already bounded (MAX_HEADINGS), and it touches no database.
        ((text, "heading") for text in heading_candidates(answer)),
    )

    admitted: List[Followup] = []
    probed = 0
    seen: set[str] = set()
    for text, origin in origins:
        if len(admitted) >= MAX_FOLLOWUPS or probed >= MAX_CANDIDATES:
            break
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        if len(retrieval.query_terms(text)) < MIN_QUERY_TERMS:
            # A backstop. The candidate generators already applied this test to
            # the heading or entity names they were built from; this catches a
            # generator added later that forgets.
            continue
        probed += 1
        score, chunk_ids = probe(
            db,
            workspace_id=workspace_id,
            query=text,
            space_id=space_id,
            conversation_id=conversation_id,
            settings=settings,
        )
        if not chunk_ids:
            continue
        admitted.append(
            Followup(
                text=text,
                origin=origin,
                probe_score=score,
                chunk_ids=tuple(chunk_ids),
            )
        )
    return admitted[:MAX_FOLLOWUPS]


def polish(
    db: Session,
    items: Sequence[Followup],
    *,
    workspace_id: str,
    user_id: str,
    space_id: str = "",
    conversation_id: str = "",
    settings: Optional[Settings] = None,
) -> List[Followup]:
    """Rewrite admitted chips to read naturally. Live providers only.

    REWRITE-ONLY, and the constraint is the feature: the model may not add a
    candidate, drop one, or reorder the list, because every one of those would
    hand it the admission decision the probe exists to make. Each rewrite is
    re-probed against the same corpus, and one that no longer clears the floor
    falls back to the original string unchanged.

    Nothing retrieved is ever sent — only question strings this module
    generated — so the standing "no adversarial content to a live provider"
    rule is untouched, and the scripted branch means CI never gets here at all.
    """
    settings = settings or get_settings()
    if settings.active_model_provider != "openai":
        return list(items)
    if not items:
        return []
    # Deferred: model imports retrieval-adjacent services at module scope.
    from . import model as model_service

    try:
        rewritten = model_service.polish_followups(
            [item.text for item in items], user_id=user_id, settings=settings
        )
    except Exception:
        logger.warning("follow-up polish raised; keeping the derived text", exc_info=True)
        return list(items)
    if len(rewritten) != len(items):
        return list(items)
    polished: List[Followup] = []
    for original, text in zip(items, rewritten, strict=True):
        candidate = (text or "").strip()
        if not candidate or candidate == original.text:
            polished.append(original)
            continue
        if len(retrieval.query_terms(candidate)) < MIN_QUERY_TERMS:
            polished.append(original)
            continue
        score, chunk_ids = probe(
            db,
            workspace_id=workspace_id,
            query=candidate,
            space_id=space_id,
            conversation_id=conversation_id,
            settings=settings,
        )
        if not chunk_ids:
            polished.append(original)
            continue
        polished.append(
            Followup(
                text=candidate,
                origin=original.origin,
                probe_score=score,
                chunk_ids=tuple(chunk_ids),
            )
        )
    return polished


def serialize(items: Sequence[Followup]) -> List[Dict[str, object]]:
    """The one shape: the message column, the API payload and the run event."""
    return [
        {
            "text": item.text,
            "origin": item.origin,
            "probe_score": round(float(item.probe_score), 6),
            "chunk_ids": list(item.chunk_ids),
        }
        for item in items
    ]
