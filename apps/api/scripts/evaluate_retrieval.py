"""Measure retrieval quality against the evaluation corpus.

The previous version of this harness could not fail: two documents, four
questions, and `limit=5` meant recall@5 returned the entire corpus and scored
100% by construction. A saturated benchmark cannot tell you whether a change
helped, which is the only thing a benchmark is for.

This one is built to discriminate:

* Ground truth is a passage, not a filename. `must_contain` has to appear in the
  retrieved text, so pulling the right document for the wrong reason is a miss.
* Questions are labelled `lexical`, `paraphrase` or `indirect` and reported
  separately. A keyword matcher should win the lexical set and lose the others;
  collapsing them into one number hides exactly the signal a retrieval change is
  trying to move.
* Thresholds are per category. A single global floor would either be so low that
  the lexical set carries it or so high that nothing passes.

Two modes:

    evaluate_retrieval.py            # this process's configuration, floors enforced
    evaluate_retrieval.py --stages   # ablate each stage in turn and compare

`--stages` builds each stage's index from scratch and needs a real embedding
provider for the dense arms, so it costs money and a couple of minutes. It also
reports, per stage, which arm found each answer — because if both arms find a
passage, fusion taught you nothing, and the questions only one arm found are the
entire case for having two (RESEARCH.md §4.3).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

# Retrieval reaches a model only for the dense arm's embeddings, and importing the
# app loads Settings, which refuses to boot the product provider without a key.
# `setdefault` so a real configuration in the environment still wins.
os.environ.setdefault("MODEL_PROVIDER", "scripted")
os.environ.setdefault(
    "SCRIPTED_MODEL_SCRIPT",
    str(Path(__file__).parents[1] / "tests" / "scripts" / "agent.json"),
)
# And the env that makes the line above legal. `scripted` is gated on APP_ENV
# being development or test — a deliberate structural refusal, so that a test
# double can never answer a production request — and `app_env` defaults to
# "production". Setting the provider without setting this asks Settings for a
# combination it exists to reject.
#
# It ran anyway for a long time because a developer's repo-root `.env` supplies
# APP_ENV, so the gap was invisible to everyone who had one and fatal to everyone
# who did not — which is exactly CI, where this harness has been failing on
# "MODEL_PROVIDER=scripted requires APP_ENV to be development or test" while
# reading like a config problem with the runner. `setdefault` like its siblings:
# a real configuration in the environment still wins.
os.environ.setdefault("APP_ENV", "development")

from app.config import Settings, get_settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import Chunk, Source, User, Workspace  # noqa: E402
from app.services.embeddings import query_embedding_cache  # noqa: E402
from app.services.ingestion import contextualize_chunks, make_chunks  # noqa: E402
from app.services.retrieval import (  # noqa: E402
    embed_chunks,
    index_chunks,
    rank_arms,
    search_evidence,
)

WORKSPACE_ID = "00000000-0000-4000-8000-000000000091"
USER_ID = "00000000-0000-4000-8000-000000000092"
RETRIEVE = 5

# Floors sit one question below the measured score of what this gate actually
# runs: BM25 + RRF with the dense arm inert, because the gate is hermetic and
# there is no embedding provider behind it. Measured there: lexical 100%,
# paraphrase 90%, indirect 80%. With a provider configured the dense arm takes
# paraphrase to 90% at mrr 0.850 and indirect to 100% (`--stages`), but a floor
# has to describe the configuration it is enforcing.
#
# One question is 10-12.5 points in these strata, so a floor tighter than one
# question would fail on noise and a looser one would let a whole category rot.
FLOORS: Dict[str, float] = {
    "lexical": 0.85,
    "paraphrase": 0.80,
    "indirect": 0.70,
}

KINDS = ("lexical", "paraphrase", "indirect")

# Each stage adds exactly one mechanism to the one above it, so a difference
# between two rows is attributable to the mechanism that separates them.
STAGES: List[Tuple[str, Dict[str, object]]] = [
    ("lexical-only", {"retrieval_bm25": False, "retrieval_hybrid": False}),
    ("+bm25", {"retrieval_bm25": True, "retrieval_hybrid": False}),
    ("+dense/rrf", {"retrieval_bm25": True, "retrieval_hybrid": True}),
    (
        "+contextual",
        {"retrieval_bm25": True, "retrieval_hybrid": True, "retrieval_contextual": True},
    ),
]


@dataclass
class Report:
    recall: Dict[str, float] = field(default_factory=dict)
    mrr: Dict[str, float] = field(default_factory=dict)
    counts: Dict[str, int] = field(default_factory=dict)
    overall: float = 0.0
    misses: List[str] = field(default_factory=list)
    # Which arm ranked the gold passage in its own top-5, summed over questions.
    arms: Dict[str, int] = field(default_factory=dict)
    latency_ms: List[float] = field(default_factory=list)
    scoring_ms: List[float] = field(default_factory=list)
    embedded_chunks: int = 0
    contextualized_chunks: int = 0


def _load() -> tuple[List[dict], List[dict]]:
    path = Path(__file__).parents[1] / "evals" / "corpus.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["documents"], data["questions"]


def _seed(db: Session, documents: Sequence[dict]) -> List[Chunk]:
    db.add(Workspace(id=WORKSPACE_ID, name="Evaluation"))
    db.add(User(id=USER_ID, email="eval@example.com", name="Evaluator"))
    records: List[Chunk] = []
    for item in documents:
        source = Source(
            workspace_id=WORKSPACE_ID,
            created_by=USER_ID,
            filename=item["source"],
            media_type="text/markdown",
            object_key="/evaluation/" + item["source"],
            byte_size=len(item["text"]),
            status="ready",
        )
        db.add(source)
        db.flush()
        chunks = list(make_chunks(item["text"]))
        source.chunk_count = len(chunks)
        for ordinal, (start, end, content) in enumerate(chunks):
            record = Chunk(
                workspace_id=WORKSPACE_ID,
                source_id=source.id,
                ordinal=ordinal,
                content=content,
                char_start=start,
                char_end=end,
                token_count=len(content.split()),
            )
            db.add(record)
            records.append(record)
    db.commit()
    return records


def _prepare(db: Session, chunks: Sequence[Chunk], settings: Settings) -> Tuple[int, int]:
    """Build whatever index this stage's configuration calls for.

    Mirrors `ingestion.prepare_for_search` deliberately: measuring a pipeline the
    product does not run would be measuring nothing. Grouped by source so the
    situating blurb sees the same parent document a real ingest would show it.
    """
    contextualized = 0
    if settings.retrieval_contextual:
        by_source: Dict[str, List[Chunk]] = defaultdict(list)
        for chunk in chunks:
            by_source[chunk.source_id].append(chunk)
        for group in by_source.values():
            document = "\n\n".join(item.content for item in group)
            contextualized += len(contextualize_chunks(group, document, settings))
    index_chunks(db, chunks)
    embedded = embed_chunks(db, chunks, settings) if settings.retrieval_hybrid else 0
    db.commit()
    return contextualized, embedded


def _arm_rank(ranking: Sequence[Tuple[str, float]], gold: set[str]) -> int:
    for position, (chunk_id, _score) in enumerate(ranking[:RETRIEVE], start=1):
        if chunk_id in gold:
            return position
    return 0


def evaluate(
    db: Session,
    questions: Sequence[dict],
    settings: Settings,
    *,
    verbose: bool = True,
) -> Report:
    report = Report()
    per_kind_rr: Dict[str, List[float]] = defaultdict(list)
    per_kind_hit: Dict[str, List[int]] = defaultdict(list)
    arms = ArmTally()

    gold_by_question = _gold_by_question(db, questions)

    for item in questions:
        kind = item["kind"]
        query_embedding_cache.clear()  # a fresh question is a cold cache in production
        started = time.perf_counter()
        results = search_evidence(
            db,
            workspace_id=WORKSPACE_ID,
            query=item["question"],
            limit=RETRIEVE,
            settings=settings,
        )
        report.latency_ms.append((time.perf_counter() - started) * 1000)
        # Second pass with the query vector cached: the difference between the two
        # is the embedding round-trip, which is the only latency the dense arm adds.
        warm = time.perf_counter()
        search_evidence(
            db,
            workspace_id=WORKSPACE_ID,
            query=item["question"],
            limit=RETRIEVE,
            settings=settings,
        )
        report.scoring_ms.append((time.perf_counter() - warm) * 1000)

        # A hit requires the right document AND the passage that answers it,
        # so a lucky filename match without the evidence does not count.
        rank = 0
        for position, result in enumerate(results, start=1):
            if (
                result.filename == item["source"]
                and item["must_contain"].lower() in result.excerpt.lower()
            ):
                rank = position
                break
        per_kind_hit[kind].append(1 if rank else 0)
        per_kind_rr[kind].append(1.0 / rank if rank else 0.0)
        if not rank:
            report.misses.append(f"  [{kind}] {item['question']}")

        gold = gold_by_question[id(item)]
        rankings = rank_arms(
            db, workspace_id=WORKSPACE_ID, query=item["question"], settings=settings
        )
        lexical_rank = _arm_rank(rankings.lexical, gold)
        dense_rank = _arm_rank(rankings.dense, gold)
        arms.add(lexical_rank, dense_rank)
        if verbose:
            print(
                ("PASS" if rank else "MISS"),
                f"[{kind}]",
                item["question"],
                f"(lexical#{lexical_rank or '-'} dense#{dense_rank or '-'})",
            )

    overall_hits: List[int] = []
    for kind in KINDS:
        hits = per_kind_hit.get(kind, [])
        if not hits:
            continue
        overall_hits.extend(hits)
        report.recall[kind] = sum(hits) / len(hits)
        report.mrr[kind] = sum(per_kind_rr[kind]) / len(per_kind_rr[kind])
        report.counts[kind] = len(hits)
    report.overall = sum(overall_hits) / len(overall_hits) if overall_hits else 0.0
    report.arms = arms.totals
    return report


class ArmTally:
    """Per-arm attribution: which arm had the answer in its own top-5."""

    def __init__(self) -> None:
        self.totals = {"both": 0, "lexical only": 0, "dense only": 0, "neither": 0}

    def add(self, lexical_rank: int, dense_rank: int) -> None:
        if lexical_rank and dense_rank:
            self.totals["both"] += 1
        elif lexical_rank:
            self.totals["lexical only"] += 1
        elif dense_rank:
            self.totals["dense only"] += 1
        else:
            self.totals["neither"] += 1


def _gold_by_question(db: Session, questions: Sequence[dict]) -> Dict[int, set[str]]:
    """Which chunks actually answer each question: right source, right passage.

    The same standard `search_evidence` is scored against, resolved to chunk ids
    so an arm's ranking — which is ids, before any excerpt exists — can be graded
    the same way.
    """
    rows = db.execute(
        select(Chunk.id, Chunk.content, Source.filename).join(
            Source, Source.id == Chunk.source_id
        )
    ).all()
    gold: Dict[int, set[str]] = {}
    for item in questions:
        needle = item["must_contain"].lower()
        gold[id(item)] = {
            chunk_id
            for chunk_id, content, filename in rows
            if filename == item["source"] and needle in content.lower()
        }
    return gold


def _print_report(report: Report, *, floors: Optional[Dict[str, float]]) -> List[str]:
    failures: List[str] = []
    print()
    for kind in KINDS:
        if kind not in report.recall:
            continue
        recall = report.recall[kind]
        floor = (floors or {}).get(kind)
        status = "ok " if floor is None or recall >= floor else "LOW"
        suffix = f"  floor={floor:.0%}" if floor is not None else ""
        print(
            f"{status} {kind:11} recall@{RETRIEVE}={recall:5.1%} "
            f"mrr={report.mrr[kind]:5.3f}  n={report.counts[kind]:2}{suffix}"
        )
        if floor is not None and recall < floor:
            failures.append(f"{kind} recall {recall:.1%} is below its {floor:.0%} floor")
    total = sum(report.counts.values())
    print(f"\noverall recall@{RETRIEVE}={report.overall:.1%} questions={total}")
    print(
        "arms: "
        + ", ".join(f"{name} {count}" for name, count in report.arms.items())
    )
    if report.latency_ms:
        print(
            f"latency ms: p50={statistics.median(report.latency_ms):.1f} "
            f"p95={_p95(report.latency_ms):.1f} "
            f"(scoring only p50={statistics.median(report.scoring_ms):.1f} "
            f"p95={_p95(report.scoring_ms):.1f})"
        )
    if report.misses:
        print("\nmissed:")
        for line in report.misses:
            print(line)
    return failures


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return ordered[index]


def _fresh_db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def run_once(settings: Settings, *, floors: Optional[Dict[str, float]]) -> Tuple[Report, List[str]]:
    documents, questions = _load()
    db = _fresh_db()
    try:
        chunks = _seed(db, documents)
        contextualized, embedded = _prepare(db, chunks, settings)
        report = evaluate(db, questions, settings)
        report.contextualized_chunks = contextualized
        report.embedded_chunks = embedded
        failures = _print_report(report, floors=floors)
        return report, failures
    finally:
        db.close()


def run_stages() -> None:
    """Every stage, from the lexical baseline up, on identical data."""
    base = Settings(model_provider="openai")
    if not base.has_openai_key:
        print(
            "--stages needs OPENAI_API_KEY: the dense and contextual stages are "
            "the ones being measured, and there is no offline stand-in for them "
            "that would mean anything."
        )
        raise SystemExit(2)

    reports: List[Tuple[str, Report]] = []
    for name, overrides in STAGES:
        settings = base.model_copy(
            update={"retrieval_contextual": False, **overrides}
        )
        print(f"\n{'=' * 70}\nstage: {name}  {overrides}\n{'=' * 70}")
        report, _ = run_once(settings, floors=None)
        reports.append((name, report))

    print(f"\n{'=' * 70}\nsummary\n{'=' * 70}")
    header = f"{'stage':<14}" + "".join(f"{kind:>22}" for kind in KINDS) + f"{'overall':>10}"
    print(header)
    for name, report in reports:
        cells = "".join(
            f"{report.recall.get(kind, 0.0):>13.1%} /{report.mrr.get(kind, 0.0):>7.3f}"
            for kind in KINDS
        )
        print(f"{name:<14}{cells}{report.overall:>10.1%}")
    print()
    for name, report in reports:
        print(
            f"{name:<14} embedded={report.embedded_chunks:<4} "
            f"contextualized={report.contextualized_chunks:<4} "
            f"p50={statistics.median(report.latency_ms):6.1f}ms "
            f"p95={_p95(report.latency_ms):6.1f}ms  "
            + ", ".join(f"{key} {value}" for key, value in report.arms.items())
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stages",
        action="store_true",
        help="ablate each retrieval stage in turn (needs an embedding provider)",
    )
    args = parser.parse_args()
    if args.stages:
        run_stages()
        return
    _report, failures = run_once(get_settings(), floors=FLOORS)
    if failures:
        print()
        for line in failures:
            print("FAIL:", line)
        raise SystemExit(1)


if __name__ == "__main__":
    sys.exit(main())
