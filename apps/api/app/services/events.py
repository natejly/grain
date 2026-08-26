from __future__ import annotations

import json
import time
from typing import Any, Dict

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import RunEvent

# Token deltas are batched into events rather than written one row per token:
# append_event costs a max(sequence) query plus a commit, and the SSE reader
# polls on a 250ms tick, so finer granularity buys nothing a viewer can see.
DELTA_FLUSH_CHARS = 48
DELTA_FLUSH_SECONDS = 0.1


#: How many sequence collisions one append will absorb before giving up. Two
#: writers per run is the designed maximum (the worker's stream and one steer
#: route), so a second attempt nearly always lands; the margin is for a burst.
_APPEND_ATTEMPTS = 5


def _next_sequence(db: Session, run_id: str) -> int:
    latest = db.scalar(
        select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)
    )
    return (latest or 0) + 1


def append_event(
    db: Session,
    *,
    workspace_id: str,
    run_id: str,
    event_type: str,
    payload: Dict[str, Any],
) -> RunEvent:
    """Append the run's next event, surviving a concurrent appender.

    `sequence` is allocated read-then-insert, and steering made two writers
    per run the designed common case: the worker flushing deltas and the steer
    route recording a note race on the same `UNIQUE(run_id, sequence)`. Each
    attempt runs in a SAVEPOINT so a lost race rolls back only the one insert
    — never the caller's transaction — and retries against the fresh maximum.
    Without this, whichever side lost the race raised IntegrityError: in the
    worker that failed the very run being steered; in the route it was a 500.
    """
    body = json.dumps(payload, separators=(",", ":"), default=str)
    for attempt in range(_APPEND_ATTEMPTS):
        event = RunEvent(
            workspace_id=workspace_id,
            run_id=run_id,
            sequence=_next_sequence(db, run_id),
            event_type=event_type,
            payload_json=body,
        )
        try:
            with db.begin_nested():
                db.add(event)
            return event
        except IntegrityError:
            if attempt == _APPEND_ATTEMPTS - 1:
                raise
            # A concurrent appender took this sequence between the read and
            # the insert; the savepoint rolled our row back — go again. The
            # rollback usually expunges the pending row itself; the guard is
            # for dialects that leave it in the session's new set.
            if event in db:
                db.expunge(event)
    raise AssertionError("unreachable")


class DeltaBuffer:
    """Accumulates streamed model text and flushes it as `message.delta` events.

    Holds text back until it is worth a row — DELTA_FLUSH_CHARS of text or
    DELTA_FLUSH_SECONDS since the last flush — and remembers everything it has
    seen so the caller can use it as the final message body.
    """

    def __init__(
        self,
        db: Session,
        *,
        workspace_id: str,
        run_id: str,
        event_type: str = "message.delta",
    ) -> None:
        self._db = db
        self._workspace_id = workspace_id
        self._run_id = run_id
        #: The answer streams as `message.delta`; a thinking trail streams the
        #: same way under `thinking.delta` — same buffering, different lane.
        self._event_type = event_type
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
            event_type=self._event_type,
            payload={"delta": self._pending},
        )
        self._db.commit()
        self._pending = ""
        self._last_flush = time.monotonic()

