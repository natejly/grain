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
    latest = db.scalar(
        select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id)
    )
    event = RunEvent(
        workspace_id=workspace_id,
        run_id=run_id,
        sequence=(latest or 0) + 1,
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

