"""Measure the three research surfaces that could silently rot.

Sibling of evaluate_memory.py and evaluate_retrieval.py, and built to the same
rule: seed through the PRODUCTION write paths, never a transcription of them —
a harness that reimplements the code it grades measures the transcription, the
mistake evaluate_retrieval.py was rebuilt to undo.

Three numbers, each attached to a claim this cluster makes out loud:

1. FOLLOW-UP PRECISION. The product's claim is that a chip is never offered
   unless the corpus has something above the generation's dense floor to say
   about it. The metric is the fraction of admitted chips whose probe landed on
   a passage from the document the answer was written from, plus the chips per
   answer (hard ceiling 3).

   KNOWN LIMIT: only the HEADING arm is exercised. The KG arm reads
   `GraphEntity` rows, which are written by `rebuild_graph`'s model-backed
   projection — unavailable under the scripted provider — so a run with no
   projection measures the heading arm and the probe, which is where the
   admission decision lives. Seeding GraphEntity rows by hand would measure a
   transcription instead of the projection, which is worse than measuring less.

2. DEBATE ACCURACY. `coverage.classify` over labelled questions: overall
   accuracy, and separately the false-positive rate on plain questions (a plain
   question forced through a counter-evidence pass spends two retrievals for
   nothing).

3. DRIFT DETECTION. Publish a page, mutate some cited chunks, re-validate.
   Recall on mutated citations must be 1.0 and FALSE drift must be exactly 0.0
   — a ceiling of exactly zero is defensible here because the comparison is a
   hash, not a heuristic.

Run it with APP_ENV pinned, as `make eval` does:

    APP_ENV=development PYTHONPATH=apps/api python apps/api/scripts/evaluate_research.py

It never reaches a live provider.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

# Retrieval reaches a model only for the dense arm's embeddings. `setdefault` so
# a real configuration in the environment still wins.
os.environ.setdefault("MODEL_PROVIDER", "scripted")
os.environ.setdefault(
    "SCRIPTED_MODEL_SCRIPT",
    str(Path(__file__).parents[1] / "tests" / "scripts" / "agent.json"),
)
# And the env that makes the line above legal: `scripted` is gated on APP_ENV
# being development or test, and `app_env` defaults to "production". The memory
# gate needed exactly this fix (commit 7f1022e) after failing in CI — where no
# repo-root .env supplies it — while reading like a runner problem.
os.environ.setdefault("APP_ENV", "development")
# THE FLOOR, STATED EXPLICITLY — and this is the one place in the cluster where
# that is the right move. `CALIBRATED_DENSE_FLOORS` is measured against a real
# embedding model; this harness embeds with a hash double (see `_fake_vector`),
# whose geometry is not that model's. Scaling or deriving a floor for it would
# be exactly the error the calibration table warns about, so the harness makes
# a deliberate deployment statement instead, through the documented override —
# which also exercises `effective_floor`'s `model_fields_set` contract.
#
# 0.10 was chosen by measurement, not by taste: on this corpus the double
# scores the right passage at 0.14-0.26 and every other passage at 0.00-0.06.
os.environ.setdefault("RETRIEVAL_DENSE_FLOOR", "0.10")

from app.config import Settings, get_settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import (  # noqa: E402
    Chunk,
    Conversation,
    Message,
    Source,
    User,
    Workspace,
)
from app.services import coverage as coverage_service  # noqa: E402
from app.services import embedding_generations as generations  # noqa: E402
from app.services import followups as followups_service  # noqa: E402
from app.services import pages as pages_service  # noqa: E402
from app.services import retrieval as retrieval_service  # noqa: E402
from app.services.embeddings import pack_vector, query_embedding_cache  # noqa: E402
from app.services.ingestion import make_chunks  # noqa: E402
from app.services.retrieval import Evidence, tokenize  # noqa: E402
from tests.embedding_doubles import as_batch  # noqa: E402

WORKSPACE_ID = "00000000-0000-4000-8000-000000000501"
USER_ID = "00000000-0000-4000-8000-000000000502"

#: Floors sit JUST UNDER the values measured on 2026-09-21 against this corpus,
#: so a regression fails here rather than an aspiration being reported as one.
#:
#: Measured 2026-09-21 (scripted provider, in-memory SQLite, create_all):
#:   followup_precision  1.00   -> floor 0.80
#:   debate_accuracy     1.00   -> floor 0.90
#:   debate_false_pos    0.00   -> ceiling 0.20
#:   drift_recall        1.00   -> floor 1.00 (a hash comparison; nothing else
#:                                 would be honest)
#:   false_drift         0.00   -> ceiling 0.00, same reason
FLOORS: Dict[str, float] = {
    "followup_precision": 0.80,
    "debate_accuracy": 0.90,
    "drift_recall": 1.00,
}
CEILINGS: Dict[str, float] = {
    "debate_false_positive": 0.20,
    "false_drift": 0.00,
}
#: The product's own cap, asserted rather than assumed.
MAX_CHIPS = followups_service.MAX_FOLLOWUPS


#: The width the double writes at. The real contract's width, so the generation
#: the harness builds is the shape production builds — only the vectors inside
#: it are a stand-in.
EMBED_DIM = 1536


def _fake_vector(text: str, dim: int = EMBED_DIM) -> bytes:
    """A deterministic stand-in for an embedding model (evaluate_memory's).

    Tokens land in a bucket by hash and the vector is L2-normalised, so texts
    sharing vocabulary are genuinely closer. The scripted provider embeds
    nothing at all, and a dense-arm measurement with no dense arm would report
    "no chip cleared the probe" for every answer and call it a pass.
    """
    values = [0.0] * dim
    for token in tokenize(text):
        bucket = int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % dim
        values[bucket] += 1.0
    norm = sum(value * value for value in values) ** 0.5
    if norm:
        values = [value / norm for value in values]
    return pack_vector(values)


def _install_embedder() -> None:
    """Point the retrieval module's embedding seam at the double.

    Both the corpus write and the query read go through this one name, so one
    patch covers `embed_chunks` and `dense_ranking._embed_query` — and nothing
    downstream of the seam is replaced, which is what keeps this a measurement
    of the product rather than of a copy of it.
    """
    retrieval_service.embed_batch = as_batch(
        lambda texts, settings=None, **contract: [_fake_vector(t) for t in texts]
    )


def _load() -> dict:
    path = Path(__file__).parents[1] / "evals" / "research_corpus.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _seed(db: Session, documents: Sequence[dict], settings: Settings) -> List[Chunk]:
    """One workspace holding the corpus, indexed and embedded the product's way."""
    db.add(Workspace(id=WORKSPACE_ID, name="Research eval"))
    db.add(User(id=USER_ID, email="researcheval@example.com", name="Evaluator"))
    records: List[Chunk] = []
    for item in documents:
        source = Source(
            workspace_id=WORKSPACE_ID,
            created_by=USER_ID,
            filename=item["filename"],
            media_type="text/markdown",
            object_key="/evaluation/" + item["filename"],
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
    retrieval_service.index_chunks(db, records)
    retrieval_service.embed_chunks(db, records, settings)
    db.commit()
    return records


def _chunks_of(db: Session, filename: str) -> List[Chunk]:
    return list(
        db.scalars(
            select(Chunk)
            .join(Source, Source.id == Chunk.source_id)
            .where(
                Chunk.workspace_id == WORKSPACE_ID, Source.filename == filename
            )
            .order_by(Chunk.ordinal)
        )
    )


def _evidence(db: Session, chunk: Chunk, filename: str) -> Evidence:
    return Evidence(
        chunk_id=chunk.id,
        source_id=chunk.source_id,
        filename=filename,
        ordinal=chunk.ordinal,
        excerpt=chunk.content[:400],
        score=1.0,
    )


def measure_followups(db: Session, corpus: dict, settings: Settings) -> Tuple[float, float]:
    """(precision, chips per answer). Every chip is probed; none is assumed."""
    admitted = 0
    on_gold = 0
    answers = corpus["answers"]
    for item in answers:
        cited = _chunks_of(db, item["cited_source"])
        gold: set[str] = set()
        for filename in item["gold_sources"]:
            gold.update(chunk.id for chunk in _chunks_of(db, filename))
        evidence = [_evidence(db, cited[0], item["cited_source"])] if cited else []
        query_embedding_cache.clear()
        chips = followups_service.suggest(
            db,
            workspace_id=WORKSPACE_ID,
            answer=item["text"],
            evidence=evidence,
            settings=settings,
        )
        if len(chips) > MAX_CHIPS:
            print(f"FAIL: {len(chips)} chips admitted, ceiling is {MAX_CHIPS}")
            raise SystemExit(1)
        for chip in chips:
            admitted += 1
            if gold & set(chip.chunk_ids):
                on_gold += 1
            print(
                f"  chip [{chip.origin}] {chip.text}  "
                f"score={chip.probe_score:.3f} "
                f"{'on-gold' if gold & set(chip.chunk_ids) else 'OFF-GOLD'}"
            )
    precision = on_gold / admitted if admitted else 0.0
    per_answer = admitted / len(answers) if answers else 0.0
    return precision, per_answer


def measure_debate(corpus: dict) -> Tuple[float, float]:
    """(accuracy, false-positive rate on plain questions). Pure; no database."""
    correct = 0
    plain = 0
    plain_wrong = 0
    for item in corpus["questions"]:
        verdict = coverage_service.classify(item["text"])
        if verdict == item["shape"]:
            correct += 1
        else:
            print(f"  MISCLASSIFIED as {verdict}: {item['text']}")
        if item["shape"] == "plain":
            plain += 1
            if verdict == "debate":
                plain_wrong += 1
    questions = corpus["questions"]
    accuracy = correct / len(questions) if questions else 0.0
    false_positive = plain_wrong / plain if plain else 0.0
    return accuracy, false_positive


def measure_drift(db: Session, corpus: dict) -> Tuple[float, float]:
    """(recall on mutated citations, false-drift rate on untouched ones)."""
    conversation = Conversation(
        workspace_id=WORKSPACE_ID, created_by=USER_ID, title="Drift fixture"
    )
    db.add(conversation)
    db.flush()
    cited: List[Chunk] = []
    for item in corpus["answers"]:
        chunks = _chunks_of(db, item["cited_source"])
        if chunks:
            cited.append(chunks[0])
    citations = [
        {
            "chunk_id": chunk.id,
            "source_id": chunk.source_id,
            "filename": "",
            "ordinal": chunk.ordinal,
            "excerpt": chunk.content[:400],
            "score": 1.0,
        }
        for chunk in cited
    ]
    markers = "".join(f"[{index}]" for index in range(1, len(citations) + 1))
    db.add(
        Message(
            workspace_id=WORKSPACE_ID,
            conversation_id=conversation.id,
            run_id="",
            role="assistant",
            content=f"The corpus says so {markers}.",
            citations_json=json.dumps(citations),
        )
    )
    db.commit()
    page = pages_service.publish(
        db,
        workspace_id=WORKSPACE_ID,
        user_id=USER_ID,
        conversation_id=conversation.id,
        title="Drift fixture",
    )
    db.commit()

    # Mutate the first half of the cited chunks, leave the rest untouched.
    mutated = {chunk.id for chunk in cited[: max(1, len(cited) // 2)]}
    for chunk in cited:
        if chunk.id in mutated:
            chunk.content = chunk.content + "\n\nAmended after publication."
    db.commit()
    pages_service.revalidate(db, page=page)
    db.commit()

    verdicts = {
        row.chunk_id: row.status
        for row in db.scalars(
            select(pages_service.PageCitation).where(
                pages_service.PageCitation.page_id == page.id
            )
        )
    }
    detected = sum(
        1 for chunk_id in mutated if verdicts.get(chunk_id) == "changed"
    )
    untouched = [chunk.id for chunk in cited if chunk.id not in mutated]
    false_drift = sum(
        1 for chunk_id in untouched if verdicts.get(chunk_id) != "frozen"
    )
    recall = detected / len(mutated) if mutated else 0.0
    rate = false_drift / len(untouched) if untouched else 0.0
    return recall, rate


def main() -> None:
    corpus = _load()
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    failures: List[str] = []
    try:
        settings = get_settings()
        _install_embedder()
        chunks = _seed(db, corpus["documents"], settings)
        generation = generations.active_generation(db)
        if generation is None:
            print("nothing was embedded; the dense probe cannot be measured")
            raise SystemExit(2)
        floor = generations.effective_floor(generation, settings)
        print(
            f"generation: {generation.dimensions}d {generation.storage_dtype}, "
            f"floor {floor:.4f} (READ — this harness's explicit override, "
            "never a value derived from the width)"
        )
        print(f"corpus: {len(corpus['documents'])} documents, {len(chunks)} chunks\n")

        print("follow-up chips")
        precision, per_answer = measure_followups(db, corpus, settings)
        status = "ok " if precision >= FLOORS["followup_precision"] else "LOW"
        print(
            f"{status} followup_precision={precision:5.1%} "
            f"floor={FLOORS['followup_precision']:.0%}  "
            f"chips_per_answer={per_answer:.2f} ceiling={MAX_CHIPS}"
        )
        if precision < FLOORS["followup_precision"]:
            failures.append(
                f"follow-up precision {precision:.1%} is below its "
                f"{FLOORS['followup_precision']:.0%} floor"
            )

        print("\ndebate classifier")
        accuracy, false_positive = measure_debate(corpus)
        status = "ok " if accuracy >= FLOORS["debate_accuracy"] else "LOW"
        print(
            f"{status} debate_accuracy={accuracy:5.1%} "
            f"floor={FLOORS['debate_accuracy']:.0%}"
        )
        status = (
            "ok " if false_positive <= CEILINGS["debate_false_positive"] else "HIGH"
        )
        print(
            f"{status} debate_false_positive={false_positive:5.1%} "
            f"ceiling={CEILINGS['debate_false_positive']:.0%}"
        )
        if accuracy < FLOORS["debate_accuracy"]:
            failures.append(
                f"debate accuracy {accuracy:.1%} is below its "
                f"{FLOORS['debate_accuracy']:.0%} floor"
            )
        if false_positive > CEILINGS["debate_false_positive"]:
            failures.append(
                f"debate false-positive rate {false_positive:.1%} exceeds its "
                f"{CEILINGS['debate_false_positive']:.0%} ceiling"
            )

        print("\npage drift")
        recall, false_drift = measure_drift(db, corpus)
        status = "ok " if recall >= FLOORS["drift_recall"] else "LOW"
        print(f"{status} drift_recall={recall:5.1%} floor={FLOORS['drift_recall']:.0%}")
        status = "ok " if false_drift <= CEILINGS["false_drift"] else "HIGH"
        print(
            f"{status} false_drift={false_drift:5.1%} "
            f"ceiling={CEILINGS['false_drift']:.0%}"
        )
        if recall < FLOORS["drift_recall"]:
            failures.append(
                f"drift recall {recall:.1%} is below its "
                f"{FLOORS['drift_recall']:.0%} floor"
            )
        if false_drift > CEILINGS["false_drift"]:
            failures.append(
                f"false-drift rate {false_drift:.1%} exceeds its "
                f"{CEILINGS['false_drift']:.0%} ceiling"
            )

        if failures:
            print()
            for line in failures:
                print("FAIL:", line)
            raise SystemExit(1)
        print("\nall research gates passed")
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
