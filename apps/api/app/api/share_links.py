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
A PAGE is the one deliberate exception to that rule and the reason to state it
here: a page is a snapshot on purpose, because its whole value is that a reader
following a marker sees the passage the answer was written from. Its staleness
is reported (`page_drifted`), never papered over.
It is also rate limited per source address (the shared `public_rate_limit`
dependency, public tier): every hit on a shared dashboard runs a live DuckDB
query, so an anonymous surface with no budget would be a compute amplifier for
whoever holds — or guesses at — URLs.

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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..clock import utcnow
from ..database import get_db
from ..models import (
    Conversation,
    Dashboard,
    Document,
    Message,
    Page,
    PageCitation,
    ShareLink,
    User,
)
from ..schemas import ApiModel, DashboardSpec
from ..services import conversations as conversations_service
from ..services import share_links as service
from ..services.analytics import AnalyticsValidationError, execute_dataset_query
from ..services.audit import record_audit
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone
from .ratelimit import public_rate_limit, rate_limit

router = APIRouter(tags=["share-links"])

#: Belt over braces: the query's own `limit` is schema-capped at 500, but the
#: public surface states its own ceiling so a future cap change cannot silently
#: turn an anonymous GET into a bulk export.
PUBLIC_ROW_CAP = 1000


class ShareLinkOut(ApiModel):
    id: str
    #: 'dashboard' | 'document' | 'conversation' | 'page'
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
    resource_kind: Literal["dashboard", "document", "conversation", "page"]
    resource_id: str = Field(min_length=1, max_length=36)
    #: Optional self-destruct: when set, `load_active` refuses the link from
    #: this moment on — the mitigation for a link that leaks and is forgotten.
    #: Must be in the future; omitted means the link lives until revoked.
    expires_at: Optional[datetime] = None


class SharedMessageOut(ApiModel):
    """One transcript turn as the anonymous reader sees it: who spoke (by
    display name — the mint is an explicit act by someone who can read the
    thread, and a multi-person transcript without attribution misreads who
    said what), what they said, and when."""

    role: str
    sender_name: str = ""
    content: str
    created_at: datetime
    #: True for a "/btw" aside (role "user", run_id "") — recorded in the
    #: thread and read by later turns, but never a prompt. The Markdown export
    #: labels these "(aside)" for exactly this reason (chat.py
    #: `_render_markdown`: unmarked, the transcript reads as an unanswered
    #: ask), and the strictly-more-public surface must not say less.
    is_aside: bool = False


class SharedPageCitationOut(ApiModel):
    """One frozen marker as the anonymous reader sees it.

    No chunk id and no source id: a reader outside the workspace cannot open
    either, and naming rows they cannot reach is a leak with no upside. The
    excerpt is the author's words at publish time, which is the whole point.
    """

    marker: int
    filename: str
    ordinal: int
    frozen_excerpt: str
    #: "frozen" | "changed" | "missing" — this marker's own drift verdict.
    status: str


