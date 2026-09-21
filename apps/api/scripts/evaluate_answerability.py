"""Measure whether a retrieved answer is actually supported by what was retrieved.

The third gate, beside evaluate_retrieval.py and evaluate_memory.py, and the
one they left a hole for. `evaluate_retrieval` measures whether the right
passage comes back. Nothing measured the step after it: whether the sentences
of an answer are supported by the passage they cite. That step is now
`services/citations.grade_grounding`, and this is the harness that keeps it
honest.

WHAT EACH FLOOR CERTIFIES — and what none of them do
----------------------------------------------------
* `FLOORS[kind]` certifies RETRIEVAL: the gold passage is in the top five for
  that stratum. Same standard as evaluate_retrieval.py (right document AND the
  passage that answers it), on a deliberately duplicated corpus.
* `GROUNDING_FLOOR` certifies the mean grounding score of the AUTHORED answers,
  over questions whose retrieval HIT. A retrieval miss is reported as
  `unretrieved` and never averaged in as a zero — a grader cannot be blamed for
  a passage it was never handed, and folding the two failures into one number
  is how a benchmark stops telling you which thing broke.
* `VALIDATOR_FLOOR` certifies `validate_citations`: every authored answer's
  `[n]` markers resolve.
* `DETECTION_FLOOR` certifies that every PLANTED defect is caught as the class
  the fixture says it is. It is 1.0 and must stay 1.0: a defect the gate stops
  catching is the only failure here that is unambiguously a regression.

None of them certifies TRUTH. `grade_grounding` is a lexical-overlap support
test — "the words this sentence uses are present in the passage it cites" — so
a high grounding score certifies word overlap and nothing about entailment. The
fixture's `blind_spots` are printed on every run for exactly that reason: a
negation and a misattribution, both assembled from the passage's own words,
both passing. If they ever stop passing, this gate's floors were measured
against a different method and must be re-baselined.

And this gate cannot observe the REPAIR pass at all. `model.regenerate_unsupported`
returns "" on every provider but openai — deliberately, because a scripted
rewrite would make the stage look measured while measuring a fixed string — so
there is nothing behind it here. The repair is exercised only by the
monkeypatched tests in tests/test_runs_grounding.py, and saying so is the point:
a green run of this script is not evidence that repair works.

    APP_ENV=development PYTHONPATH=apps/api \\
        .venv/bin/python apps/api/scripts/evaluate_answerability.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

# The same three setdefaults its two siblings carry, for the same reasons.
# Retrieval reaches a model only for the dense arm's embeddings, and importing
# the app loads Settings, which refuses to boot the product provider with no key.
os.environ.setdefault("MODEL_PROVIDER", "scripted")
os.environ.setdefault(
    "SCRIPTED_MODEL_SCRIPT",
    str(Path(__file__).parents[1] / "tests" / "scripts" / "agent.json"),
)
# And the env that makes the line above legal: `scripted` is gated on APP_ENV
# being development or test, and `app_env` defaults to "production". CI has no
# repo-root .env to supply it, which is the exact failure the ci-green work
# already paid for once.
os.environ.setdefault("APP_ENV", "development")

from app.config import Settings, get_settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import Chunk, Source, User, Workspace  # noqa: E402
from app.services.citations import (  # noqa: E402
    CITED_UNSUPPORTED,
    UNCITED,
    grade_grounding,
    validate_citations,
)
from app.services.embeddings import pack_vector, query_embedding_cache  # noqa: E402
from app.services.ingestion import make_chunks  # noqa: E402
from app.services.retrieval import (  # noqa: E402
    index_chunks,
    search_evidence,
    tokenize,
)

WORKSPACE_ID = "00000000-0000-4000-8000-000000000094"
USER_ID = "00000000-0000-4000-8000-000000000095"
RETRIEVE = 5
EMBED_DIM = 64

KINDS = ("lexical", "paraphrase", "indirect")

# Every floor sits one item below the baseline measured at build time, so this
# gate fails on a regression rather than describing an aspiration. Measured on
# the committed fixture with BM25 + RRF over the hash-bucket vectorizer below
# (this harness is hermetic; there is no embedding provider behind it):
# lexical 100% (6), paraphrase 75% (4), indirect 100% (3), grounding 100%,
# validator 100%, detection 100%.
#
# One question is 17-33 points in these strata, so a floor tighter than one item
# would fail on noise and a looser one would let a whole stratum rot. The
# paraphrase stratum already sits at 3 of 4: the miss is the invoices/bank
# question, which shares no content term with its passage and is exactly the
# case the dense arm exists for — with a real embedding provider it is a hit.
# The floor describes the configuration it enforces, not the one we would like.
FLOORS: Dict[str, float] = {
    "lexical": 0.83,
    "paraphrase": 0.50,
    "indirect": 0.66,
}
GROUNDING_FLOOR = 0.90
VALIDATOR_FLOOR = 0.91
# Not one item below: a planted defect that stops being caught is the one
# failure in this file that cannot be noise.
DETECTION_FLOOR = 1.0

#: The floor `grade_grounding` is measured at here. Stated rather than read from
#: Settings, so the number this gate certifies cannot be moved by a deployment's
#: configuration — and so the report can print what it measured.
FLOOR = 0.6


def _fake_vector(text: str, dim: int = EMBED_DIM) -> bytes:
    """A deterministic stand-in for an embedding model.

    Copied from evaluate_memory.py rather than imported, exactly as that file
    copied it from evaluate_retrieval.py: a harness that shared a fixture with
    the code under test would move when the code moved.
    """
    values = [0.0] * dim
    for token in tokenize(text):
        bucket = int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % dim
        values[bucket] += 1.0
    norm = sum(value * value for value in values) ** 0.5
    if norm:
        values = [value / norm for value in values]
    return pack_vector(values)


def _load() -> Tuple[List[dict], List[dict]]:
    path = Path(__file__).parents[1] / "evals" / "answerability.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["documents"], data["questions"]


def _seed(db: Session, documents: Sequence[dict]) -> List[Chunk]:
    """The corpus, chunked by the product's own splitter. `evaluate_retrieval._seed`."""
    db.add(Workspace(id=WORKSPACE_ID, name="Answerability"))
    db.add(User(id=USER_ID, email="answereval@example.com", name="Evaluator"))
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


