"""The coverage ledger: what a research run actually looked at, as data.

ZERO model calls anywhere in this module, and that is the whole design rather
than an optimisation. The thing being reported on is a model's tendency to
answer a two-sided question from one side; a classifier or a counter-query that
was itself model output would inherit exactly that bias and the report would
certify its own failure mode. So the shape classifier is a word test, both
counter-queries are fixed templates, and the renderer is byte-stable — which
also makes the whole module safe to run in CI and measurable by an eval gate.

THE CALLER CONTRACT. A producer calls five functions in this order:

    ledger = open_ledger(db, workspace_id=..., question=...)
    record_step(db, ledger=ledger, sub_question=..., evidence=...)   # per step
    counter_evidence_pass(db, ledger=ledger, settings=...)           # after the last
    close_ledger(db, ledger=ledger)
    report += render(db, ledger=ledger)

THE PRODUCER IS PLAN MODE, in `services/step_plan.py`: `submit_plan` opens the
ledger, each `complete_step` records one entry from the passages that step
named, and the last step runs the counter-evidence pass and closes it. That is
the only producer in the app, and saying so here is load-bearing — this
docstring used to claim the deliverable preset called the same five, which it
never did: `deliverables.write_manifest` only READS a ledger to copy an id, so
for one cycle the classifier, the counter-evidence pass, the renderer, the
route, the drawer and two migration tables were all reachable and all fed by
nothing. Every test hand-built the writer production lacked, so the suite was
green. A test that drives a real plan-mode turn now pins the call sites.

Nothing commits. Routes and executors own transactions.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Chunk, CoverageEntry, CoverageLedger
from . import retrieval
from .retrieval import Evidence

logger = logging.getLogger(__name__)

#: The words that make a question two-sided. Lowercase, and space-padded where
#: the word would otherwise match inside another one.
#:
#: SUPERLATIVES ARE DELIBERATELY ABSENT. " best " and " worst " were tried and
#: removed: the eval corpus's "What does the Best Practices doc say about
#: deletion?" is a plain lookup, and space-padding does not save it, because
#: "the Best Practices doc" really does contain " best ". A superlative turns up
#: inside proper nouns and ordinary prose; a COMPARATIVE ("better than",
#: "versus", "instead of") does not, which is why every marker here is one. The
#: superlative questions that genuinely are two-sided are still caught, by
#: "which is " and "compare".
DEBATE_MARKERS: Tuple[str, ...] = (
    " better ",
    " worse ",
    " versus ",
    " vs ",
    "compare",
    "comparison",
    "should we ",
    "should i ",
    "pros and cons",
    "trade-off",
    "tradeoff",
    "advantages",
    "disadvantages",
    " prefer ",
    "instead of",
    "rather than",
    "outperform",
    " superior ",
    " inferior ",
    "downside",
    "upside",
    "worth it",
    "which is ",
)


def classify(question: str) -> str:
    """"debate" or "plain". Pure, and deliberately conservative.

    A false positive costs two retrievals for nothing; a false negative costs
    a one-sided answer nobody was warned about. The markers are still kept
    narrow rather than generous, because a question forced through a
    counter-evidence pass it did not need produces a "Both sides" block that
    says nothing — and a report section people learn to skip is worse than one
    that occasionally stays quiet.

    A two-sided question carrying no marker word at all classifies plain. That
    is a known limit, recorded in the eval corpus rather than papered over.
    """
    padded = " " + " ".join(question.lower().split()) + " "
    return "debate" if any(marker in padded for marker in DEBATE_MARKERS) else "plain"


def counter_queries(question: str) -> Tuple[str, str]:
    """The two fixed retrievals a debate question forces. Pure.

    Templates rather than model-written queries: a counter-query the model
    composed would be as one-sided as the answer this pass exists to check.
    """
    return (f"evidence supporting: {question}", f"evidence against: {question}")


def open_ledger(
    db: Session,
    *,
    workspace_id: str,
    question: str,
    run_id: str = "",
    workflow_run_id: str = "",
    space_id: str = "",
    conversation_id: str = "",
) -> CoverageLedger:
    """Start a ledger, with the denominator fixed at the moment of asking.

    `in_scope_count` comes from `retrieval.in_scope_source_count`, the public
    counter that shares `_live_sources` with the ranking arms — so "sources
    consulted of in scope" cannot come to disagree with what retrieval could
    actually have reached.

    SOURCES, not chunks, and the distinction is the whole line: `close_ledger`
    fills the numerator from distinct `Evidence.source_id` values, which are
    documents. Counting the denominator in passages made the report read "6 of
    4,000 sources consulted" for a run that had read six of forty documents —
    a hundredfold understatement in the one number this module exists to state
    honestly, and unfixable afterwards because the denominator is frozen here.
    """
    ledger = CoverageLedger(
        workspace_id=workspace_id,
        run_id=run_id,
        workflow_run_id=workflow_run_id,
        question=question,
        shape=classify(question),
        in_scope_count=retrieval.in_scope_source_count(
            db,
            workspace_id=workspace_id,
            space_id=space_id,
            conversation_id=conversation_id,
        ),
    )
    db.add(ledger)
    db.flush()
    return ledger


def record_step(
    db: Session,
    *,
    ledger: CoverageLedger,
    sub_question: str,
    evidence: Sequence[Evidence],
    query: str = "",
    stance: str = "neutral",
) -> CoverageEntry:
    """One sub-question the run posed, and whether anything in scope answered it.

    `supported` is exactly "retrieval returned something" — not entailment.
    Nothing in this module claims a passage supports a claim; that is cluster
    A's verified-grounding score, and conflating the two would be the report
    overstating itself.
    """
    ordinal = int(
        db.scalar(
            select(func.count(CoverageEntry.id)).where(
                CoverageEntry.ledger_id == ledger.id,
                CoverageEntry.workspace_id == ledger.workspace_id,
            )
        )
        or 0
    )
    chunk_ids: List[str] = []
    source_ids: List[str] = []
    for item in evidence:
        if item.chunk_id and item.chunk_id not in chunk_ids:
            chunk_ids.append(item.chunk_id)
        if item.source_id and item.source_id not in source_ids:
            source_ids.append(item.source_id)
    entry = CoverageEntry(
        workspace_id=ledger.workspace_id,
        ledger_id=ledger.id,
        ordinal=ordinal,
        sub_question=sub_question,
        stance=stance,
        supported=bool(evidence),
        query=query or sub_question,
        chunk_ids_json=json.dumps(chunk_ids),
        source_ids_json=json.dumps(source_ids),
    )
    db.add(entry)
    db.flush()
    return entry


def record_step_chunks(
    db: Session,
    *,
    ledger: CoverageLedger,
    sub_question: str,
    chunk_ids: Sequence[str],
    query: str = "",
    stance: str = "neutral",
) -> CoverageEntry:
    """`record_step` for a producer that holds chunk ids rather than `Evidence`.

    Plan-mode is that producer: a step reports the passages it leaned on as ids
    (`complete_step`'s `chunk_ids`), and the retrieval that produced them
    happened several tool calls earlier, inside the agent loop.

    The ids are RESOLVED here, workspace-scoped, and anything that does not
    resolve is dropped. Two reasons, and both are about the ledger meaning
    something: the ids come from a model, so an invented one must not be able
    to inflate "sources consulted"; and the `source_id` behind each chunk is
    what the numerator counts, so it has to come from the row, not from the
    claim.
    """
    wanted = [str(value) for value in chunk_ids if str(value)]
    resolved: List[Evidence] = []
    if wanted:
        rows = db.execute(
            select(Chunk.id, Chunk.source_id).where(
                Chunk.workspace_id == ledger.workspace_id,
                Chunk.id.in_(wanted),
            )
        ).all()
        resolved = [
            Evidence(
                chunk_id=str(row[0]),
                source_id=str(row[1]),
                filename="",
                ordinal=0,
                excerpt="",
                score=0.0,
            )
            for row in rows
        ]
    return record_step(
        db,
        ledger=ledger,
        sub_question=sub_question,
        evidence=resolved,
        query=query,
        stance=stance,
    )


def counter_evidence_pass(
    db: Session,
    *,
    ledger: CoverageLedger,
    settings: Optional[Settings] = None,
    space_id: str = "",
    conversation_id: str = "",
) -> None:
    """Retrieve BOTH sides of a debate question, whether or not anyone asked.

    This is the forced step. A model that answered a comparison from one side
    will not ask for the other, so the pass cannot be conditional on the
    model's behaviour without inheriting it. A plain ledger is a no-op.
    """
    if ledger.shape != "debate":
        return
    settings = settings or get_settings()
    supporting, against = counter_queries(ledger.question)
    for stance, query in (("for", supporting), ("against", against)):
        evidence = retrieval.search_evidence(
            db,
            workspace_id=ledger.workspace_id,
            query=query,
            space_id=space_id,
            conversation_id=conversation_id,
            settings=settings,
        )
        record_step(
            db,
            ledger=ledger,
            sub_question=query,
            evidence=evidence,
            query=query,
            stance=stance,
        )


def close_ledger(
    db: Session,
    *,
    ledger: CoverageLedger,
    consulted_evidence: Sequence[Evidence] = (),
) -> None:
    """Total up what was consulted, and decide whether the corpus is one-sided.

    `consulted_evidence` is what the RUN as a whole had in hand — the turn's
    own evidence list, which the producer holds and the per-step entries can
    only partly account for. A plan step reports the passages it says it used,
    and a step that reports none is not evidence that the run read nothing, so
    the union is the honest numerator: every source that reached the answer,
    plus every source a step named. Defaulting it to empty keeps a producer
    that only has entries (the counter-evidence pass, and every existing
    caller) exactly as it was.
    """
    entries = _entries(db, ledger)
    consulted: List[str] = []
    for item in consulted_evidence:
        if item.source_id and item.source_id not in consulted:
            consulted.append(item.source_id)
    for entry in entries:
        try:
            source_ids = json.loads(entry.source_ids_json or "[]")
        except ValueError:
            source_ids = []
        for source_id in source_ids:
            if isinstance(source_id, str) and source_id not in consulted:
                consulted.append(source_id)
    ledger.consulted_count = len(consulted)
    if ledger.shape != "debate":
        ledger.one_sided = False
        return
    supported = {
        entry.stance for entry in entries if entry.stance in ("for", "against")
        and entry.supported
    }
    ledger.one_sided = not ({"for", "against"} <= supported)


def render(db: Session, *, ledger: CoverageLedger) -> str:
    """The report section, byte for byte. Pinned by a test.

    One renderer for the API and the report, so a reader comparing the two
    cannot find them disagreeing.
    """
    entries = _entries(db, ledger)
    posed = [entry for entry in entries if entry.stance == "neutral"]
    unsupported = [entry for entry in posed if not entry.supported]
    lines = [
        "## Coverage",
        "",
        f"- Sub-questions posed: {len(posed)}",
        f"- Sources consulted: {ledger.consulted_count} of "
        f"{ledger.in_scope_count} in scope",
        f"- Unsupported sub-questions: {len(unsupported)}",
    ]
    for entry in unsupported:
        # "No passage was recorded", not "nothing in scope supported this":
        # the two producers know different things. The counter-evidence pass
        # ran a real retrieval and found nothing; a plan step recorded the
        # passages it said it used, and naming none is not the same fact as
        # the corpus holding none. One line has to be true of both.
        lines.append(
            f"  - “{entry.sub_question}” — no passage was recorded for this."
        )
    if ledger.shape == "debate":
        counts: Dict[str, int] = {"for": 0, "against": 0}
        for entry in entries:
            if entry.stance in counts and entry.supported:
                counts[entry.stance] += len(_chunk_ids(entry))
        lines.extend(
            [
                "",
                "### Both sides",
                "",
                f"- Supporting passages: {counts['for']}",
                f"- Opposing passages: {counts['against']}",
            ]
        )
        if ledger.one_sided:
            lines.append("")
            lines.append(
                "The corpus is one-sided on this question: no counter-evidence "
                "was found."
            )
    return "\n".join(lines) + "\n"


def _entries(db: Session, ledger: CoverageLedger) -> List[CoverageEntry]:
    return list(
        db.scalars(
            select(CoverageEntry)
            .where(
                CoverageEntry.ledger_id == ledger.id,
                CoverageEntry.workspace_id == ledger.workspace_id,
            )
            .order_by(CoverageEntry.ordinal)
        )
    )


def _chunk_ids(entry: CoverageEntry) -> List[str]:
    try:
        parsed = json.loads(entry.chunk_ids_json or "[]")
    except ValueError:
        return []
    return [item for item in parsed if isinstance(item, str)]


def ledger_for_run(
    db: Session, *, workspace_id: str, run_id: str
) -> Optional[CoverageLedger]:
    """The newest ledger for this run, or None. Workspace-scoped by construction."""
    if not run_id:
        return None
    return db.scalar(
        select(CoverageLedger)
        .where(
            CoverageLedger.workspace_id == workspace_id,
            CoverageLedger.run_id == run_id,
        )
        .order_by(CoverageLedger.created_at.desc(), CoverageLedger.id)
        .limit(1)
    )


def entries_for(db: Session, *, ledger: CoverageLedger) -> List[CoverageEntry]:
    """The ledger's entries in order, for a caller outside this module."""
    return _entries(db, ledger)
