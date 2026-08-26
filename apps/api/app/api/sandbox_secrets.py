"""REST surface for the sandbox's workspace secrets — the "connect stuff" seam.

A secret is a credential the workspace lets its sandbox code read as an
environment variable (`os.environ["STRIPE_API_KEY"]`). The value is written once,
encrypted at rest under the integrations key, and never returned by any route
here: `list_secrets` answers with names and metadata, and the one path that
decrypts is `secrets.secret_env`, called by `ensure_session` when a machine is
built. See `services/sandbox/secrets.py` for the three rules that make that safe.

Write is owner-only and read is member-visible, matching how the rest of the
workspace treats a shared credential: any member's sandbox code can *use* the
secret (they share the machine), but adding or removing one is an owner act, the
same standing `require_owner` guards elsewhere. Nothing here accepts or returns a
provider-side id; the tenant boundary is the `workspace_id` on the actor, applied
to every query, exactly as the sessions routes rely on `resolve_session`.
"""
from __future__ import annotations

from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor, require_owner
from ..config import Settings, get_settings
from ..database import get_db
from ..models import SandboxSecret
from ..schemas import ApiModel
from ..services.audit import record_audit
from ..services.sandbox import secrets as secrets_service
from ..services.sandbox.secrets import SecretError

router = APIRouter(prefix="/api/sandbox/secrets", tags=["sandbox"])

#: A generous ceiling for a token or key. Long enough for a PEM private key,
#: short enough that the column is not a place to stash a file.
MAX_VALUE_CHARS = 16_384


class SecretOut(ApiModel):
    """A secret by name and provenance. Never carries the value — the field does
    not exist rather than being blanked, so no serialization mistake can add it."""

    name: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class SecretRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    value: str = Field(..., min_length=1, max_length=MAX_VALUE_CHARS)


def _secret_out(row: SandboxSecret) -> SecretOut:
    return SecretOut(
        name=row.name,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=List[SecretOut])
def list_sandbox_secrets(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[SecretOut]:
    """Every secret this workspace holds, by name. Values stay encrypted; a
    listing has no business decrypting anything, so nothing here can."""
    return [
        _secret_out(row)
        for row in secrets_service.list_secrets(db, workspace_id=actor.workspace_id)
    ]


@router.put("", response_model=SecretOut, status_code=201)
def set_sandbox_secret(
    payload: SecretRequest,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SecretOut:
    """Create or replace a secret. Idempotent by name — PUT, not POST — because
    rotating a key is the common case and "already exists" is not an error the
    caller should have to branch on.

    A bad name (or unconfigured encryption) is a 400 with the service layer's
    message intact: it names the constraint the owner has to fix, and nothing in
    it is a value or a key.
    """
    try:
        row = secrets_service.set_secret(
            db,
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            name=payload.name,
            value=payload.value,
            settings=settings,
        )
    except SecretError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="sandbox_secret.set",
        resource_type="sandbox_secret",
        resource_id=row.name,  # the name, never the value
        detail={"name": row.name},
    )
    db.commit()
    return _secret_out(row)


@router.delete("/{name}", status_code=204)
def delete_sandbox_secret(
    name: str,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> None:
    """Remove a secret. A miss is a 404 rather than a silent success, so the UI
    can tell "removed" from "was never here" — the name is caller-supplied and
    echoing it back in the 404 leaks nothing it did not already hold."""
    removed = secrets_service.delete_secret(
        db, workspace_id=actor.workspace_id, name=name
    )
    if not removed:
        raise HTTPException(status_code=404, detail="Sandbox secret not found")
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="sandbox_secret.deleted",
        resource_type="sandbox_secret",
        resource_id=name,
        detail={"name": name},
    )
    db.commit()
