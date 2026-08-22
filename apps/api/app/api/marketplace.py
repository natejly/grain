"""The marketplace: publish a skill as a listing, browse the gallery, install a copy.

Four invariants this router owns:

- Publishing is a snapshot. The listing's payload is produced by the allowlist
  serializers in `services/marketplace.py` and appended as an immutable
  `ListingVersion`; editing the source afterwards changes nothing published.
- Installing is a copy. The payload lands as an ordinary local row in the
  caller's workspace (a skill arrives `shared=False`), owned by the installer,
  editable and deletable like anything they authored — a remote publisher can
  never mutate what runs in someone else's workspace.
- Visibility goes through `marketplace.resolve_visible`, and only through it.
  A foreign or invisible listing id is a 404, never a leak.
- Publish refuses secrets. The lint over payload strings answers 422 with the
  findings, so the refusal tells the publisher what to remove.

The whole router sits behind `marketplace_enabled`; off, every route answers
404 as if the feature did not exist.
"""
from __future__ import annotations

import json
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..config import get_settings
from ..database import get_db
from ..models import Listing, ListingVersion, Skill, Workspace
from ..schemas import (
    InstallOut,
    ListingCreate,
    ListingDetailOut,
    ListingOut,
    ListingUpdate,
    ListingVersionOut,
)
from ..services import marketplace as marketplace_service
from ..services import skills as skills_service
from ..services.audit import record_audit
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])


def require_marketplace() -> None:
    if not get_settings().marketplace_enabled:
        raise HTTPException(status_code=404, detail="Not found")


def _can_manage(actor: Actor, listing: Listing) -> bool:
    """Who may republish, edit, or delist: the publisher, or the owner of the
    workspace it was published from — and only from that workspace."""
    if listing.workspace_id != actor.workspace_id:
        return False
    return listing.created_by == actor.user_id or actor.role == "owner"


def _out(listing: Listing, actor: Actor) -> ListingOut:
    return ListingOut(
        id=listing.id,
        kind=listing.kind,
        slug=listing.slug,
        title=listing.title,
        description=listing.description,
        visibility=listing.visibility,
        status=listing.status,
        author_name=listing.author_name,
        install_count=listing.install_count,
        latest_version=listing.latest_version,
        mine=listing.workspace_id == actor.workspace_id,
        can_manage=_can_manage(actor, listing),
        created_at=listing.created_at,
        updated_at=listing.updated_at,
    )


def _detail(db: Session, listing: Listing, actor: Actor) -> ListingDetailOut:
    head = marketplace_service.latest_version(db, listing=listing)
    try:
        payload = json.loads(head.payload_json) if head else {}
    except ValueError:
        payload = {}
    versions = marketplace_service.list_versions(db, listing=listing)
    base = _out(listing, actor)
    publisher = db.scalar(
        select(Workspace.name).where(Workspace.id == listing.workspace_id)
    )
    return ListingDetailOut(
        **base.model_dump(),
        payload=payload if isinstance(payload, dict) else {},
        versions=[ListingVersionOut.model_validate(row) for row in versions],
        publisher_workspace=publisher or "",
    )


def _load_visible(db: Session, actor: Actor, listing_id: str) -> Listing:
    listing = marketplace_service.resolve_visible(
        db,
        workspace_id=actor.workspace_id,
        organization_id=actor.organization_id,
        listing_id=listing_id,
    )
    if listing is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    return listing


