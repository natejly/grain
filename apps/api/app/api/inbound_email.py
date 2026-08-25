"""Inbound email: the provider webhook and the addresses it routes by.

Two surfaces in one feature module.

**The door** — `POST /api/hooks/email/inbound` — is a machine entry point on
the tick's exact posture: unauthenticated (a mail provider holds no session),
guarded by a shared bearer compared with `secrets.compare_digest`, 503 when
the secret is unconfigured. Inside, the routing token in the recipient is the
per-address credential; an unknown or revoked one answers `200
{accepted: false}` rather than a 404, because a live probe against this
endpoint must learn nothing about which addresses exist.

**The keys** — `/api/inbound-addresses` — are owner-gated management, the
ApiToken posture: the raw address appears exactly once in the mint response,
only its token's hash is stored, and revocation is a stamp.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, require_owner
from ..clock import utcnow
from ..config import Settings, get_settings
from ..database import get_db
from ..models import InboundAddress, Message, Space
from ..schemas import ApiModel
from ..services import inbound_email as address_service
from ..services.audit import record_audit
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api", tags=["inbound-email"])

#: Bounds on what a provider may hand us in one delivery. Generous for real
#: mail, small enough that a hostile payload buys nothing.
MAX_FIELD_CHARS = 2000
MAX_BODY_CHARS = 200000


class InboundEmailIn(BaseModel):
    """The generic provider shape: recipient, sender, subject, text, message_id.

    Everything defaults to "" because providers disagree about which fields a
    bounce or an empty mail carries — a missing field must never be a 422 the
    provider retries forever, let alone a 500.
    """

    recipient: str = Field(default="", max_length=MAX_FIELD_CHARS)
    sender: str = Field(default="", max_length=MAX_FIELD_CHARS)
    subject: str = Field(default="", max_length=MAX_FIELD_CHARS)
    text: str = Field(default="", max_length=MAX_BODY_CHARS)
    html: str = Field(default="", max_length=MAX_BODY_CHARS)
    message_id: str = Field(default="", max_length=MAX_FIELD_CHARS)


class InboundEmailOut(ApiModel):
    #: False for an unknown or revoked address — deliberately the same body,
    #: with a 200, so the endpoint is not an oracle over which addresses exist.
    accepted: bool
    conversation_id: str = ""
    message_id: str = ""


class InboundAddressOut(ApiModel):
    id: str
    label: str
    target_space_id: str
    created_at: datetime
    revoked_at: Optional[datetime]


class InboundAddressMintedOut(InboundAddressOut):
    #: The full address, present only in the mint response — the token inside
    #: it is stored hashed and cannot be re-derived. Blank on an idempotent
    #: replay, exactly as a replayed API token's secret is.
    address: str


class InboundAddressCreate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    target_space_id: str = Field(default="", max_length=64)


def _out(address: InboundAddress) -> InboundAddressOut:
    return InboundAddressOut(
        id=address.id,
        label=address.label,
        target_space_id=address.target_space_id,
        created_at=address.created_at,
        revoked_at=address.revoked_at,
    )


@router.post("/hooks/email/inbound", response_model=InboundEmailOut)
def receive_email(
    payload: InboundEmailIn,
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> InboundEmailOut:
    """Land one provider-delivered email as a thread. The tick's auth posture.

    The bearer authenticates the *provider*; the `+token` in the recipient
    routes to (and authorises) the workspace. Idempotency rides the provider's
    message id — redelivery of the same mail answers the original thread
    rather than posting it twice — and no agent turn is ever started.
    """
    configured = settings.inbound_email_webhook_secret
    if configured is None or not configured.get_secret_value():
        raise HTTPException(
            status_code=503, detail="Inbound email is not configured"
        )
    presented = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(presented, configured.get_secret_value()):
        raise HTTPException(status_code=401, detail="Not authorised")

    address = address_service.resolve(
        db, address_service.token_from_recipient(payload.recipient)
    )
    if address is None:
        # Unknown and revoked are the same quiet 200: a 4xx would both make
        # the provider retry forever and confirm to a probe which local parts
        # are real. Nothing was written, nothing is to see.
        return InboundEmailOut(accepted=False)

    replay_key = _delivery_key(payload.message_id)
    if replay_key:
        replay = find_replay(
            db,
            workspace_id=address.workspace_id,
            operation="email.inbound",
            key=replay_key,
        )
        if replay:
            # Redelivery of a mail that already landed. Answer what the first
            # delivery answered; if the thread has since been deleted, the
            # delivery still happened — accepted, with nothing to point at.
            message = db.get(Message, replay.resource_id)
            if message is None or message.workspace_id != address.workspace_id:
                return InboundEmailOut(accepted=True)
            return InboundEmailOut(
                accepted=True,
                conversation_id=message.conversation_id,
                message_id=message.id,
            )

    body = payload.text or address_service.strip_html(payload.html)
    conversation, message = address_service.deliver(
        db,
        address=address,
        sender=payload.sender,
        subject=payload.subject,
        body=body,
    )
    if replay_key:
        record_key(
            db,
            workspace_id=address.workspace_id,
            operation="email.inbound",
            key=replay_key,
            resource_id=message.id,
        )
    record_audit(
        db,
        workspace_id=address.workspace_id,
        actor_id=address.created_by,
        action="email.received",
        resource_type="conversation",
        resource_id=conversation.id,
        detail={
            "address_id": address.id,
            "sender": payload.sender[:200],
            "message_id": payload.message_id[:200],
        },
    )
    db.commit()
    return InboundEmailOut(
        accepted=True, conversation_id=conversation.id, message_id=message.id
    )


def _delivery_key(message_id: str) -> str:
    """The idempotency key for a provider message id; "" when there is none.

    Hashed rather than truncated: the column holds 200 characters, provider
    ids have no length contract, and two long ids sharing a prefix must not
    collapse into one delivery.
    """
    trimmed = message_id.strip()
    if not trimmed:
        return ""
    return "sha256:" + hashlib.sha256(trimmed.encode()).hexdigest()


@router.get("/inbound-addresses", response_model=List[InboundAddressOut])
def list_inbound_addresses(
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> List[InboundAddressOut]:
    rows = db.scalars(
        select(InboundAddress)
        .where(InboundAddress.workspace_id == actor.workspace_id)
        .order_by(InboundAddress.created_at, InboundAddress.id)
    )
    return [_out(address) for address in rows]


@router.post(
    "/inbound-addresses", response_model=InboundAddressMintedOut, status_code=201
)
def create_inbound_address(
    payload: InboundAddressCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> InboundAddressMintedOut:
    """Mint an address. The full `inbox+<token>@<domain>` appears here only.

    The target space is resolved under the caller's workspace FIRST, so a
    foreign space id is a plain 404 before anything — the domain check
    included — could say more.
    """
    if payload.target_space_id:
        space = db.scalar(
            select(Space.id).where(
                Space.id == payload.target_space_id,
                Space.workspace_id == actor.workspace_id,
            )
        )
        if space is None:
            raise HTTPException(status_code=404, detail="Space not found")
    if not settings.inbound_email_domain:
        raise HTTPException(
            status_code=503, detail="Inbound email is not configured"
        )
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="inbound_address.create",
        key=key,
    )
    if replay:
        address = db.get(InboundAddress, replay.resource_id)
        if address is None or address.workspace_id != actor.workspace_id:
            raise replayed_resource_gone()
        # The address existed once, in the original response; only its hash
        # remains. A replay proves the mint happened, nothing more.
        return InboundAddressMintedOut(**_out(address).model_dump(), address="")
    minted = address_service.mint(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        label=payload.label,
        target_space_id=payload.target_space_id,
    )
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="inbound_address.create",
        key=key,
        resource_id=minted.address.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="inbound_address.created",
        resource_type="inbound_address",
        resource_id=minted.address.id,
        detail={"label": minted.address.label},
    )
    db.commit()
    return InboundAddressMintedOut(
        **_out(minted.address).model_dump(),
        address=f"inbox+{minted.token}@{settings.inbound_email_domain}",
    )


@router.post(
    "/inbound-addresses/{address_id}/revoke", response_model=InboundAddressOut
)
def revoke_inbound_address(
    address_id: str,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> InboundAddressOut:
    """Stop mail landing through this address, now. Naturally idempotent."""
    address = db.scalar(
        select(InboundAddress).where(
            InboundAddress.id == address_id,
            InboundAddress.workspace_id == actor.workspace_id,
        )
    )
    if address is None:
        raise HTTPException(status_code=404, detail="Address not found")
    if address.revoked_at is None:
        address.revoked_at = utcnow()
        record_audit(
            db,
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            action="inbound_address.revoked",
            resource_type="inbound_address",
            resource_id=address.id,
            detail={"label": address.label},
        )
        db.commit()
    return _out(address)
