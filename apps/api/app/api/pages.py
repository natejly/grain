"""REST surface over published pages.

The share-link router's construction, deliberately: the resource is resolved
under the caller's workspace FIRST so a foreign id uniformly 404s, the create
is idempotent under one operation key, every mutation audits, and authority is
FLAT — any member may publish a thread they can see and any member may
re-validate or delete the workspace's pages. The one thing a page adds is that
its evidence is frozen, and that decision lives in `services/pages.py`.

Every fetch here is a workspace-scoped `select`, never `db.get`, so the
`DB_GET_ALLOWLIST` test is untouched — the standing rule `api/memory.py` and
`services/styles.py` both record.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import Page, PageCitation, ShareLink
from ..schemas import ApiModel
from ..services import pages as service
from ..services import share_links as share_link_service
from ..services.audit import record_audit
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone
from .ratelimit import rate_limit

router = APIRouter(prefix="/api/pages", tags=["pages"])


class PageOut(ApiModel):
    id: str
    #: The thread this was published from. A plain id, not a link: the page
    #: outlives the thread, and the thread may since have been purged.
    conversation_id: str
    title: str
    #: "published" | "drifted". Set by the sweep, never by a read.
    status: str
    drift_count: int
    #: The embedding generation active at publish time, so a receipt and a page
    #: can be compared.
    generation_id: str
    published_by: str
    #: None means the drift sweep has never reached this page — a different
    #: fact from "checked and clean", which is `status == "published"` with a
    #: timestamp.
    drift_checked_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class PageCitationOut(ApiModel):
    marker: int
    chunk_id: str
    source_id: str
    filename: str
    ordinal: int
    #: The author's words at publish time. Never re-read from the live chunk:
    #: that is the whole product.
    frozen_excerpt: str
    #: "frozen" | "changed" | "missing".
    status: str
    checked_at: Optional[datetime]


class PageDetailOut(ApiModel):
    page: PageOut
    body_md: str
    citations: List[PageCitationOut]


class PagePublishRequest(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=36)
    title: str = Field(min_length=1, max_length=200)


def _out(page: Page) -> PageOut:
    return PageOut(
        id=page.id,
        conversation_id=page.conversation_id,
        title=page.title,
        status=page.status,
        drift_count=page.drift_count,
        generation_id=page.generation_id,
        published_by=page.published_by,
        drift_checked_at=page.drift_checked_at,
        created_at=page.created_at,
        updated_at=page.updated_at,
    )


def _detail(db: Session, page: Page) -> PageDetailOut:
    rows = list(
        db.scalars(
            select(PageCitation)
            .where(
                PageCitation.page_id == page.id,
                PageCitation.workspace_id == page.workspace_id,
            )
            .order_by(PageCitation.marker)
        )
    )
    return PageDetailOut(
        page=_out(page),
        body_md=page.body_md,
        citations=[
            PageCitationOut(
                marker=row.marker,
                chunk_id=row.chunk_id,
                source_id=row.source_id,
                filename=row.filename,
                ordinal=row.ordinal,
                frozen_excerpt=row.frozen_excerpt,
                status=row.status,
                checked_at=row.checked_at,
            )
            for row in rows
        ],
    )


def _load(db: Session, actor: Actor, page_id: str) -> Page:
    """Resolve a page inside the caller's workspace, or 404.

    404 and not 403: a 403 would confirm the id names a real page in somebody
    else's workspace, which is the fact worth withholding.
    """
    page = db.scalar(
        select(Page).where(
            Page.id == page_id, Page.workspace_id == actor.workspace_id
        )
    )
    if page is None:
        raise HTTPException(status_code=404, detail="Page not found")
    return page


@router.post(
    "",
    response_model=PageDetailOut,
    status_code=201,
    dependencies=[Depends(rate_limit("page-publish", tier="mint"))],
)
def publish_page(
    payload: PagePublishRequest,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> PageDetailOut:
    """Freeze a thread into a page. The thread's visibility decides who may.

    Rate limited at the mint tier because publishing re-reads every cited chunk
    and writes a row per marker — the same shape of work minting a share link
    does, and budgeted the same way.
    """
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="page.publish", key=key
    )
    if replay:
        page = db.scalar(
            select(Page).where(
                Page.id == replay.resource_id,
                Page.workspace_id == actor.workspace_id,
            )
        )
        if page is None:
            raise replayed_resource_gone()
        return _detail(db, page)
    try:
        page = service.publish(
            db,
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            conversation_id=payload.conversation_id,
            title=payload.title,
        )
    except service.PageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    citation_count = db.scalar(
        select(PageCitation.id).where(PageCitation.page_id == page.id)
    )
    detail = _detail(db, page)
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="page.publish",
        key=key,
        resource_id=page.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="page.published",
        resource_type="page",
        resource_id=page.id,
        detail={
            "conversation_id": page.conversation_id,
            "citations": len(detail.citations),
            "has_citations": bool(citation_count),
        },
    )
    db.commit()
    return detail


@router.get("", response_model=List[PageOut])
def list_pages(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[PageOut]:
    rows = list(
        db.scalars(
            select(Page)
            .where(Page.workspace_id == actor.workspace_id)
            .order_by(Page.created_at.desc(), Page.id)
        )
    )
    return [_out(page) for page in rows]


@router.get("/{page_id}", response_model=PageDetailOut)
def get_page(
    page_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> PageDetailOut:
    return _detail(db, _load(db, actor, page_id))


@router.post("/{page_id}/revalidate", response_model=PageDetailOut)
def revalidate_page(
    page_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> PageDetailOut:
    """Re-check this page's evidence now, rather than waiting for the sweep.

    No Idempotency-Key: re-validating is naturally idempotent — it reads the
    live chunks and writes the verdict they imply, so a second click recomputes
    the same answer. The tool-policies precedent the revoke route cites.
    """
    page = _load(db, actor, page_id)
    frozen, changed, missing = service.revalidate(db, page=page)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="page.revalidated",
        resource_type="page",
        resource_id=page.id,
        detail={"frozen": frozen, "changed": changed, "missing": missing},
    )
    db.commit()
    return _detail(db, page)


@router.delete("/{page_id}", status_code=204)
def delete_page(
    page_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    """Delete a page and STOP EVERY LINK THAT SERVED IT.

    A page that is gone must not keep answering: the public route resolves the
    resource under the link's workspace, so a dangling link would 404 anyway —
    but a link that still appears live in the share-links list is a promise the
    product cannot keep, and revoking is the only honest state.
    """
    page = _load(db, actor, page_id)
    links = list(
        db.scalars(
            select(ShareLink).where(
                ShareLink.workspace_id == actor.workspace_id,
                ShareLink.resource_kind == "page",
                ShareLink.resource_id == page.id,
            )
        )
    )
    revoked = sum(1 for link in links if share_link_service.revoke(link))
    for citation in db.scalars(
        select(PageCitation).where(
            PageCitation.page_id == page.id,
            PageCitation.workspace_id == actor.workspace_id,
        )
    ):
        db.delete(citation)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="page.deleted",
        resource_type="page",
        resource_id=page.id,
        detail={"revoked_links": revoked, "title": page.title},
    )
    db.delete(page)
    db.commit()
