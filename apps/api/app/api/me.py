"""Per-member preferences: the caller's own membership, nobody else's.

One route today — the daily digest opt-in. It takes no resource id at all:
the row it edits is the (workspace, user) membership the session already
names, so there is nothing here for a foreign id to probe (the isolation
sweep covers it as SCOPED). A natural upsert of two columns, so no
Idempotency-Key — replaying "enabled at 9" is "enabled at 9".

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


@router.put("/digest", response_model=DigestPrefsOut)
def update_digest_prefs(
    payload: DigestPrefsIn,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> DigestPrefsOut:
    """Set the caller's own digest opt-in and hour. Member self-serve."""
    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == actor.workspace_id,
            Membership.user_id == actor.user_id,
        )
    )
    if membership is None:
        # A session outliving its membership: the actor dependency vouched for
        # the workspace, but the row is the thing being edited.
        raise HTTPException(status_code=404, detail="Membership not found")
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