def _prepare(db: Session, chunks: Sequence[Chunk], settings: Settings) -> None:
    """Build the index this configuration calls for, `ingestion.prepare_for_search`.

    The dense arm is embedded with the fake vectorizer rather than skipped, so
    the fusion the product runs is the fusion measured — a lexical-only harness
    would report a fused pipeline it never exercised.
    """
    index_chunks(db, chunks)
    if settings.retrieval_hybrid:
        for chunk in chunks:
            if chunk.embedding is None:
                chunk.embedding = _fake_vector(chunk.content)
                chunk.embedding_dim = EMBED_DIM
    db.commit()


def _gold_chunk_ids(db: Session, questions: Sequence[dict]) -> Dict[int, set]:
    """Which chunks answer each question: right source AND the passage that says it.

    `_gold_by_question`'s standard, resolved at seed time so a fixture whose
    `must_contain` stops appearing anywhere fails loudly here rather than
    scoring zero forever.
    """
    rows = db.execute(
        select(Chunk.id, Chunk.content, Source.filename).join(
            Source, Source.id == Chunk.source_id
        )
    ).all()
    gold: Dict[int, set] = {}
    for item in questions:
        needle = item["must_contain"].lower()
        found = {
            chunk_id
            for chunk_id, content, filename in rows
            if filename == item["source"] and needle in content.lower()
        }
        if not found:
            raise SystemExit(
                f"fixture error: no chunk of {item['source']} contains "
                f"{item['must_contain']!r}; the gold passage cannot be resolved"
            )
        gold[id(item)] = found
    return gold


def _verdicts(answer: str, passage: str) -> List[str]:
    return [
        sentence.verdict
        for sentence in grade_grounding(answer, [passage], floor=FLOOR).sentences
    ]


def _caught(answer: str, passage: str, expect: str) -> bool:
    """Was the planted fault caught as the class the fixture named?

    `fabricated` is `validate_citations`' job, the other two are
    `grade_grounding`'s; the fixture names the class so a defect cannot be
    counted as caught by the wrong mechanism.
    """
    if expect == "fabricated":
        return not validate_citations(answer, [passage]).is_valid
    if expect == CITED_UNSUPPORTED:
        return CITED_UNSUPPORTED in _verdicts(answer, passage)
    if expect == UNCITED:
        return UNCITED in _verdicts(answer, passage)
    raise SystemExit(f"fixture error: unknown defect class {expect!r}")


