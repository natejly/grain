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
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..config import get_settings
from ..database import get_db
from ..models import (
    Agent,
    Listing,
    ListingInstall,
    ListingVersion,
    Skill,
    Workflow,
    Workspace,
)
from ..schemas import (
    InstallOut,
    ListingCreate,
    ListingDetailOut,
    ListingInstallBody,
    ListingOut,
    ListingPinBody,
    ListingUpdate,
    ListingUpdateApply,
    ListingVersionOut,
)
from ..services import marketplace as marketplace_service
from ..services import skills as skills_service
from ..services.audit import record_audit
from ..services.llm_tools import ToolContext, build_registry
from ..services.workflows.validate import parse_graph, validate_graph
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


def _out(
    db: Session,
    listing: Listing,
    actor: Actor,
    state_pinned: Optional[tuple[str, bool]] = None,
) -> ListingOut:
    state, pinned = (
        state_pinned
        if state_pinned is not None
        else marketplace_service.install_state(
            db, listing=listing, workspace_id=actor.workspace_id
        )
    )
    return ListingOut(
        install_state=state,
        pinned=pinned,
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
    base = _out(db, listing, actor)
    publisher = db.scalar(
        select(Workspace.name).where(Workspace.id == listing.workspace_id)
    )
    return ListingDetailOut(
        **base.model_dump(),
        payload=payload if isinstance(payload, dict) else {},
        versions=[ListingVersionOut.model_validate(row) for row in versions],
        publisher_workspace=publisher or "",
    )


def _load_source(db: Session, actor: Actor, payload: ListingCreate):
    """The thing being published, snapshotted through its kind's allowlist.

    Returns (snapshot, source_version, default_title), or raises the 404/422
    the load deserves. Skills keep their own visibility rule (own-or-shared);
    agents and workflows are workspace-wide by construction, so the workspace
    filter is the whole rule for them.
    """
    if payload.kind == "skill":
        skill = skills_service.resolve_visible(
            db,
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            skill_id=payload.source_id,
        )
        if skill is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        return marketplace_service.snapshot_skill(skill), skill.version, skill.title
    if payload.kind == "agent":
        agent = db.scalar(
            select(Agent).where(
                Agent.id == payload.source_id,
                Agent.workspace_id == actor.workspace_id,
            )
        )
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        # Agents carry no version counter; 0 is the honest "unversioned source".
        return (
            marketplace_service.snapshot_agent(db, agent, user_id=actor.user_id),
            0,
            agent.name,
        )
    workflow = db.scalar(
        select(Workflow).where(
            Workflow.id == payload.source_id,
            Workflow.workspace_id == actor.workspace_id,
        )
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    try:
        workflow_snapshot = marketplace_service.snapshot_workflow(workflow)
    except ValueError:
        raise HTTPException(
            status_code=422, detail="This workflow's graph is unreadable"
        ) from None
    return workflow_snapshot, workflow.version, workflow.name


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
    states = marketplace_service.install_states(
        db, listings=rows, workspace_id=actor.workspace_id
    )
    return [_out(db, row, actor, states[row.id]) for row in rows]


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

    snapshot, source_version, default_title = _load_source(db, actor, payload)
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
    title = payload.title.strip() or default_title
    if existing is None:
        listing = Listing(
            organization_id=actor.organization_id,
            workspace_id=actor.workspace_id,
            kind=payload.kind,
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
        # A taken-down slug is not reachable by republish any more than by
        # PATCH — the takedown flow, when it exists, is the only door back.
        # Same non-confirming wording as the plain conflict below.
        if existing.status == "taken_down":
            raise HTTPException(
                status_code=409,
                detail=f"The slug “{payload.slug}” is not available",
            )
        head = marketplace_service.latest_version(db, listing=existing)
        if (
            not _can_manage(actor, existing)
            or existing.kind != payload.kind
            or (head is not None and head.source_id != payload.source_id)
        ):
            # Including a *different* source under the same slug, even by the
            # same publisher: a slug names one thing's lineage, not a name pool.
            # "Not available" rather than "already exists" — the conflicting row
            # may be invisible to this caller (delisted, or workspace-tier in a
            # sibling workspace), and its existence is not theirs to confirm.
            raise HTTPException(
                status_code=409,
                detail=f"The slug “{payload.slug}” is not available",
            )
        # New versions of an org-tier listing ship straight to the whole
        # organization, so republishing is gated exactly like widening is:
        # the owner approved what crossed the workspace boundary, and the
        # author alone cannot swap that content out from under them.
        if existing.visibility == "org" and actor.role != "owner":
            raise HTTPException(
                status_code=403,
                detail="Republishing an organization listing requires the workspace owner",
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
        # Republish-and-widen in one step: honored, never the reverse — the
        # body's default visibility is "workspace" and must not silently
        # narrow an org listing. (The owner gate above already vetted "org".)
        if payload.visibility == "org":
            listing.visibility = "org"
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
            source_id=payload.source_id,
            source_version=source_version,
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
        if not payload.title.strip():
            raise HTTPException(status_code=422, detail="Title cannot be blank")
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
    payload: Optional[ListingInstallBody] = None,
    key: str = Depends(idempotency_key),
    _: None = Depends(require_marketplace),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> InstallOut:
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="listing.install", key=key
    )
    if replay:
        return _replayed_install(db, actor, replay.resource_id)

    listing = _load_visible(db, actor, listing_id)
    head = marketplace_service.latest_version(db, listing=listing)
    if head is None:
        raise HTTPException(status_code=404, detail="Listing not found")

    if listing.kind == "skill":
        result = _install_skill(db, actor, listing, head)
    elif listing.kind == "agent":
        result = _install_agent(db, actor, listing, head, payload)
    else:
        result = _install_workflow(db, actor, listing, head)

    listing.install_count += 1
    # Lineage: one row per (workspace, listing). A re-install re-points the
    # row at the fresh copy rather than growing a second history, and clears
    # any pin — the pin froze the *previous* copy, and taking a fresh install
    # is exactly the "I want the head now" act the pin existed to prevent
    # happening silently.
    local_hash = marketplace_service.local_content_hash(
        db,
        kind=listing.kind,
        target_id=result.resource_id,
        workspace_id=actor.workspace_id,
    )
    lineage = marketplace_service.find_install(
        db, workspace_id=actor.workspace_id, listing_id=listing.id
    )
    if lineage is None:
        lineage = ListingInstall(
            workspace_id=actor.workspace_id,
            listing_id=listing.id,
            created_by=actor.user_id,
        )
        db.add(lineage)
    lineage.listing_version_id = head.id
    lineage.target_kind = listing.kind
    lineage.target_id = result.resource_id
    lineage.content_hash_at_install = local_hash or ""
    lineage.pinned = False
    db.flush()
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="listing.install",
        key=key,
        resource_id=result.resource_id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="listing.installed",
        resource_type="listing",
        resource_id=listing.id,
        detail={
            "slug": listing.slug,
            "kind": listing.kind,
            "resource_id": result.resource_id,
        },
    )
    db.commit()
    return result


def _payload_unreadable() -> HTTPException:
    return HTTPException(status_code=422, detail="This listing's payload is unreadable")


def _copy_gone() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail="The installed copy no longer exists — install it again instead",
    )


def _install_skill(
    db: Session, actor: Actor, listing: Listing, head: ListingVersion
) -> InstallOut:
    try:
        snapshot = marketplace_service.SkillPayload.model_validate_json(
            head.payload_json
        )
    except ValueError:
        raise _payload_unreadable() from None
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
    return InstallOut(
        kind="skill", resource_id=skill.id, name=skill.name, title=skill.title
    )


def _install_agent(
    db: Session,
    actor: Actor,
    listing: Listing,
    head: ListingVersion,
    body: Optional[ListingInstallBody],
) -> InstallOut:
    try:
        snapshot = marketplace_service.AgentPayload.model_validate_json(
            head.payload_json
        )
    except ValueError:
        raise _payload_unreadable() from None

    warnings: list[str] = []
    if snapshot.unresolved_tools:
        warnings.append(
            "Not published with the listing (workspace-specific in the "
            "publisher's workspace): " + ", ".join(snapshot.unresolved_tools)
        )

    local = marketplace_service.registry_names(
        db, workspace_id=actor.workspace_id, user_id=actor.user_id
    )
    confirmed = None if body is None else body.allowed_tools
    if not snapshot.allowed_tools_json:
        # The source saw the whole registry. Without a scope sheet's confirmed
        # subset that semantics carries over ("" narrows nothing) — the agent
        # still lands disabled, and every tool call still meets ToolPolicy and
        # the org ceiling; with one, the subset is the grant.
        allowed_json = (
            ""
            if confirmed is None
            else json.dumps(sorted(set(confirmed) & local), separators=(",", ":"))
        )
    else:
        try:
            requested = set(json.loads(snapshot.allowed_tools_json))
        except ValueError:
            raise _payload_unreadable() from None
        if confirmed is not None:
            requested &= set(confirmed)
        missing = sorted(requested - local)
        if missing:
            warnings.append(
                "Not available in this workspace: " + ", ".join(missing)
            )
        allowed_json = json.dumps(sorted(requested & local), separators=(",", ":"))

    # ALWAYS disabled on arrival — even when the installer just confirmed the
    # scope sheet. Enabling is a separate deliberate act in the agent editor,
    # so adding a row here can never trip the last-enabled-agent invariant
    # either. The local ToolPolicy/OrgToolPolicy ceilings are untouched: the
    # subset narrows what the agent sees, never what the workspace permits.
    agent = Agent(
        workspace_id=actor.workspace_id,
        name=snapshot.name,
        instructions=snapshot.instructions,
        description=snapshot.description,
        allowed_tools_json=allowed_json,
        created_by=actor.user_id,
        enabled=False,
    )
    db.add(agent)
    db.flush()
    return InstallOut(
        kind="agent",
        resource_id=agent.id,
        name=agent.name,
        title=agent.name,
        warnings=warnings,
    )


def _workflow_landing_warnings(db: Session, actor: Actor, graph) -> list[str]:
    """Revalidate against the RECEIVING workspace's registry. What the
    publisher's workspace offered is irrelevant here; what matters is whether
    each node's tool resolves where the copy will run. Failures are warnings,
    not refusals — the copy lands either way, and a named hole beats a refusal
    with no artifact to fix."""
    context = ToolContext(
        workspace_id=actor.workspace_id, user_id=actor.user_id, conversation_id=""
    )
    report = validate_graph(graph, build_registry(db, context))
    warnings = [error.render() for error in report.errors]
    local_agents = {
        row
        for row in db.scalars(
            select(Agent.id).where(
                Agent.workspace_id == actor.workspace_id, Agent.enabled.is_(True)
            )
        )
    }
    foreign_agents = sorted(
        {
            node.id
            for node in graph.nodes
            if node.kind == "agent" and node.agent and node.agent not in local_agents
        }
    )
    if foreign_agents:
        warnings.append(
            "These steps name an agent this workspace does not have and need "
            "re-pointing here: " + ", ".join(foreign_agents)
        )
    return warnings


def _install_workflow(
    db: Session, actor: Actor, listing: Listing, head: ListingVersion
) -> InstallOut:
    try:
        snapshot = marketplace_service.WorkflowPayload.model_validate_json(
            head.payload_json
        )
        document = json.loads(snapshot.graph_json)
    except ValueError:
        raise _payload_unreadable() from None
    graph, parse_errors = parse_graph(document)
    if graph is None or parse_errors:
        raise _payload_unreadable()

    warnings = _workflow_landing_warnings(db, actor, graph)

    workflow = Workflow(
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        name=snapshot.name,
        description=snapshot.description,
        source_prompt=snapshot.source_prompt,
        graph_json=snapshot.graph_json,
        status="draft",
        trigger_kind="manual",
    )
    db.add(workflow)
    db.flush()
    return InstallOut(
        kind="workflow",
        resource_id=workflow.id,
        name=workflow.name,
        title=workflow.name,
        warnings=warnings,
    )


def _replayed_install(db: Session, actor: Actor, resource_id: str) -> InstallOut:
    """A retried install answered from what the first attempt created. The kind
    is recovered from whichever table holds the row — ids are UUIDs, so at most
    one of these resolves."""
    skill = db.scalar(
        select(Skill).where(
            Skill.id == resource_id, Skill.workspace_id == actor.workspace_id
        )
    )
    if skill is not None:
        return InstallOut(
            kind="skill", resource_id=skill.id, name=skill.name, title=skill.title
        )
    agent = db.scalar(
        select(Agent).where(
            Agent.id == resource_id, Agent.workspace_id == actor.workspace_id
        )
    )
    if agent is not None:
        return InstallOut(
            kind="agent", resource_id=agent.id, name=agent.name, title=agent.name
        )
    workflow = db.scalar(
        select(Workflow).where(
            Workflow.id == resource_id,
            Workflow.workspace_id == actor.workspace_id,
        )
    )
    if workflow is not None:
        return InstallOut(
            kind="workflow",
            resource_id=workflow.id,
            name=workflow.name,
            title=workflow.name,
        )
    raise replayed_resource_gone()


@router.post("/listings/{listing_id}/update", response_model=InstallOut)
def update_install(
    listing_id: str,
    payload: Optional[ListingUpdateApply] = None,
    key: str = Depends(idempotency_key),
    _: None = Depends(require_marketplace),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> InstallOut:
    """Bring this workspace's installed copy up to the listing's head version.

    Content fields only. The installer's operational choices — a skill's
    `shared`, an agent's `enabled` and local tool grant, a workflow's status
    and trigger — survive untouched: an update can bring new words, never a
    wider reach, and it is the person clicking Update (with the changelog in
    front of them) who consents to the new content, so nothing is re-inerted.

    A diverged copy (edited locally since install) refuses to be replaced
    unless `confirm_overwrite` says so; for skills even the overwrite is
    recoverable, because applying it appends an ordinary skill version.

    A pin does not block this route: a pin means "stop OFFERING updates"
    (install_state suppression), not "freeze against my own explicit acts" —
    the same reading that lets a re-install take the head. The pin survives
    the update, now freezing the version just taken.
    """
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="listing.update", key=key
    )
    if replay:
        return _replayed_install(db, actor, replay.resource_id)

    listing = _load_visible(db, actor, listing_id)
    lineage = marketplace_service.find_install(
        db, workspace_id=actor.workspace_id, listing_id=listing.id
    )
    if lineage is None:
        raise HTTPException(
            status_code=404,
            detail="Nothing installed from this listing in this workspace",
        )
    head = marketplace_service.latest_version(db, listing=listing)
    if head is None:
        raise HTTPException(status_code=404, detail="Listing not found")
    local = marketplace_service.local_content_hash(
        db,
        kind=lineage.target_kind,
        target_id=lineage.target_id,
        workspace_id=actor.workspace_id,
    )
    if local is None:
        raise _copy_gone()
    # Divergence is checked before "already current": a copy edited while at
    # the head version can still be re-synced to the published content with
    # confirm_overwrite — otherwise the diverged state would be a dead end.
    if local != lineage.content_hash_at_install:
        if not (payload is not None and payload.confirm_overwrite):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Your copy has local edits; updating would overwrite them. "
                    "Pass confirm_overwrite to replace the copy anyway."
                ),
            )
    elif lineage.listing_version_id == head.id:
        raise HTTPException(status_code=409, detail="Already on the latest version")

    if listing.kind == "skill":
        result = _update_skill_copy(db, actor, lineage, head)
    elif listing.kind == "agent":
        result = _update_agent_copy(db, actor, lineage, head)
    else:
        result = _update_workflow_copy(db, actor, lineage, head)

    lineage.listing_version_id = head.id
    # The recomputed baseline is the LOCAL copy's identity — for a workflow
    # that includes the spliced local trigger, so it is deliberately NOT the
    # published payload's hash. Divergence always means "the copy changed
    # since this moment", never "the copy differs from the payload".
    lineage.content_hash_at_install = (
        marketplace_service.local_content_hash(
            db,
            kind=lineage.target_kind,
            target_id=lineage.target_id,
            workspace_id=actor.workspace_id,
        )
        or ""
    )
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="listing.update",
        key=key,
        resource_id=result.resource_id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="listing.install_updated",
        resource_type="listing",
        resource_id=listing.id,
        detail={
            "slug": listing.slug,
            "kind": listing.kind,
            "resource_id": result.resource_id,
            "version": head.version,
        },
    )
    db.commit()
    return result