@router.get("/listings", response_model=List[ListingOut])
def list_listings(
    _: None = Depends(require_marketplace),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[ListingOut]:
    rows = marketplace_service.list_visible(
        db, workspace_id=actor.workspace_id, organization_id=actor.organization_id
    )
    return [_out(row, actor) for row in rows]


@router.get("/listings/{listing_id}", response_model=ListingDetailOut)
def get_listing(
    listing_id: str,
    _: None = Depends(require_marketplace),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ListingDetailOut:
    return _detail(db, _load_visible(db, actor, listing_id), actor)


@router.post("/listings", response_model=ListingDetailOut, status_code=201)
def publish_listing(
    payload: ListingCreate,
    key: str = Depends(idempotency_key),
    _: None = Depends(require_marketplace),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ListingDetailOut:
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="listing.publish", key=key
    )
    if replay:
        listing = db.scalar(
            select(Listing).where(
                Listing.id == replay.resource_id,
                Listing.workspace_id == actor.workspace_id,
            )
        )
        if listing is None:
            raise replayed_resource_gone()
        return _detail(db, listing, actor)

    # Publishing to the whole organization is owner-gated — the same tier of
    # decision as flipping a skill `shared` or publishing an app, one ring out.
    # Workspace-tier publishing stays open to any member: what transfers is
    # something the workspace could already read.
    if payload.visibility == "org" and actor.role != "owner":
        raise HTTPException(
            status_code=403,
            detail="Publishing to the organization requires the workspace owner",
        )

    skill = skills_service.resolve_visible(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        skill_id=payload.source_id,
    )
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    snapshot = marketplace_service.snapshot_skill(skill)
    payload_json = marketplace_service.serialize_payload(snapshot)
    digest = marketplace_service.payload_hash(payload_json)

    findings = marketplace_service.lint_strings(
        marketplace_service.payload_strings(snapshot), workspace_id=actor.workspace_id
    )
    if findings:
        raise HTTPException(
            status_code=422,
            detail="This cannot be published: " + "; ".join(findings) + ".",
        )

    existing = db.scalar(
        select(Listing).where(
            Listing.organization_id == actor.organization_id,
            Listing.slug == payload.slug,
        )
    )
    title = payload.title.strip() or skill.title
    if existing is None:
        listing = Listing(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            kind="skill",
            slug=payload.slug,
            title=title,
            description=payload.description.strip(),
            visibility=payload.visibility,
            status="published",
            author_name=payload.author_name.strip(),
            created_by=actor.user_id,
            latest_version=1,
        )
        db.add(listing)
        db.flush()
        version = 1
        action = "listing.published"
    else:
        # Republishing a slug appends a version — but only for the listing's
        # own manager, with the same kind, and with something to say about what
        # changed. Anyone else reusing the slug has picked a taken name: 409.
        head = marketplace_service.latest_version(db, listing=existing)
        if (
            not _can_manage(actor, existing)
            or existing.kind != "skill"
            or (head is not None and head.source_id != skill.id)
        ):
            # Including a *different* source under the same slug, even by the
            # same publisher: a slug names one thing's lineage, not a name pool.
            raise HTTPException(
                status_code=409,
                detail=f"A listing with the slug “{payload.slug}” already exists",
            )
        if head is not None and head.content_hash == digest:
            raise HTTPException(
                status_code=409,
                detail="Nothing changed: this content is already the published version",
            )
        if not payload.changelog.strip():
            raise HTTPException(
                status_code=422,
                detail="A changelog is required when republishing an existing listing",
            )
        listing = existing
        listing.title = title
        if payload.description.strip():
            listing.description = payload.description.strip()
        if payload.author_name.strip():
            listing.author_name = payload.author_name.strip()
        listing.latest_version += 1
        version = listing.latest_version
        action = "listing.republished"

    db.add(
        ListingVersion(
            workspace_id=actor.workspace_id,
            listing_id=listing.id,
            version=version,
            payload_json=payload_json,
            content_hash=digest,
            changelog=payload.changelog.strip() if version > 1 else "",
            source_id=skill.id,
            source_version=skill.version,
            created_by=actor.user_id,
        )
    )
    db.flush()
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="listing.publish",
        key=key,
        resource_id=listing.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action=action,
        resource_type="listing",
        resource_id=listing.id,
        detail={"slug": listing.slug, "kind": listing.kind, "version": version},
    )
    db.commit()
    return _detail(db, listing, actor)


@router.patch("/listings/{listing_id}", response_model=ListingDetailOut)
def update_listing(
    listing_id: str,
    payload: ListingUpdate,
    _: None = Depends(require_marketplace),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ListingDetailOut:
    """Head metadata only: title, description, byline, visibility, delist.

    The payload is deliberately not editable here — published versions are
    immutable, and the only way to change what installs is to publish again.
    A visible listing the caller may not manage answers 403, not 404: an org
    reader is entitled to know the listing exists (they can see it), just not
    to steer it. Loaded by workspace rather than through the browse chokepoint,
    because a manager must be able to reach their own *delisted* row — undoing
    a delist is the whole point of it being a status, not a delete.
    """
    listing = db.scalar(
        select(Listing).where(
            Listing.id == listing_id,
            Listing.workspace_id == actor.workspace_id,
            Listing.status != "taken_down",
        )
    )
    if listing is None:
        if (
            marketplace_service.resolve_visible(
                db,
                workspace_id=actor.workspace_id,
                organization_id=actor.organization_id,
                listing_id=listing_id,
            )
            is not None
        ):
            raise HTTPException(
                status_code=403,
                detail="Only the publisher's workspace may change a listing",
            )
        raise HTTPException(status_code=404, detail="Listing not found")
    if not _can_manage(actor, listing):
        raise HTTPException(
            status_code=403,
            detail="Only the publisher or their workspace owner may change a listing",
        )
    if payload.visibility == "org" and listing.visibility != "org" and actor.role != "owner":
        raise HTTPException(
            status_code=403,
            detail="Publishing to the organization requires the workspace owner",
        )
    if payload.title is not None:
        listing.title = payload.title.strip()
    if payload.description is not None:
        listing.description = payload.description.strip()
    if payload.author_name is not None:
        listing.author_name = payload.author_name.strip()
    if payload.visibility is not None:
        listing.visibility = payload.visibility
    if payload.status is not None:
        listing.status = payload.status
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="listing.updated",
        resource_type="listing",
        resource_id=listing.id,
        detail={
            "slug": listing.slug,
            "visibility": listing.visibility,
            "status": listing.status,
        },
    )
    db.commit()
    return _detail(db, listing, actor)


@router.post("/listings/{listing_id}/install", response_model=InstallOut, status_code=201)
def install_listing(
    listing_id: str,
    key: str = Depends(idempotency_key),
    _: None = Depends(require_marketplace),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> InstallOut:
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="listing.install", key=key
    )
    if replay:
        skill = db.scalar(
            select(Skill).where(
                Skill.id == replay.resource_id,
                Skill.workspace_id == actor.workspace_id,
            )
        )
        if skill is None:
            raise replayed_resource_gone()
        return InstallOut(
            kind="skill", resource_id=skill.id, name=skill.name, title=skill.title
        )

    listing = _load_visible(db, actor, listing_id)
    head = marketplace_service.latest_version(db, listing=listing)
    if head is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    try:
        snapshot = marketplace_service.SkillPayload.model_validate_json(
            head.payload_json
        )
    except ValueError:
        raise HTTPException(
            status_code=422, detail="This listing's payload is unreadable"
        ) from None

    name = marketplace_service.free_skill_name(
        db, workspace_id=actor.workspace_id, base=listing.slug
    )
    # The copy lands inert: `shared=False` no matter who installs it, so a
    # gallery import never becomes workspace-wide without the same deliberate
    # share step an authored skill requires.
    skill = skills_service.create_skill(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        name=name,
        title=snapshot.title,
        description=snapshot.description,
        body=snapshot.body,
        args=skills_service.parse_args(snapshot.args_json),
        shared=False,
    )
    listing.install_count += 1
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="listing.install",
        key=key,
        resource_id=skill.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="listing.installed",
        resource_type="listing",
        resource_id=listing.id,
        detail={"slug": listing.slug, "kind": listing.kind, "skill_id": skill.id},
    )
    db.commit()
    return InstallOut(
        kind="skill", resource_id=skill.id, name=skill.name, title=skill.title
    )
