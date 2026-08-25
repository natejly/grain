"""Index conversations that predate the conversation index.

Every message ever written is still in `messages`; this walks each workspace's
conversations through `index_conversation`, which chunks the uncovered tail of
each transcript, refreshes the per-thread summary, and embeds what it wrote.
The search-time reconcile does the same thing lazily, five conversations per
search — this is the bulk path, for bringing a whole deployment's history
online at once instead of as searches happen to touch it.

    # every workspace:
    PYTHONPATH=apps/api python apps/api/scripts/backfill_conversation_index.py
    # one workspace:
    PYTHONPATH=apps/api python apps/api/scripts/backfill_conversation_index.py --workspace <id>

Idempotent: chunk coverage is by message id, so a second run writes nothing and
embeds nothing.
"""
from __future__ import annotations

import argparse
import sys
from typing import List

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Conversation, Workspace
from app.services.conversation_index import index_conversation


def _workspace_ids() -> List[str]:
    db = SessionLocal()
    try:
        return [str(row) for row in db.scalars(select(Workspace.id).order_by(Workspace.id))]
    finally:
        db.close()


def _backfill_workspace(workspace_id: str) -> int:
    settings = get_settings()
    written = 0
    db = SessionLocal()
    try:
        conversations = list(
            db.scalars(
                select(Conversation)
                .where(Conversation.workspace_id == workspace_id)
                .order_by(Conversation.created_at)
            )
        )
        for conversation in conversations:
            written += index_conversation(db, conversation, settings)
            # Per conversation, so one bad thread costs itself, not the run.
            db.commit()
    finally:
        db.close()
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="one workspace id; default is all")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.conversation_index_enabled:
        print("CONVERSATION_INDEX_ENABLED is off; nothing to do.")
        sys.exit(0)

    workspace_ids = [args.workspace] if args.workspace else _workspace_ids()
    for workspace_id in workspace_ids:
        written = _backfill_workspace(workspace_id)
        print(f"{workspace_id}: {written} chunks written")


if __name__ == "__main__":
    main()