def _update_skill_copy(
    db: Session, actor: Actor, lineage: ListingInstall, head: ListingVersion
) -> InstallOut:
    try:
        snapshot = marketplace_service.SkillPayload.model_validate_json(
            head.payload_json
        )
    except ValueError:
        raise _payload_unreadable() from None
    skill = db.scalar(
        select(Skill).where(
            Skill.id == lineage.target_id, Skill.workspace_id == actor.workspace_id
        )
    )
    if skill is None:
        # The route's hash check saw it, but a concurrent delete can land
        # between the two reads — same answer as a copy that was never there.
        raise _copy_gone()
    # The local `name` and `shared` stay: the name is this workspace's handle
    # (possibly suffixed at install), and visibility is the installer's call.
    skills_service.update_skill(
        db,
        skill=skill,
        user_id=actor.user_id,
        title=snapshot.title,
        description=snapshot.description,
        body=snapshot.body,
        args=skills_service.parse_args(snapshot.args_json),
    )
    return InstallOut(
        kind="skill", resource_id=skill.id, name=skill.name, title=skill.title
    )


def _update_agent_copy(
    db: Session, actor: Actor, lineage: ListingInstall, head: ListingVersion
) -> InstallOut:
    try:
        snapshot = marketplace_service.AgentPayload.model_validate_json(
            head.payload_json
        )
    except ValueError:
        raise _payload_unreadable() from None
    agent = db.scalar(
        select(Agent).where(
            Agent.id == lineage.target_id, Agent.workspace_id == actor.workspace_id
        )
    )
    if agent is None:
        raise _copy_gone()

    warnings: list[str] = []
    if snapshot.unresolved_tools:
        warnings.append(
            "Not published with the listing (workspace-specific in the "
            "publisher's workspace): " + ", ".join(snapshot.unresolved_tools)
        )
    # The local grant survives — a new version can request more tools but never
    # receive them silently. Name the gap so granting stays a visible act.
    if snapshot.allowed_tools_json and agent.allowed_tools_json:
        try:
            requested = set(json.loads(snapshot.allowed_tools_json))
            granted = set(json.loads(agent.allowed_tools_json))
        except ValueError:
            requested, granted = set(), set()
        ungranted = sorted(requested - granted)
        if ungranted:
            warnings.append(
                "The new version asks for tools your copy does not grant "
                "(grant them in the agent editor if wanted): "
                + ", ".join(ungranted)
            )

    agent.name = snapshot.name
    agent.description = snapshot.description
    agent.instructions = snapshot.instructions
    db.flush()
    return InstallOut(
        kind="agent",
        resource_id=agent.id,
        name=agent.name,
        title=agent.name,
        warnings=warnings,
    )


