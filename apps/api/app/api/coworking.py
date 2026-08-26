"""Live coworking: one stream, presence heartbeats, and card claims.

The shell holds a single SSE connection to `/api/coworking/stream` and gets
three kinds of frame out of it:

- durable `workspace_events` (claims, ticks) after a cursor, exactly as a run
  stream replays `run_events`;
- `runs` snapshot frames — the workspace's runs in flight, re-sent only when
  the set changes. A snapshot rather than mirrored lifecycle events on
  purpose: runs park and resume from half a dozen sites, and a diffed
  snapshot cannot miss one of them;
- `presence` snapshot frames — cursors, selections, typing, live drafts —
  re-sent only when a heartbeat lands.

Everything is filtered per viewer with the same visibility rules the run and
conversation surfaces use: another member's personal thread does not leak
here as an intent line or a typing chip.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import SessionLocal, get_db
from ..models import BoardCard, Run, WorkspaceEvent
from ..schemas import (
    CoworkingActivityOut,
    PresenceHeartbeat,
    PresenceOut,
    TodoItemOut,
)
from ..services import conversations, coworking
from ..services.artifacts import todos
from ..services.audit import record_audit

router = APIRouter(prefix="/api/coworking", tags=["coworking"])

#: A live draft in a presence heartbeat is a courtesy view, not storage; past
#: this size a follower sees a truncated tail-of-draft rather than a 422.
MAX_DRAFT_CHARS = 64_000

#: Run states the activity surfaces call "in flight".
ACTIVE_RUN_STATES = ("queued", "running", "waiting_for_approval")


def _visible_runs(db: Session, *, workspace_id: str, user_id: str) -> List[Dict[str, Any]]:
    rows = list(
        db.scalars(
            select(Run)
            .where(
                Run.workspace_id == workspace_id,
                Run.status.in_(ACTIVE_RUN_STATES),
            )
            .order_by(Run.created_at.asc())
        )
    )
    out: List[Dict[str, Any]] = []
    for run in rows:
        if not conversations.run_activity_visible(
            db, actor_workspace_id=workspace_id, actor_user_id=user_id, run=run
        ):
            continue
        agent_id, label = coworking.agent_actor(db, run)
        out.append(
            {
                "run_id": run.id,
                "conversation_id": run.conversation_id,
                "status": run.status,
                "agent_id": agent_id,
                "agent_label": label,
                "intent": coworking.excerpt(run.prompt),
                "created_by": run.created_by,
            }
        )
    return out


def _presence_surface_visible(
    db: Session, *, workspace_id: str, user_id: str, surface: str
) -> bool:
    """A presence on a personal thread must not leak as a typing chip. Every
    other surface — documents, boards, dashboards — is workspace-shared."""
    kind, _, subject_id = surface.partition(":")
    if kind != "conversation" or not subject_id:
        return True
    return (
        conversations.resolve_visible(
            db, workspace_id=workspace_id, user_id=user_id, conversation_id=subject_id
        )
        is not None
    )


def _visible_presences(
    db: Session, *, workspace_id: str, user_id: str
) -> List[Dict[str, Any]]:
    return [
        coworking.presence_snapshot(row)
        for row in coworking.active_presences(db, workspace_id=workspace_id)
        if _presence_surface_visible(
            db, workspace_id=workspace_id, user_id=user_id, surface=row.surface
        )
    ]


def _last_sequence(db: Session, workspace_id: str) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.max(WorkspaceEvent.sequence), 0)).where(
                WorkspaceEvent.workspace_id == workspace_id
            )
        )
        or 0
    )


@router.get("/activity", response_model=CoworkingActivityOut)
def activity(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> CoworkingActivityOut:
    """The snapshot a shell paints before subscribing: runs in flight, who is
    where, and the stream cursor to resume from so nothing lands between."""
    return CoworkingActivityOut.model_validate(
        {
            "runs": _visible_runs(
                db, workspace_id=actor.workspace_id, user_id=actor.user_id
            ),
            "presences": _visible_presences(
                db, workspace_id=actor.workspace_id, user_id=actor.user_id
            ),
            "last_event_sequence": _last_sequence(db, actor.workspace_id),
        }
    )


@router.post("/presence", response_model=PresenceOut)
def heartbeat(
    payload: PresenceHeartbeat,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> PresenceOut:
    state = dict(payload.state)
    draft = state.get("draft")
    if isinstance(draft, str) and len(draft) > MAX_DRAFT_CHARS:
        state["draft"] = draft[:MAX_DRAFT_CHARS]
        state["draft_truncated"] = True
    row = coworking.heartbeat_presence(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        actor_kind="user",
        actor_label=actor.user_name,
        surface=payload.surface,
        state=state,
    )
    db.commit()
    return PresenceOut.model_validate(coworking.presence_snapshot(row))


@router.delete("/presence", status_code=204)
def leave(
    surface: str = Query(default=""),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    """The explicit goodbye a closing tab sends, so chips clear in one tick
    instead of a TTL. Best-effort: a tab that dies silently expires anyway."""
    coworking.drop_presence(
        db, workspace_id=actor.workspace_id, actor_id=actor.user_id, surface=surface
    )
    db.commit()


def _emit_with_retry(
    db: Session, *, workspace_id: str, event_type: str, payload: Dict[str, Any]
) -> None:
    """Request-thread append: one-shot retry on the sequence race, the same
    belt `steer_run` wears over `append_event`, for the same reason."""
    for attempt in (1, 2):
        try:
            coworking.append_workspace_event(
                db, workspace_id=workspace_id, event_type=event_type, payload=payload
            )
            db.commit()
            return
        except IntegrityError:
            db.rollback()
            if attempt == 2:
                raise HTTPException(
                    status_code=409, detail="The workspace is busy; try again"
                ) from None


def _load_card(db: Session, actor: Actor, item_id: str) -> BoardCard:
    card = db.scalar(
        select(BoardCard).where(
            BoardCard.id == item_id, BoardCard.workspace_id == actor.workspace_id
        )
    )
    if card is None:
        raise HTTPException(status_code=404, detail="No item with that id")
    return card


@router.post("/items/{item_id}/claim", response_model=TodoItemOut)
def claim_item(
    item_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> TodoItemOut:
    _load_card(db, actor, item_id)
    try:
        card = coworking.claim_card(
            db,
            workspace_id=actor.workspace_id,
            card_id=item_id,
            actor_id=actor.user_id,
            actor_kind="user",
            actor_label=actor.user_name,
        )
    except coworking.ClaimConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="card.claimed",
        resource_type="board_card",
        resource_id=card.id,
        detail={"title": card.title},
    )
    _emit_with_retry(
        db,
        workspace_id=actor.workspace_id,
        event_type="card.claimed",
        payload={
            "item_id": card.id,
            "list_id": card.board_id,
            "title": card.title,
            "actor_id": actor.user_id,
            "actor_kind": "user",
            "actor_label": actor.user_name,
        },
    )
    return TodoItemOut.model_validate(todos.item_snapshot(card))


@router.post("/items/{item_id}/release", response_model=TodoItemOut)
def release_item(
    item_id: str,
    force: bool = Query(default=False),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> TodoItemOut:
    """Release a claim. `force` lets a user take any claim off a card — humans
    outrank agents on their own board; agents have no such override."""
    card = _load_card(db, actor, item_id)
    try:
        coworking.release_card(
            db,
            workspace_id=actor.workspace_id,
            card=card,
            actor_id=actor.user_id,
            force=force,
        )
    except coworking.ClaimConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="card.released",
        resource_type="board_card",
        resource_id=card.id,
        detail={"title": card.title, "force": force},
    )
    _emit_with_retry(
        db,
        workspace_id=actor.workspace_id,
        event_type="card.released",
        payload={
            "item_id": card.id,
            "list_id": card.board_id,
            "title": card.title,
            "actor_id": actor.user_id,
            "actor_kind": "user",
            "actor_label": actor.user_name,
        },
    )
    return TodoItemOut.model_validate(todos.item_snapshot(card))


def _frame(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _coworking_stream(
    *, workspace_id: str, user_id: str, after: int, once: bool = False
) -> AsyncIterator[str]:
    cursor = after
    last_runs = ""
    last_presence = ""
    idle_ticks = 0
    while True:
        emitted = False
        db = SessionLocal()
        try:
            for event in coworking.events_after(
                db, workspace_id=workspace_id, after=cursor
            ):
                cursor = event.sequence
                emitted = True
                yield (
                    f"id: {event.sequence}\nevent: {event.event_type}\ndata: "
                    + event.payload_json
                    + "\n\n"
                )
            runs = _visible_runs(db, workspace_id=workspace_id, user_id=user_id)
            serialized = json.dumps(runs, default=str)
            if serialized != last_runs:
                last_runs = serialized
                emitted = True
                yield _frame("runs", runs)
            presences = _visible_presences(
                db, workspace_id=workspace_id, user_id=user_id
            )
            serialized = json.dumps(presences, default=str)
            if serialized != last_presence:
                last_presence = serialized
                emitted = True
                yield _frame("presence", presences)
        finally:
            db.close()
        if once:
            # One pass — the backlog plus both snapshots, then done. The poll
            # fallback for a client that cannot hold a stream, and what lets a
            # test read this endpoint without holding one.
            return
        idle_ticks = 0 if emitted else idle_ticks + 1
        if idle_ticks >= 40:
            yield ": heartbeat\n\n"
            idle_ticks = 0
        await asyncio.sleep(0.25)


@router.get("/stream")
def stream(
    after: int = Query(default=0, ge=0),
    once: bool = Query(default=False),
    actor: Actor = Depends(get_actor),
) -> StreamingResponse:
    """The workspace's one live connection. Never terminates on its own — the
    shell owns its lifetime, unlike a run stream, which ends with its run.
    `once` bounds it to a single pass instead: the poll-shaped reading."""
    return StreamingResponse(
        _coworking_stream(
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            after=after,
            once=once,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
