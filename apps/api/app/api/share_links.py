"""Share links: mint, list and revoke them; serve what they point at.

Two surfaces in one module, deliberately side by side so the boundary between
them stays legible:

*The authenticated side* (`/api/share-links...`) is a normal workspace-scoped
CRUD router: the resource is resolved under the caller's workspace FIRST so a
foreign id uniformly 404s, creation is idempotent under `share_link.create`,
and every mutation audits. The raw token appears in exactly one response — the
201 that minted it — and nowhere else: the list omits even the hash, and an
idempotent replay of the create comes back with the token blank, because the
database holds only a digest and "raw exactly once" is the house token rule.

*The public side* (`GET /shared/{token}`) is the published-app pattern
(`api/generated_apps.py`): no `get_actor`, the unguessable value is the whole
credential, and everything is fail-closed — unknown, revoked, expired and
deleted-resource all answer the same 404. The workspace is resolved from the
link row, never from the request, and a dashboard's answer is re-run LIVE
against its dataset at request time: a share link is a window, not a snapshot,
and must never leak a stale copy of data the workspace has since corrected
(nor reuse a frozen release manifest, which answers a different question).
It is also rate limited per source address (`_throttle`): every hit on a
shared dashboard runs a live DuckDB query, so an anonymous surface with no
budget would be a compute amplifier for whoever holds — or guesses at — URLs.

AUTHZ, decided (QA F9): share-link authority is FLAT — any member may publish
any dashboard or document their workspace holds, and any member may revoke any
of its links, including a colleague's. Deliberate, on two grounds: minting
mirrors edit rights (every member can already modify these resources, so
gating who may *show* one adds a role check the write surface does not have),
and revocation open to all members is the safety valve — the person who spots
a leaked link must be able to stop it without hunting down its owner. This
sits intentionally beside F10's owner-gate for third-party subscriptions:
routing a *colleague's* attention is the move that needs the owner role;
widening a resource the member can already edit is not.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..clock import utcnow
from ..config import get_settings
from ..database import get_db
from ..models import Dashboard, Document, ShareLink
from ..schemas import ApiModel, DashboardSpec
from ..services import share_links as service
from ..services.analytics import AnalyticsValidationError, execute_dataset_query
from ..services.audit import record_audit
from ..services.auth.ratelimit import auth_rate_limiter
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(tags=["share-links"])

#: Belt over braces: the query's own `limit` is schema-capped at 500, but the
#: public surface states its own ceiling so a future cap change cannot silently
#: turn an anonymous GET into a bulk export.
PUBLIC_ROW_CAP = 1000


class ShareLinkOut(ApiModel):
    id: str
    #: 'dashboard' | 'document'
    resource_kind: str
    resource_id: str
    created_by: str
    created_at: datetime
    expires_at: Optional[datetime]
    revoked_at: Optional[datetime]


class ShareLinkCreatedOut(ApiModel):
    """The 201 body, and the only place the raw token ever appears.

    `AdminInviteCreatedOut`'s contract: the database holds a SHA-256, the list
    route never returns tokens, and nothing logs it. An idempotent replay of
    the create answers with the link row but `token`/`url_path` blank — the
    raw value cannot be re-derived from its hash, and a credential that
    appears in two responses is a credential that appears in logs.
    """

    link: ShareLinkOut
    token: str
    #: The path the web app serves the link at ("/share/{token}"); blank on
    #: replay, like the token it contains.
    url_path: str


class ShareLinkCreateRequest(BaseModel):
    resource_kind: Literal["dashboard", "document"]
    resource_id: str = Field(min_length=1, max_length=36)
    #: Optional self-destruct: when set, `load_active` refuses the link from
    #: this moment on — the mitigation for a link that leaks and is forgotten.
    #: Must be in the future; omitted means the link lives until revoked.
    expires_at: Optional[datetime] = None


class SharedResourceOut(ApiModel):
    """What an anonymous holder of a working link sees. One model for both
    kinds — the unset half stays at its empty default — so the public page has
    one response shape to render."""

    #: 'dashboard' | 'document'
    kind: str
    title: str
    # The dashboard half: the stored spec (how to draw) plus a live answer.
    spec_json: str = ""
    columns: List[str] = []
    rows: List[Dict[str, Any]] = []
    generated_at: Optional[datetime] = None
    # The document half.
    document_kind: str = ""
    content: str = ""
    updated_at: Optional[datetime] = None


def _out(link: ShareLink) -> ShareLinkOut:
    return ShareLinkOut(
        id=link.id,
        resource_kind=link.resource_kind,
        resource_id=link.resource_id,
        created_by=link.created_by,
        created_at=link.created_at,
        expires_at=link.expires_at,
        revoked_at=link.revoked_at,
    )


def _resolve_resource(
    db: Session, *, workspace_id: str, resource_kind: str, resource_id: str
) -> None:
    """The thing being shared must exist in the caller's own workspace — a
    foreign id 404s here, uniformly, before anything else can answer."""
    if resource_kind == "dashboard":
        found = db.scalar(
            select(Dashboard.id).where(
                Dashboard.id == resource_id,
                Dashboard.workspace_id == workspace_id,
            )
        )
        if found is None:
            raise HTTPException(status_code=404, detail="Dashboard not found")
        return
    found = db.scalar(
        select(Document.id).where(
            Document.id == resource_id,
            Document.workspace_id == workspace_id,
        )
    )
    if found is None:
        raise HTTPException(status_code=404, detail="Document not found")


def _load_link(db: Session, actor: Actor, link_id: str) -> ShareLink:
    link = db.scalar(
        select(ShareLink).where(
            ShareLink.id == link_id,
            ShareLink.workspace_id == actor.workspace_id,
        )
    )
    if link is None:
        raise HTTPException(status_code=404, detail="Share link not found")
    return link


# --------------------------------------------------------------------------
# The authenticated side


@router.post("/api/share-links", response_model=ShareLinkCreatedOut, status_code=201)
def create_share_link(
    payload: ShareLinkCreateRequest,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ShareLinkCreatedOut:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="share_link.create",
        key=key,
    )
    if replay:
        link = db.scalar(
            select(ShareLink).where(
                ShareLink.id == replay.resource_id,
                ShareLink.workspace_id == actor.workspace_id,
            )
        )
        if link is None:
            raise replayed_resource_gone()
        # The raw token went out with the first response and only its hash
        # remains; see ShareLinkCreatedOut.
        return ShareLinkCreatedOut(link=_out(link), token="", url_path="")
    expires_at = _validated_expiry(payload.expires_at)
    _resolve_resource(
        db,
        workspace_id=actor.workspace_id,
        resource_kind=payload.resource_kind,
        resource_id=payload.resource_id,
    )
    link, raw_token = service.issue(
        db,
        workspace_id=actor.workspace_id,
        resource_kind=payload.resource_kind,
        resource_id=payload.resource_id,
        created_by=actor.user_id,
        expires_at=expires_at,
    )
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="share_link.create",
        key=key,
        resource_id=link.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="share_link.created",
        resource_type="share_link",
        resource_id=link.id,
        # The kind and target, never the token — the audit trail is read by
        # more people, and kept for longer, than any response body.
        detail={
            "resource_kind": link.resource_kind,
            "resource_id": link.resource_id,
            "expires_at": link.expires_at.isoformat() if link.expires_at else "",
        },
    )
    db.commit()
    return ShareLinkCreatedOut(
        link=_out(link),
        token=raw_token,
        url_path=f"/share/{raw_token}",
    )


def _validated_expiry(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize a requested expiry to the house naive-UTC and insist it is in
    the future — a link born dead is a caller mistake worth naming, and an
    aware datetime compared against `utcnow()` would be a 500."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    if value <= utcnow():
        raise HTTPException(
            status_code=422, detail="expires_at must be in the future"
        )
    return value


@router.get("/api/share-links", response_model=List[ShareLinkOut])
def list_share_links(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[ShareLinkOut]:
    """Every link this workspace has issued, newest first — revoked and expired
    ones included, because "did we ever share that, and is it off now?" is the
    question this list is opened to answer. No token, hashed or raw, appears
    anywhere in the response."""
    rows = db.scalars(
        select(ShareLink)
        .where(ShareLink.workspace_id == actor.workspace_id)
        .order_by(ShareLink.created_at.desc(), ShareLink.id)
    ).all()
    return [_out(link) for link in rows]


@router.post("/api/share-links/{link_id}/revoke", response_model=ShareLinkOut)
def revoke_share_link(
    link_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ShareLinkOut:
    """Stop the link working, now. Naturally idempotent — revoking is a one-way
    door and a second click keeps the first timestamp — so it takes no
    Idempotency-Key, per the tool-policies precedent."""
    link = _load_link(db, actor, link_id)
    if service.revoke(link):
        record_audit(
            db,
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            action="share_link.revoked",
            resource_type="share_link",
            resource_id=link.id,
            detail={
                "resource_kind": link.resource_kind,
                "resource_id": link.resource_id,
            },
        )
        db.commit()
    return _out(link)


# --------------------------------------------------------------------------
# The public side


def _shared_not_found() -> HTTPException:
    # One message for every way a link can not-work: unknown, revoked, expired,
    # or pointing at something since deleted. An anonymous caller learns only
    # "this link serves nothing".
    return HTTPException(status_code=404, detail="Share link not found")


def _throttle(request: Request) -> None:
    """Budget the anonymous surface per source address, before any lookup.

    Reuses `auth_rate_limiter` under its own `shared:` bucket (same knobs, same
    per-process caveat as `api/auth.py` — the durable half there is the account
    lockout; here it is revocation). Two things are being blunted at once: a
    shared dashboard runs a live DuckDB query per hit, and the token path is
    the credential, so the same budget also prices token guessing. Counted
    before resolution on purpose — a miss must spend budget too.
    """
    settings = get_settings()
    client = request.client.host if request.client else "unknown"
    if not auth_rate_limiter.allow(
        f"shared:{client}",
        limit=settings.auth_rate_limit_attempts,
        window_seconds=settings.auth_rate_limit_window_seconds,
    ):
        raise HTTPException(
            status_code=429, detail="Too many attempts. Try again later."
        )


@router.get("/shared/{token}", response_model=SharedResourceOut)
def read_shared_resource(
    token: str,
    request: Request,
    db: Session = Depends(get_db),
) -> SharedResourceOut:
    _throttle(request)
    link = service.load_active(db, raw_token=token)
    if link is None:
        raise _shared_not_found()
    if link.resource_kind == "dashboard":
        dashboard = db.scalar(
            select(Dashboard).where(
                Dashboard.id == link.resource_id,
                Dashboard.workspace_id == link.workspace_id,
            )
        )
        if dashboard is None:
            raise _shared_not_found()
        try:
            spec = DashboardSpec.model_validate(json.loads(dashboard.spec_json))
            result = execute_dataset_query(
                db,
                workspace_id=link.workspace_id,
                dataset_id=dashboard.dataset_id,
                query=spec.query,
            )
        except (AnalyticsValidationError, ValidationError, ValueError) as exc:
            # A dashboard that cannot answer — dataset purged, spec unreadable —
            # serves nothing rather than an error shape an anonymous caller
            # could probe. Fail-closed, like every other branch here.
            raise _shared_not_found() from exc
        return SharedResourceOut(
            kind="dashboard",
            title=dashboard.name,
            spec_json=dashboard.spec_json,
            columns=result.columns,
            rows=result.rows[:PUBLIC_ROW_CAP],
            generated_at=utcnow(),
        )
    document = db.scalar(
        select(Document).where(
            Document.id == link.resource_id,
            Document.workspace_id == link.workspace_id,
        )
    )
    if document is None:
        raise _shared_not_found()
    return SharedResourceOut(
        kind="document",
        title=document.title,
        document_kind=document.kind,
        content=document.content,
        updated_at=document.updated_at,
    )
