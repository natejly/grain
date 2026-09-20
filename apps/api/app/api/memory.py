from __future__ import annotations

import json
import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..clock import utcnow
from ..config import get_settings
from ..database import get_db
from ..models import MemoryItem
from ..schemas import MemoryCreate, MemoryItemOut, MemoryUpdate
from ..services import spaces as spaces_service
from ..services.audit import record_audit
from ..services.coworking import append_workspace_event
from ..services.memory import (
    ALL_SPACES,
    SHARED_OWNER,
    _active,
    _content_key,
    edit_memory,
    normalize_memory_content,
    remember_memory,
    tombstone_key,
)
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["memory"])


def _signal_memory_updated(
    db: Session, *, workspace_id: str, item_id: str, owner_id: str
) -> None:
    """The same `memory.updated` ping the run path emits, after a manual write.

    Without it, member A hand-adding or editing a shared memory leaves member
    B's open Memory view (and A's own other tabs) stale until a reload — the
    exact staleness the event exists to prevent. Same two-commit doctrine as
    `write_conversation_memory`: the row's own commit already happened, and a
    workspace_events sequence collision must cost the signal, never the row.
    The stream filters per viewer on `owner_id` (api/coworking._event_visible),
    so a personal row's ping reaches its owner's tabs and nobody else's.
    """
    try:
        append_workspace_event(
            db,
            workspace_id=workspace_id,
            event_type="memory.updated",
            payload={
                "run_id": "",
                "conversation_id": "",
                "count": 1,
                "ids": [item_id],
                "owner_id": owner_id,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.warning(
            "memory.updated event was not appended for memory %s",
            item_id,
            exc_info=True,
        )


def _memory_out(item: MemoryItem) -> MemoryItemOut:
    return MemoryItemOut(
        id=item.id,
        conversation_id=item.conversation_id,
        kind=item.kind,
        content=item.content,
        entity_names=json.loads(item.entity_names_json),
        message_ids=json.loads(item.message_ids_json),
        importance=item.importance,
        # A boolean and not the owner id: the caller only ever receives shared
        # rows and their own, so "is this everyone's" is the whole of what they
        # can learn, and putting a user id on the wire would say more. Same shape
        # `ConversationOut.shared` settled on for the identical question.
        shared=item.owner_id == SHARED_OWNER,
        space_id=item.space_id,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.get("/memory", response_model=List[MemoryItemOut])
def list_memory(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[MemoryItemOut]:
    """The workspace's memories and the caller's own — never another member's.

    Which means nobody, owner included, has a complete view of what the workspace
    knows. ADR 0010 records that as a real cost rather than an oversight: it is
    the same trade `Conversation.shared` already made, and the audit trail still
    records every write.
    """
    items = db.scalars(
        # ALL_SPACES: the admin surface. Every space is workspace-visible, so
        # hiding its shelf here would strand rows nowhere reviewable; the owner
        # axis (the privacy one) stays exactly as narrow as before.
        _active(select(MemoryItem), actor.workspace_id, actor.user_id, ALL_SPACES)
        .order_by(MemoryItem.updated_at.desc())
        .limit(200)
    )
    return [_memory_out(item) for item in items]


@router.post("/memory", response_model=MemoryItemOut, status_code=201)
def create_memory(
    payload: MemoryCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> MemoryItemOut:
    """Add a memory by hand.

    Scope is chosen by the authenticated user between exactly "mine" and
    "everyone's" — never another member's, and never a model-settable argument
    (the agent's `remember` tool derives scope from its conversation instead).
    """
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="memory.create",
        key=key,
    )
    if replay:
        # Through `_active`, the same chokepoint every other memory read uses
        # (and a scoped select rather than db.get, keeping DB_GET_ALLOWLIST
        # untouched). A spent key must never widen visibility: another
        # member's personal row and a since-forgotten tombstone both answer
        # replayed_resource_gone here, exactly as GET/PATCH/DELETE would hide
        # them — DELETE tombstones in place, so without the status filter the
        # `item is None` arm could never fire for memories at all.
        item = db.scalar(
            _active(
                select(MemoryItem), actor.workspace_id, actor.user_id, ALL_SPACES
            ).where(MemoryItem.id == replay.resource_id)
        )
        if item is None:
            raise replayed_resource_gone()
        return _memory_out(item)
    space_id = ""
    if payload.space_id:
        # Proved against the caller's workspace before it is stamped, exactly
        # as create_conversation does; "" passes through as the global shelf.
        try:
            space = spaces_service.get_space(
                db, workspace_id=actor.workspace_id, space_id=payload.space_id
            )
        except spaces_service.SpaceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        space_id = space.id
    # remember_memory already dedupes/reinforces, embeds via _embed_pending,
    # marks the graph stale and audits memory.remembered with the outcome.
    result = remember_memory(
        db,
        workspace_id=actor.workspace_id,
        conversation_id=None,
        user_id=actor.user_id,
        content=payload.content,
        kind=payload.kind,
        owner_id=SHARED_OWNER if payload.shared else actor.user_id,
        space_id=space_id,
    )
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="memory.create",
        key=key,
        resource_id=result.item.id,
    )
    item_id, owner_id = result.item.id, result.item.owner_id
    db.commit()
    _signal_memory_updated(
        db, workspace_id=actor.workspace_id, item_id=item_id, owner_id=owner_id
    )
    return _memory_out(result.item)


@router.patch("/memory/{memory_id}", response_model=MemoryItemOut)
def update_memory(
    memory_id: str,
    payload: MemoryUpdate,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> MemoryItemOut:
    """Rewrite one memory's sentence. A new value, never a re-scope.

    No Idempotency-Key: replaying the same content lands on the same state,
    the `set_conversation_defaults` precedent.
    """
    # The exact shape DELETE uses: another member's personal row is invisible,
    # not forbidden, so it 404s here for the same reason it is absent above.
    item = db.scalar(
        _active(select(MemoryItem), actor.workspace_id, actor.user_id, ALL_SPACES).where(
            MemoryItem.id == memory_id
        )
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    if item.kind == "summary":
        raise HTTPException(
            status_code=422,
            detail="The rolling summary rewrites itself; edit fact or preference memories.",
        )
    if not normalize_memory_content(payload.content):
        raise HTTPException(status_code=422, detail="Memory content is required")
    # Captured before the write: a rollback below expires the instance, and
    # the tombstone probe needs the row's own unique-key axes.
    owner_id, space_id, kind = item.owner_id, item.space_id, item.kind
    try:
        edit_memory(db, item=item, content=payload.content, settings=get_settings())
    except IntegrityError:
        db.rollback()
        # Which row is blocking decides what the 409 says. The constraint
        # includes tombstones, and a tombstone is invisible to GET — blaming
        # "another memory" would name a duplicate the caller can never find.
        blocker = db.scalar(
            select(MemoryItem).where(
                MemoryItem.workspace_id == actor.workspace_id,
                MemoryItem.owner_id == owner_id,
                MemoryItem.space_id == space_id,
                MemoryItem.kind == kind,
                MemoryItem.normalized_key
                == _content_key(normalize_memory_content(payload.content)),
                MemoryItem.id != memory_id,
            )
        )
        if blocker is not None and blocker.status != "active":
            raise HTTPException(
                status_code=409,
                detail=(
                    "That sentence was previously forgotten; add it as a new "
                    "memory to restore it"
                ),
            ) from None
        raise HTTPException(
            status_code=409, detail="Another memory already says this"
        ) from None
    # The write-time liveness re-check. The row was loaded through _active,
    # but the post-run extractor retires rows from its own session, so the
    # supersession of this exact row can land between that load and this
    # commit — and an edit that commits onto a retired row is acknowledged
    # with 200 and then invisible everywhere. One conditional UPDATE decides
    # it (the coworking.claim_card shape): zero rows means the slot moved on.
    still_active = db.execute(
        update(MemoryItem)
        .where(MemoryItem.id == memory_id, MemoryItem.status == "active")
        .values(updated_at=utcnow())
    ).rowcount
    if not still_active:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="This memory was just superseded; reload to see its replacement",
        )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="memory.edited",
        resource_type="memory_item",
        resource_id=item.id,
        detail={"kind": item.kind},
    )
    db.commit()
    db.refresh(item)
    _signal_memory_updated(
        db, workspace_id=actor.workspace_id, item_id=item.id, owner_id=item.owner_id
    )
    return _memory_out(item)


@router.delete("/memory/{memory_id}", status_code=204)
def forget_memory(
    memory_id: str,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="memory.forget",
        key=key,
    )
    if replay:
        return
    # `_active` rather than a status check of its own, so forgetting reaches
    # exactly the rows listing shows: shared plus the caller's own. Another
    # member's personal memory is a 404 here for the same reason it is invisible
    # above — it is not that you may not delete it, it is that it is not yours.
    item = db.scalar(
        _active(select(MemoryItem), actor.workspace_id, actor.user_id, ALL_SPACES).where(
            MemoryItem.id == memory_id
        )
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    # Same tombstone as the agent's `forget` tool, so this endpoint cannot leave
    # a deleted row parked on a claim key and make that claim unlearnable.
    item.status = "deleted"
    item.normalized_key = tombstone_key(db, item)
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="memory.forget",
        key=key,
        resource_id=item.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="memory.forgotten",
        resource_type="memory_item",
        resource_id=item.id,
        detail={"kind": item.kind},
    )
    db.commit()