class SharedResourceOut(ApiModel):
    """What an anonymous holder of a working link sees. One model for all
    kinds — the unset halves stay at their empty defaults — so the public page
    has one response shape to render."""

    #: 'dashboard' | 'document' | 'conversation' | 'page'
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
    # The conversation half.
    messages: List[SharedMessageOut] = []
    #: True when the thread outgrew `PUBLIC_ROW_CAP` and only the newest
    #: window is served — the dashboard branch's public-ceiling rule applied
    #: to transcripts, surfaced so the page can say earlier turns are omitted.
    truncated: bool = False
    # The page half. FROZEN, unlike every other half of this model.
    page_body: str = ""
    page_citations: List[SharedPageCitationOut] = []
    #: True once the drift sweep found a cited passage edited or gone. The
    #: reader still sees the published text; this is how they are told it is no
    #: longer what the workspace holds.
    page_drifted: bool = False


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
    db: Session, *, actor: Actor, resource_kind: str, resource_id: str
) -> None:
    """The thing being shared must exist in the caller's own workspace — a
    foreign id 404s here, uniformly, before anything else can answer.

    Dashboards and documents resolve on the workspace alone (flat share-link
    authority, per the module docstring). A conversation resolves through the
    visibility chokepoint instead: only the creator can reach — and therefore
    mint for — a personal thread, any member can for a shared one, and a
    foreign workspace 404s before either question is asked.

    A PAGE resolves flat, like a dashboard, and that is deliberate rather than
    an oversight: the personal-thread question was already asked and answered
    at PUBLISH time by `conversations.resolve_visible`, and what exists now is
    a frozen copy somebody made on purpose — not a window into a thread whose
    visibility can change under it afterwards.
    """
    if resource_kind == "page":
        found = db.scalar(
            select(Page.id).where(
                Page.id == resource_id,
                Page.workspace_id == actor.workspace_id,
            )
        )
        if found is None:
            raise HTTPException(status_code=404, detail="Page not found")
        return
    if resource_kind == "conversation":
        conversation = conversations_service.resolve_visible(
            db,
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            conversation_id=resource_id,
        )
        if conversation is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        if conversation.subject_id:
            raise HTTPException(
                status_code=409,
                detail="A subject thread belongs to its subject — share the "
                "document or dashboard instead",
            )
        if conversation.incognito:
            raise HTTPException(
                status_code=409,
                detail="A temporary chat cannot get a public link",
            )
        return
    if resource_kind == "dashboard":
        found = db.scalar(
            select(Dashboard.id).where(
                Dashboard.id == resource_id,
                Dashboard.workspace_id == actor.workspace_id,
            )
        )
        if found is None:
            raise HTTPException(status_code=404, detail="Dashboard not found")
        return
    found = db.scalar(
        select(Document.id).where(
            Document.id == resource_id,
            Document.workspace_id == actor.workspace_id,
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


@router.post(
    "/api/share-links",
    response_model=ShareLinkCreatedOut,
    status_code=201,
    dependencies=[Depends(rate_limit("share-link-mint", tier="mint"))],
)
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
        actor=actor,
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


# The anonymous surface is budgeted per source address by the shared
# `public_rate_limit` dependency rather than a local throttle: it counts before
# the token is resolved (so a miss spends budget too, pricing token guessing),
# it carries its own public tier and `Retry-After` instead of borrowing the far
# tighter credential-endpoint knobs, and `RATE_LIMITED_ROUTES` keeps a test
# asserting this route stays covered. Two things are blunted at once — a shared
# dashboard runs a live DuckDB query per hit, and the token path is the
# credential.
@router.get(
    "/shared/{token}",
    response_model=SharedResourceOut,
    dependencies=[Depends(public_rate_limit("shared-resource"))],
)
def read_shared_resource(
    token: str,
    db: Session = Depends(get_db),
) -> SharedResourceOut:
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
    if link.resource_kind == "page":
        # SNAPSHOT DECISION: every other kind here is a live window — this one
        # is not, and that is the whole product. A page's evidence is pinned at
        # publish time; staleness is told by `page_drifted`, which the sweep
        # sets, not by quietly serving text the reader never saw cited. A
        # future reader "fixing the inconsistency" by making pages live would
        # silently destroy the feature; test_page_share.py's "serves the frozen
        # body after the chunk is rewritten" assertion is the real guard.
        page = db.scalar(
            select(Page).where(
                Page.id == link.resource_id,
                Page.workspace_id == link.workspace_id,
            )
        )
        if page is None:
            raise _shared_not_found()
        citations = list(
            db.scalars(
                select(PageCitation)
                .where(
                    PageCitation.page_id == page.id,
                    PageCitation.workspace_id == link.workspace_id,
                )
                .order_by(PageCitation.marker)
                .limit(PUBLIC_ROW_CAP)
            )
        )
        return SharedResourceOut(
            kind="page",
            title=page.title,
            page_body=page.body_md,
            page_citations=[
                SharedPageCitationOut(
                    marker=citation.marker,
                    filename=citation.filename,
                    ordinal=citation.ordinal,
                    frozen_excerpt=citation.frozen_excerpt,
                    status=citation.status,
                )
                for citation in citations
            ],
            page_drifted=page.status == "drifted",
            updated_at=page.updated_at,
        )
    if link.resource_kind == "conversation":
        # LIVE-WINDOW DECISION: documents and dashboards serve current content
        # at request time ("a share link is a window, not a snapshot" — this
        # module's own header), so a conversation serves the transcript as it
        # stands now. Edits, message-edit truncations and deletion all
        # propagate, and a purged conversation fail-closes to the same 404.
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.id == link.resource_id,
                Conversation.workspace_id == link.workspace_id,
            )
        )
        if conversation is None:
            raise _shared_not_found()
        # THE PERSONAL-LEAK GATE: a thread unshared after a colleague minted
        # goes dark through their link, while the creator's own link on their
        # personal thread keeps serving. The anonymous caller learns nothing
        # from the uniform 404.
        if not conversation.shared and link.created_by != conversation.created_by:
            raise _shared_not_found()
        # The public payload states its own ceiling, exactly like the
        # dashboard branch's row cap above: an anonymous GET must not become
        # a bulk export, and the per-address rate limit prices requests, not
        # bytes. A tail window — the newest PUBLIC_ROW_CAP turns, served
        # oldest-first — with one extra row fetched only to learn whether
        # anything was cut.
        newest_first = list(
            db.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation.id,
                    Message.workspace_id == link.workspace_id,
                )
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(PUBLIC_ROW_CAP + 1)
            )
        )
        truncated = len(newest_first) > PUBLIC_ROW_CAP
        messages = list(reversed(newest_first[:PUBLIC_ROW_CAP]))
        # Sender names, the one-query pattern list_messages uses (chat.py's
        # _sender_names precedent): resolved only for this workspace's rows.
        sender_ids = {message.created_by for message in messages if message.created_by}
        names: Dict[str, str] = {}
        if sender_ids:
            names = {
                user_id: name
                for user_id, name in db.execute(
                    select(User.id, User.name).where(User.id.in_(sender_ids))
                )
            }
        return SharedResourceOut(
            kind="conversation",
            title=conversation.title,
            messages=[
                SharedMessageOut(
                    role=message.role,
                    sender_name=names.get(message.created_by, ""),
                    content=message.content,
                    created_at=message.created_at,
                    # The export's aside rule, verbatim (chat.py
                    # _render_markdown): role "user" with no run is a "/btw"
                    # note, not a prompt the assistant ignored.
                    is_aside=message.role == "user" and message.run_id == "",
                )
                for message in messages
            ],
            updated_at=conversation.updated_at,
            truncated=truncated,
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
