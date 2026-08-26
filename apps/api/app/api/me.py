"""Per-member preferences: the caller's own membership, nobody else's.

Two routes — the daily digest opt-in, and Safe mode. Neither takes a resource
id at all: the row each edits is the (workspace, user) membership the session
already names, so there is nothing here for a foreign id to probe (the
isolation sweep covers them as SCOPED). Natural upserts of one or two columns,
so no Idempotency-Key — replaying "enabled at 9" is "enabled at 9".

The matching read is on `GET /api/bootstrap`, beside the identity these
preferences belong to; a second GET here would be a smaller copy of that.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Membership
from ..schemas import ApiModel
from ..services.audit import record_audit

router = APIRouter(prefix="/api/me", tags=["me"])


class DigestPrefsIn(ApiModel):
    enabled: bool
    #: The UTC hour after which the daily mail may go out. Pydantic's bounds
    #: are the validation — 24 is a 422 at the door, never a bad row.
    hour_utc: int = Field(ge=0, le=23)


class DigestPrefsOut(ApiModel):
    enabled: bool
    hour_utc: int


class SafeModePrefIn(ApiModel):
    enabled: bool


class SafeModePrefOut(ApiModel):
    enabled: bool


def _own_membership(db: Session, actor: Actor) -> Membership:
    """The caller's membership row, or the 404 that says it is gone.

    The actor dependency vouched for the workspace; it does not vouch for the
    row, and a session outliving its membership is the case both routes here
    have to answer the same way.
    """
    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == actor.workspace_id,
            Membership.user_id == actor.user_id,
        )
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="Membership not found")
    return membership


@router.put("/safe-mode", response_model=SafeModePrefOut)
def update_safe_mode(
    payload: SafeModePrefIn,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SafeModePrefOut:
    """Turn the approval step on or off for the caller's future threads.

    A seed, not a switch on anything already running: it changes what
    `services.conversations.default_approval_mode` hands the next thread this
    member creates, and touches no existing conversation. That is the whole
    reason it can be a plain preference rather than a privileged operation —
    turning it *off* cannot loosen a thread a colleague is watching, and
    turning it *on* cannot strand one mid-turn.

    Audited on both edges. Off is the interesting direction, and an audit trail
    that only recorded the cautious half would be no trail at all.
    """
    membership = _own_membership(db, actor)
    membership.safe_mode = payload.enabled
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="safe_mode.updated",
        resource_type="membership",
        resource_id=membership.id,
        detail={"enabled": payload.enabled},
    )
    db.commit()
    return SafeModePrefOut(enabled=membership.safe_mode)


@router.put("/digest", response_model=DigestPrefsOut)
def update_digest_prefs(
    payload: DigestPrefsIn,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DigestPrefsOut:
    """Set the caller's own digest opt-in and hour. Member self-serve."""
    membership = _own_membership(db, actor)
    membership.digest_enabled = payload.enabled
    membership.digest_hour_utc = payload.hour_utc
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="digest.updated",
        resource_type="membership",
        resource_id=membership.id,
        detail={"enabled": payload.enabled, "hour_utc": payload.hour_utc},
    )
    db.commit()
    return DigestPrefsOut(
        enabled=membership.digest_enabled, hour_utc=membership.digest_hour_utc
    )