def main() -> None:
    documents, questions = _load()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    try:
        settings = get_settings()
        chunks = _seed(db, documents)
        _prepare(db, chunks, settings)
        gold_by_question = _gold_chunk_ids(db, questions)

        per_kind: Dict[str, List[int]] = defaultdict(list)
        grounding_scores: List[float] = []
        validator_hits: List[int] = []
        defects_caught = 0
        defects_total = 0
        unretrieved: List[str] = []
        blind: List[Tuple[str, str]] = []

        print(f"grounding floor = {FLOOR:.2f} (lexical coverage, not entailment)\n")

        for item in questions:
            kind = item["kind"]
            query_embedding_cache.clear()
            results = search_evidence(
                db,
                workspace_id=WORKSPACE_ID,
                query=item["question"],
                limit=RETRIEVE,
                settings=settings,
            )
            gold = gold_by_question[id(item)]
            passage = ""
            for result in results:
                if result.chunk_id in gold:
                    passage = result.excerpt
                    break
            per_kind[kind].append(1 if passage else 0)
            if not passage:
                # Reported, never averaged in as a zero: the grader was handed
                # nothing, which is retrieval's failure and not its own.
                unretrieved.append(f"  [{kind}] {item['question']}")
                print("MISS", f"[{kind}]", item["question"], "(unretrieved)")
                continue

            report = grade_grounding(item["answer"], [passage], floor=FLOOR)
            grounding_scores.append(report.score)
            valid = validate_citations(item["answer"], [passage]).is_valid
            validator_hits.append(1 if valid else 0)
            print(
                "PASS",
                f"[{kind}]",
                item["question"],
                f"(grounding={report.score:.0%} "
                f"scored={report.scored} markers={'ok' if valid else 'BAD'})",
            )

            for defect in item.get("defects", []):
                defects_total += 1
                if _caught(defect["answer"], passage, defect["expect"]):
                    defects_caught += 1
                else:
                    print(
                        f"     UNCAUGHT defect ({defect['expect']}): {defect['why']}"
                    )
            for spot in item.get("blind_spots", []):
                verdicts = _verdicts(spot["answer"], passage)
                blind.append((spot["why"], ", ".join(verdicts)))

        print()
        failures: List[str] = []
        overall: List[int] = []
        for kind in KINDS:
            hits = per_kind.get(kind, [])
            if not hits:
                continue
            overall.extend(hits)
            recall = sum(hits) / len(hits)
            floor = FLOORS.get(kind, 0.0)
            status = "ok " if recall >= floor else "LOW"
            print(
                f"{status} {kind:11} recall@{RETRIEVE}={recall:5.1%}  "
                f"n={len(hits):2}  floor={floor:.0%}"
            )
            if recall < floor:
                failures.append(f"{kind} recall {recall:.1%} is below its {floor:.0%} floor")

        grounding = sum(grounding_scores) / len(grounding_scores) if grounding_scores else 0.0
        validator = sum(validator_hits) / len(validator_hits) if validator_hits else 0.0
        detection = defects_caught / defects_total if defects_total else 1.0

        print(f"\noverall recall={sum(overall) / len(overall):5.1%} items={len(overall)}")
        print(
            f"GROUNDING       = {grounding:5.1%} "
            f"over {len(grounding_scores)} retrieved answers  floor={GROUNDING_FLOOR:.0%}"
        )
        print(
            f"VALIDATOR PASS  = {validator:5.1%} "
            f"({sum(validator_hits)}/{len(validator_hits)} answers cite only real passages)  "
            f"floor={VALIDATOR_FLOOR:.0%}"
        )
        print(
            f"DEFECT CATCH    = {detection:5.1%} "
            f"({defects_caught}/{defects_total} planted faults caught as their class)  "
            f"floor={DETECTION_FLOOR:.0%}"
        )

        if grounding < GROUNDING_FLOOR:
            failures.append(
                f"mean grounding {grounding:.1%} is below its {GROUNDING_FLOOR:.0%} floor"
            )
        if validator < VALIDATOR_FLOOR:
            failures.append(
                f"validator pass rate {validator:.1%} is below its {VALIDATOR_FLOOR:.0%} floor"
            )
        if detection < DETECTION_FLOOR:
            failures.append(
                f"defect catch rate {detection:.1%} is below its {DETECTION_FLOOR:.0%} floor"
            )

        if unretrieved:
            print("\nunretrieved (the grader was handed nothing; not averaged in):")
            for line in unretrieved:
                print(line)

        # Printed every run, gated by nothing. These are the faults the method
        # is documented not to catch; seeing them pass is what keeps "verified"
        # from being read as "true".
        print("\nknown blind spots (lexical overlap cannot see these):")
        for why, verdicts in blind:
            print(f"  {verdicts:10} — {why}")

        if failures:
            print()
            for line in failures:
                print("FAIL:", line)
            raise SystemExit(1)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