def _update_workflow_copy(
    db: Session, actor: Actor, lineage: ListingInstall, head: ListingVersion
) -> InstallOut:
    try:
        snapshot = marketplace_service.WorkflowPayload.model_validate_json(
            head.payload_json
        )
        document = json.loads(snapshot.graph_json)
    except ValueError:
        raise _payload_unreadable() from None
    graph, parse_errors = parse_graph(document)
    if graph is None or parse_errors:
        raise _payload_unreadable()
    warnings = _workflow_landing_warnings(db, actor, graph)

    workflow = db.scalar(
        select(Workflow).where(
            Workflow.id == lineage.target_id,
            Workflow.workspace_id == actor.workspace_id,
        )
    )
    if workflow is None:
        raise _copy_gone()
    workflow.name = snapshot.name
    workflow.description = snapshot.description
    workflow.source_prompt = snapshot.source_prompt
    # The payload's graph carries the published (manual) trigger; the copy's
    # trigger is the installer's operational choice. Splice the local trigger
    # into the stored graph so the two places the product reads it from — the
    # graph (editor, recompile) and the columns (scheduler) — keep agreeing,
    # and an armed schedule survives the update instead of being disarmed by
    # the next graph save. A manual trigger is normalized to the canonical
    # shape (no leftover cron), and the splice is re-validated: the columns
    # are only ever written from a validated graph today, but a trigger the
    # grammar rejects must not be stored where the editor would choke on it.
    document["trigger"] = (
        dict(marketplace_service.MANUAL_TRIGGER)
        if workflow.trigger_kind == "manual"
        else {
            "kind": workflow.trigger_kind,
            "cron": workflow.schedule_cron,
            "timezone": workflow.schedule_timezone,
        }
    )
    respliced, splice_errors = parse_graph(document)
    if respliced is None or splice_errors:
        document["trigger"] = dict(marketplace_service.MANUAL_TRIGGER)
        workflow.trigger_kind = "manual"
        workflow.schedule_cron = ""
        workflow.schedule_timezone = "UTC"
        warnings.append(
            "Your copy's trigger could not be carried over — the updated "
            "workflow is back on a manual trigger; re-arm it deliberately."
        )
    workflow.graph_json = json.dumps(document, separators=(",", ":"), sort_keys=True)
    workflow.version += 1
    db.flush()
    return InstallOut(
        kind="workflow",
        resource_id=workflow.id,
        name=workflow.name,
        title=workflow.name,
        warnings=warnings,
    )


