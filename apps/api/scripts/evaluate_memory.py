"""Measure memory recall quality, and how often it serves a fact that is no longer true.

Sibling of evaluate_retrieval.py, and built for the same reason: nothing in this
repo measured `recall()`, so no change to memory scoring could be shown to help or
hurt. The retrieval harness was rebuilt after it turned out to score 100% on a
corpus it could not fail; this one starts from that lesson.

The headline metric is not recall. It is STALE-SERVED RATE.

`_upsert_item` keys a memory on a hash of its content, so "deploys on Fly.io" and
"moved from Fly.io to Railway" are two unrelated rows. Both stay active, both match
a deployment question, and both are injected — leaving the model to arbitrate
between a fact and its own correction from an unlabelled list. Worse, `importance`
accrues to whichever phrasing recurs rather than to whichever claim is current, so
a stale fact repeated often outranks the correction that replaced it.

The published failure rate for naive retrieval on superseded values is 15-40%, and
cosine similarity cannot separate a contradiction from a duplicate (AUROC 0.59 —
chance), so no embedding or scoring change fixes this. Only representing
supersession does. This script exists to put our own number on it before and after.

Run it both ways — the switch is one environment variable:

    PYTHONPATH=apps/api .venv/bin/python apps/api/scripts/evaluate_memory.py
    MEMORY_SUPERSESSION=0 PYTHONPATH=apps/api .venv/bin/python apps/api/scripts/evaluate_memory.py

Seeding goes through `apply_extracted_memories` — the write path itself, not a
copy of it — with each corpus fact standing in for one extraction. `keys` carries
the `subject|relation` claim key the extractor is asked to return for that fact,
which is the one thing a harness with no model has to supply: the corpus asserts
that two phrasings of one claim share a key and that unrelated claims do not, and
the code under test decides everything that follows from that.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

os.environ.setdefault("MODEL_PROVIDER", "scripted")
os.environ.setdefault(
    "SCRIPTED_MODEL_SCRIPT",
    str(Path(__file__).parents[1] / "tests" / "scripts" / "agent.json"),
)

from app.config import Settings, get_settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.models import Conversation, User, Workspace  # noqa: E402
from app.services import memory as memory_service  # noqa: E402
from app.services.embeddings import pack_vector  # noqa: E402
from app.services.memory import recall  # noqa: E402
from app.services.retrieval import tokenize  # noqa: E402
from tests.embedding_doubles import as_batch  # noqa: E402

USER_ID = "00000000-0000-4000-8000-000000000093"
EMBED_DIM = 64

# Floors are per category and sit just under the measured baseline, so this fails
# on a regression rather than describing an aspiration. STALE_CEILING was 1.01 —
# no ceiling at all — while the stale rate stood at 100%; supersession drove it to
# 0/5 with every category's recall unmoved, so it is now held there. A run with
# MEMORY_SUPERSESSION=0 is expected to fail this ceiling: that failure is the
# ablation's result, not a broken harness.
FLOORS: Dict[str, float] = {
    "preference": 0.60,
    "single_session": 0.60,
    "multi_session": 0.50,
    "temporal": 0.50,
    "knowledge_update": 0.50,
}
STALE_CEILING = 0.0


def _fake_vector(text: str, dim: int = EMBED_DIM) -> bytes:
    """A deterministic stand-in for an embedding model.

    Tokens land in a bucket by hash and the vector is L2-normalised, so texts
    sharing vocabulary are genuinely closer. This keeps the harness runnable with
    no API key while still exercising the vector path — measuring lexical-only
    recall would hide exactly the behaviour the dense scorer contributes.
    """
    values = [0.0] * dim
    for token in tokenize(text):
        bucket = int(hashlib.sha256(token.encode()).hexdigest()[:8], 16) % dim
        values[bucket] += 1.0
    norm = sum(value * value for value in values) ** 0.5
    if norm:
        values = [value / norm for value in values]
    return pack_vector(values)


def _seed_workspace(
    db: Session,
    index: int,
    facts: Sequence[str],
    keys: Sequence[str],
    settings: Settings,
) -> tuple[str, str]:
    """One isolated workspace per item, so items cannot contaminate each other.

    Each fact is written as its own extraction, in corpus order, so a correction
    arrives at a workspace that already holds the fact it corrects — which is the
    only arrangement in which supersession has anything to do.
    """
    workspace_id = f"00000000-0000-4000-8000-0000000{index:05d}"
    db.add(Workspace(id=workspace_id, name=f"Memory eval {index}"))
    conversation = Conversation(workspace_id=workspace_id, created_by=USER_ID)
    db.add(conversation)
    db.flush()
    for position, fact in enumerate(facts):
        extracted: List[Dict[str, object]] = [{"kind": "fact", "content": fact}]
        if position < len(keys):
            extracted[0]["normalized_key"] = keys[position]
        written = memory_service.apply_extracted_memories(
            db,
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            run_id="",
            extracted=extracted,
            message_ids=[],
            settings=settings,
        )
        db.flush()
        for item in written:
            # Stand in for _embed_pending, which would call a provider. Note that
            # a retired row keeps the vector it already had: nothing about this
            # measurement should depend on superseded rows being unreachable
            # because they are unembedded rather than because they are inactive.
            if item.embedding is None:
                item.embedding = _fake_vector(item.content)
    db.commit()
    return workspace_id, conversation.id


def main() -> None:
    path = Path(__file__).parents[1] / "evals" / "memory_corpus.json"
    items = json.loads(path.read_text(encoding="utf-8"))["items"]

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    try:
        settings = get_settings()
        db.add(User(id=USER_ID, email="memeval@example.com", name="Evaluator"))
        db.commit()

        per_category: Dict[str, List[int]] = defaultdict(list)
        stale_hits = 0
        stale_total = 0
        misses: List[str] = []

        print(
            "supersession:",
            "on" if settings.memory_supersession else "off (MEMORY_SUPERSESSION=0)",
        )
        print()

        for index, item in enumerate(items):
            workspace_id, conversation_id = _seed_workspace(
                db, index, item["seed"], item.get("keys", []), settings
            )

            # The query embedding has to come from the same fake embedder, or the
            # dense half of recall scores noise.
            query_blob = _fake_vector(item["question"])
            original = memory_service.embed_batch
            memory_service.embed_batch = as_batch(
                lambda texts, s=None, blob=query_blob: [blob]
            )
            try:
                found = recall(
                    db,
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    query=item["question"],
                    settings=settings,
                )
            finally:
                memory_service.embed_batch = original

            contents = [entry.content.lower() for entry in found.items]
            blob = "\n".join(contents)
            hit = item["expect"].lower() in blob.lower()
            per_category[item["category"]].append(1 if hit else 0)
            if not hit:
                misses.append(f"  [{item['category']}] {item['question']}")

            note = ""
            stale: Optional[str] = item.get("stale")
            if stale is not None:
                stale_total += 1
                # The superseded fact must NOT come back. If it does, the model is
                # handed a claim and its correction with nothing to tell them apart.
                #
                # Judged per memory, not over the joined blob, and only against
                # memories that do not also carry the current value. A correction
                # is allowed to name what it replaced — "moved the API from
                # Fly.io to Railway" mentions Fly.io and is the *answer*, not a
                # stale serve. Matching the blob counted that sentence against
                # itself and made one item unfixable by any amount of correctness.
                served = any(
                    stale.lower() in content and item["expect"].lower() not in content
                    for content in contents
                )
                if served:
                    stale_hits += 1
                    note = "  <- ALSO SERVED THE SUPERSEDED FACT"

            print(("PASS" if hit else "MISS"), f"[{item['category']}]", item["question"], note)

        print()
        failures: List[str] = []
        overall: List[int] = []
        for category in (
            "preference",
            "single_session",
            "multi_session",
            "temporal",
            "knowledge_update",
        ):
            hits = per_category.get(category, [])
            if not hits:
                continue
            overall.extend(hits)
            score = sum(hits) / len(hits)
            floor = FLOORS.get(category, 0.0)
            status = "ok " if score >= floor else "LOW"
            print(f"{status} {category:17} recall={score:5.1%}  n={len(hits):2}  floor={floor:.0%}")
            if score < floor:
                failures.append(f"{category} recall {score:.1%} is below its {floor:.0%} floor")

        rate = stale_hits / stale_total if stale_total else 0.0
        print(f"\noverall recall={sum(overall) / len(overall):.1%} items={len(overall)}")
        print(
            f"STALE-SERVED RATE = {rate:.1%} "
            f"({stale_hits}/{stale_total} superseded facts were returned alongside the truth)"
        )
        if rate > STALE_CEILING:
            failures.append(f"stale-served rate {rate:.1%} exceeds its {STALE_CEILING:.0%} ceiling")

        if misses:
            print("\nmissed:")
            for line in misses:
                print(line)

        if failures:
            print()
            for line in failures:
                print("FAIL:", line)
            raise SystemExit(1)
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
