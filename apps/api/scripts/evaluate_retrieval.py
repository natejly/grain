from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Chunk, Source, User, Workspace
from app.services.ingestion import make_chunks
from app.services.retrieval import search_evidence

WORKSPACE_ID = "00000000-0000-4000-8000-000000000091"
USER_ID = "00000000-0000-4000-8000-000000000092"


def main() -> None:
    corpus_path = Path(__file__).parents[1] / "evals" / "corpus.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    try:
        db.add(Workspace(id=WORKSPACE_ID, name="Evaluation"))
        db.add(User(id=USER_ID, email="eval@example.com", name="Evaluator"))
        for item in corpus:
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
                db.add(
                    Chunk(
                        workspace_id=WORKSPACE_ID,
                        source_id=source.id,
                        ordinal=ordinal,
                        content=content,
                        char_start=start,
                        char_end=end,
                        token_count=len(content.split()),
                    )
                )
        db.commit()

        total = 0
        hits = 0
        precise = 0
        for item in corpus:
            for question in item["questions"]:
                total += 1
                results = search_evidence(
                    db,
                    workspace_id=WORKSPACE_ID,
                    query=question,
                    limit=5,
                )
                filenames = [result.filename for result in results]
                if item["source"] in filenames:
                    hits += 1
                if results and results[0].filename == item["source"]:
                    precise += 1
                print(("PASS" if item["source"] in filenames else "MISS"), question)
        recall = hits / total if total else 0
        precision = precise / total if total else 0
        print(f"\nrecall@5={recall:.1%} citation_precision={precision:.1%} questions={total}")
        if recall < 0.8 or precision < 0.9:
            raise SystemExit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()