@router.post("/listings/{listing_id}/pin", response_model=ListingOut)
def pin_install(
    listing_id: str,
    payload: ListingPinBody,
    _: None = Depends(require_marketplace),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ListingOut:
    """Freeze (or unfreeze) this workspace's install at its current version.

    A pin is a statement about the *install*, not the listing: newer versions
    stop being offered (`install_state` reports "installed") until unpinned.
    Naturally idempotent — it sets a flag — so no Idempotency-Key dance.
    """
    listing = _load_visible(db, actor, listing_id)
    lineage = marketplace_service.find_install(
        db, workspace_id=actor.workspace_id, listing_id=listing.id
    )
    if lineage is None or (
        marketplace_service.local_content_hash(
            db,
            kind=lineage.target_kind,
            target_id=lineage.target_id,
            workspace_id=actor.workspace_id,
        )
        is None
    ):
        # A lineage row whose copy was deleted reads as "not installed"
        # everywhere else, so it is not pinnable either — the surfaces agree.
        raise HTTPException(
            status_code=404,
            detail="Nothing installed from this listing in this workspace",
        )
    lineage.pinned = payload.pinned
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="listing.pinned" if payload.pinned else "listing.unpinned",
        resource_type="listing",
        resource_id=listing.id,
        detail={"slug": listing.slug},
    )
    db.commit()
    return _out(db, listing, actor)
