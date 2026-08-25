from __future__ import annotations

import json
import time
from typing import Any, Dict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import RunEvent

# Token deltas are batched into events rather than written one row per token:
# append_event costs a max(sequence) query plus a commit, and the SSE reader
# polls on a 250ms tick, so finer granularity buys nothing a viewer can see.
DELTA_FLUSH_CHARS = 48
DELTA_FLUSH_SECONDS = 0.1


def append_event(
    db: Session,
    *,
    workspace_id: str,
    run_id: str,
    event_type: str,
    payload: Dict[str, Any],
) -> RunEvent:
    """Append one event, with its sequence computed *inside* the INSERT.

    `run_events` is unique on (run_id, sequence), and two writers race it in
    production: the loop's own DeltaBuffer/tool events, and request threads —
    cancel, and now steer, which made the collision a routine user action. A
    read-max-then-insert here would hand both writers the same number and fail
    whichever inserts second, and the loop side has no retry: an IntegrityError
    there fails the whole turn. The scalar subquery makes the assignment atomic
    on SQLite (one writer at a time under WAL; the subquery evaluates inside
    the insert's own write transaction), which is the shipped backend.

    On backends with snapshot-isolated concurrent writers (Postgres) two
    simultaneous inserts can still compute the same max — callers that write
    from a request thread keep their one-shot IntegrityError retry as the
    belt to this suspenders.
    """
    event = RunEvent(
        workspace_id=workspace_id,
        run_id=run_id,
        sequence=(
            select(func.coalesce(func.max(RunEvent.sequence), 0) + 1)
            .where(RunEvent.run_id == run_id)
            .scalar_subquery()
        ),
        event_type=event_type,
        payload_json=json.dumps(payload, separators=(",", ":"), default=str),
    )
    db.add(event)
    db.flush()
    return event


class DeltaBuffer:
    """Accumulates streamed model text and flushes it as `message.delta` events.

    Holds text back until it is worth a row — DELTA_FLUSH_CHARS of text or
    DELTA_FLUSH_SECONDS since the last flush — and remembers everything it has
    seen so the caller can use it as the final message body.
    """

    def __init__(self, db: Session, *, workspace_id: str, run_id: str) -> None:
        self._db = db
        self._workspace_id = workspace_id
        self._run_id = run_id
        self._pending = ""
        self._last_flush = time.monotonic()
        self.text = ""

    def add(self, delta: str) -> None:
        if not delta:
            return
        self._pending += delta
        self.text += delta
        due = (
            len(self._pending) >= DELTA_FLUSH_CHARS
            or time.monotonic() - self._last_flush >= DELTA_FLUSH_SECONDS
        )
        if due:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        append_event(
            self._db,
            workspace_id=self._workspace_id,
            run_id=self._run_id,
            event_type="message.delta",
            payload={"delta": self._pending},
        )
        self._db.commit()
        self._pending = ""
        self._last_flush = time.monotonic()

