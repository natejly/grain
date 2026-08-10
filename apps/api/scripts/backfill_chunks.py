"""Bring chunks written before hybrid retrieval up to the current index.

Ingest builds all three of a chunk's retrieval artefacts — term postings, a
vector, and optionally a situating blurb — but a workspace that was ingested
before any of that existed has none of them. Two of the three cannot be repaired
on the read path at any price: an embedding and a blurb are network calls, and a
user waiting for an answer is the wrong place to make them. (The term index *is*
repaired on read, by `reconcile_index`, because it is pure local tokenization.)

    PYTHONPATH=apps/api python apps/api/scripts/backfill_chunks.py            # every workspace
    PYTHONPATH=apps/api python apps/api/scripts/backfill_chunks.py --workspace <id>
    RETRIEVAL_CONTEXTUAL=1 PYTHONPATH=apps/api python apps/api/scripts/backfill_chunks.py

Idempotent: a second run re-embeds nothing, because it only touches chunks whose
artefacts are missing or stale against the configured embedding model.
"""
from __future__ import annotations

import argparse
import sys
from typing import List

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Workspace
from app.services.ingestion import backfill_workspace


def _workspace_ids() -> List[str]:
    db = SessionLocal()
    try:
        return [str(row) for row in db.scalars(select(Workspace.id).order_by(Workspace.id))]
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="one workspace id; default is all of them")
    args = parser.parse_args()

    settings = get_settings()
    if settings.active_model_provider != "openai":
        print(
            "MODEL_PROVIDER is not openai: postings will be rebuilt, but there is "
            "no provider to embed with, so the dense arm stays empty."
        )
    print(
        f"embedding model={settings.openai_embedding_model} "
        f"contextual={'on' if settings.retrieval_contextual else 'off'}"
    )

    targets = [args.workspace] if args.workspace else _workspace_ids()
    totals = {"contextualized": 0, "indexed": 0, "embedded": 0}
    for workspace_id in targets:
        counts = backfill_workspace(workspace_id, settings)
        for key, value in counts.items():
            totals[key] += value
        print(
            f"{workspace_id}  indexed={counts['indexed']:<6} "
            f"embedded={counts['embedded']:<6} contextualized={counts['contextualized']}"
        )
    print(
        f"\n{len(targets)} workspace(s): indexed={totals['indexed']} "
        f"embedded={totals['embedded']} contextualized={totals['contextualized']}"
    )


if __name__ == "__main__":
    sys.exit(main())
