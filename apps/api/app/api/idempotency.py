"""One answer, in one place, to "I have seen this Idempotency-Key before".

Every guarded route used to inline the same three steps — SELECT the record,
return the resource if the branch happened to find one, INSERT the key — and the
gap between them is where the guarantee leaked:

* the replay branch only *returned* when the resource still existed. A retry of
  a key whose resource has since been deleted fell out of the branch and
  re-inserted the same key, which is a `UniqueConstraint('workspace_id',
  'operation', 'key')` violation with nothing catching it — a deterministic 500
  from the exact request an Idempotency-Key exists to make safe.
* two overlapping requests carrying one key both miss the SELECT and both
  INSERT, so the loser hits the same unhandled violation.

Both are the same mistake — treating the SELECT as the guard when the unique
index is the guard — so they get one answer here. A replay is never allowed to
fall through into doing the work a second time: it either returns the original
resource or it is refused, and `409` is the refusal because a key that has
already bought something is a conflict, not a bad request and not a crash.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import IdempotencyRecord


def find_replay(
    db: Session, *, workspace_id: str, operation: str, key: str
) -> Optional[IdempotencyRecord]:
    """The record a previous request left for this key, if there is one."""
    return db.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.workspace_id == workspace_id,
            IdempotencyRecord.operation == operation,
            IdempotencyRecord.key == key,
        )
    )


def replayed_resource_gone() -> HTTPException:
    """The key was spent and what it bought is gone.

    Not a 404: the request itself is well-formed and the route is real. Not a
    200 either — there is no resource to return, and inventing a second one
    would be exactly the duplicate the key was sent to prevent. The client is
    being told its retry arrived too late to be answered, and that it must not
    keep retrying with this key.
    """
    return HTTPException(
        status_code=409,
        detail=(
            "This Idempotency-Key was already used and the resource it created "
            "no longer exists. Retry with a new key to create a new one."
        ),
    )


def replay_in_flight() -> HTTPException:
    """Another request holding this key is still running, or just finished."""
    return HTTPException(
        status_code=409,
        detail="Another request with this Idempotency-Key is already in progress.",
    )


def record_key(
    db: Session,
    *,
    workspace_id: str,
    operation: str,
    key: str,
    resource_id: str,
) -> None:
    """Claim the key for this request, letting the unique index decide.

    The flush is what makes the claim real before the rest of the handler runs,
    so a concurrent duplicate is refused here rather than surfacing as an
    unhandled `IntegrityError` out of the final commit. A violation is only
    reported as a replay once the record is confirmed to exist — otherwise the
    handler's own constraints (a duplicate slug, a duplicate name) would be
    mislabelled as an idempotency conflict, so those keep raising.
    """
    db.add(
        IdempotencyRecord(
            workspace_id=workspace_id,
            operation=operation,
            key=key,
            resource_id=resource_id,
        )
    )
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if find_replay(db, workspace_id=workspace_id, operation=operation, key=key):
            raise replay_in_flight() from None
        raise
