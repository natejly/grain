"""Organization administration: the tier a workspace owner cannot reach into.

Everything in `api/admin.py` is gated by `require_owner`, and until this module
existed that was the top of the ladder — a workspace owner could set any policy
in their workspace and there was no authority above them to say otherwise. These
routes sit above that, gated by `require_org_admin`, and the relationship between
the two gates is the point of the whole tier:

*The gates compose in one direction only.* `require_org_admin` reads `org_role`
and never falls back to `role`, so being a workspace owner grants nothing here.
And no route in `api/admin.py` writes an `OrgMembership` row, so there is no
endpoint an owner can reach that would promote them — the inversion is closed by
there being no door, not by a check that could be forgotten.

*Reads are wider than writes, deliberately.* Any member of a workspace may read
the posture governing them (`GET /api/org`, `GET /api/org/policies`), because
being denied a tool by a rule you cannot see is how a control becomes folklore.
Only an org admin may change it, and `GET /api/org/members` — which enumerates
people and their standing, the same class of data `/api/admin/members` guards —
stays admin-only.

Every route is scoped to `actor.organization_id`. No route takes an organization
id, so there is no id to tamper with: the org you administer is the org governing
the workspace you are acting in, and reaching another one requires a membership
in it that the header check has already proved.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor, require_org_admin
from ..config import Settings, get_settings
from ..database import get_db
from ..models import (
    ORG_ADMIN,
    ORG_ROLES,
    Membership,
    Organization,
    OrgMembership,
    OrgToolPolicy,
    User,
    Workspace,
    new_id,
)
from ..schemas import ApiModel
from ..services import embedding_generations as generations
from ..services import orgs
from ..services.agent_loop import CHAT_SCOPE, WORKFLOW_SCOPE
from ..services.audit import record_audit

router = APIRouter(prefix="/api/org", tags=["org"])

_POLICIES = ("ask", "allow", "deny")
_SCOPES = (CHAT_SCOPE, WORKFLOW_SCOPE)

#: Refused rather than silently allowed, mirroring the workspace's last-owner
#: rule. An organization with no admin is permanently unconfigurable — its
#: posture freezes, and no one, including the workspace owners it governs, can
#: thaw it. That is a strictly worse outcome than refusing the demotion.
LAST_ADMIN_DETAIL = "An organization must keep at least one admin"


def _org(db: Session, actor: Actor) -> Organization:
    org = db.get(Organization, actor.organization_id) if actor.organization_id else None
    if org is None:
        # Unreachable while `Workspace.organization_id` is NOT NULL and every
        # insert path goes through the flush listener. A 404 rather than a 500
        # because the honest answer to "show me my org" when there is none is
        # that there is nothing there, not that the server broke.
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


# --------------------------------------------------------------------------
# Configuration


class OrgOut(ApiModel):
    id: str
    name: str
    #: None means *unbounded* — the org places no limit and the deployment's own
    #: list stands. An empty list means the org permits nothing, which is a very
    #: different statement, and collapsing the two onto `[]` is how an admin
    #: clearing a field would read as "no restriction" instead of "total".
    allowed_harnesses: Optional[List[str]]
    allowed_models: Optional[List[str]]
    #: What those bounds actually resolve to for the caller's workspace, after
    #: intersecting with what this deployment offers. An org may name a model the
    #: deployment does not have; the bound is not a grant, so it appears above
    #: and not here.
    effective_harnesses: List[str]
    effective_models: List[str]
    #: The caller's own standing: "admin", "member", or "" for someone governed
    #: by this org without belonging to it. The console renders read-only unless
    #: this is "admin"; the server does not trust that, it re-checks per route.
    your_role: str
    created_at: datetime


def _org_out(db: Session, actor: Actor, org: Organization, settings: Settings) -> OrgOut:
    return OrgOut(
        id=org.id,
        name=org.name,
        allowed_harnesses=orgs.decode_allow_list(org.allowed_harnesses_json),
        allowed_models=orgs.decode_allow_list(org.allowed_models_json),
        effective_harnesses=orgs.allowed_harnesses(
            db, workspace_id=actor.workspace_id, settings=settings
        ),
        effective_models=orgs.allowed_models(
            db, workspace_id=actor.workspace_id, settings=settings
        ),
        your_role=actor.org_role,
        created_at=org.created_at,
    )


@router.get("", response_model=OrgOut)
def get_org(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> OrgOut:
    """The organization governing this workspace, and what it permits.

    Readable by any member, not just an admin. A person whose tool call was
    denied is entitled to see the rule that denied it — an invisible policy is
    indistinguishable from a bug, and the resulting support ticket is worse for
    everyone than the disclosure, which is a list of model names and the caller's
    own role.
    """
    return _org_out(db, actor, _org(db, actor), settings)


class OrgConfigIn(ApiModel):
    name: Optional[str] = Field(default=None, max_length=160)
    #: Omit a field to leave it alone; send `null` to clear the bound entirely;
    #: send a list to set it. Three states, because "unbounded" and "bounded to
    #: nothing" are both things an admin may legitimately mean and PATCH must be
    #: able to say either without a second endpoint.
    allowed_harnesses: Optional[List[str]] = None
    allowed_models: Optional[List[str]] = None
    clear_harness_bound: bool = False
    clear_model_bound: bool = False


@router.patch("", response_model=OrgOut)
def update_org(
    payload: OrgConfigIn,
    actor: Actor = Depends(require_org_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> OrgOut:
    """Set the org's name and its harness/model bounds.

    Nothing here validates a name against the registry or the price list. An org
    may name a harness this deployment has not registered or a model it does not
    price, and that is deliberate: the bound is an intersection, so naming
    something absent permits nothing extra, and refusing it would mean an admin
    cannot prepare a posture for a model the deployment is about to add.
    """
    org = _org(db, actor)
    changed: Dict[str, Any] = {}
    if payload.name is not None and payload.name.strip():
        org.name = payload.name.strip()[:160]
        changed["name"] = org.name
    if payload.clear_harness_bound:
        org.allowed_harnesses_json = orgs.encode_allow_list(None)
        changed["allowed_harnesses"] = None
    elif payload.allowed_harnesses is not None:
        org.allowed_harnesses_json = orgs.encode_allow_list(payload.allowed_harnesses)
        changed["allowed_harnesses"] = payload.allowed_harnesses
    if payload.clear_model_bound:
        org.allowed_models_json = orgs.encode_allow_list(None)
        changed["allowed_models"] = None
    elif payload.allowed_models is not None:
        org.allowed_models_json = orgs.encode_allow_list(payload.allowed_models)
        changed["allowed_models"] = payload.allowed_models
    record_audit(
        db,
        # Audit events are workspace-scoped, and an org action has no single
        # workspace. It is filed against the one the admin was acting in, which
        # is where a reader would look for it and is a true statement about how
        # the change was made.
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="org.configured",
        resource_type="organization",
        resource_id=org.id,
        detail=changed,
    )
    db.commit()
    db.refresh(org)
    return _org_out(db, actor, org, settings)


# --------------------------------------------------------------------------
# Policies


class OrgPolicyOut(ApiModel):
    tool_name: str
    policy: str
    scope: str
    updated_at: datetime


class OrgPolicyIn(ApiModel):
    tool_name: str = Field(max_length=120)
    policy: str = Field(max_length=16)
    scope: str = Field(default=CHAT_SCOPE, max_length=16)


def _policy_out(row: OrgToolPolicy) -> OrgPolicyOut:
    return OrgPolicyOut(
        tool_name=row.tool_name,
        policy=row.policy,
        scope=row.scope,
        updated_at=row.updated_at,
    )


@router.get("/policies", response_model=List[OrgPolicyOut])
def list_org_policies(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[OrgPolicyOut]:
    """Every ceiling this org has set. Readable by anyone it governs."""
    rows = db.scalars(
        select(OrgToolPolicy)
        .where(OrgToolPolicy.organization_id == actor.organization_id)
        .order_by(OrgToolPolicy.tool_name, OrgToolPolicy.scope)
    )
    return [_policy_out(row) for row in rows]


@router.put("/policies", response_model=OrgPolicyOut)
def set_org_policy(
    payload: OrgPolicyIn,
    actor: Actor = Depends(require_org_admin),
    db: Session = Depends(get_db),
) -> OrgPolicyOut:
    """Set the org's ceiling for one tool in one scope.

    An upsert keyed on (org, tool, scope) — the same key the unique constraint
    carries — so setting a ceiling twice is not two rows fighting over a slot.
    """
    if payload.policy not in _POLICIES:
        raise HTTPException(
            status_code=422, detail=f"Policy must be one of: {', '.join(_POLICIES)}"
        )
    if payload.scope not in _SCOPES:
        raise HTTPException(
            status_code=422, detail=f"Scope must be one of: {', '.join(_SCOPES)}"
        )
    tool_name = payload.tool_name.strip()
    if not tool_name:
        raise HTTPException(status_code=422, detail="Tool name is required")
    row = db.scalar(
        select(OrgToolPolicy).where(
            OrgToolPolicy.organization_id == actor.organization_id,
            OrgToolPolicy.tool_name == tool_name,
            OrgToolPolicy.scope == payload.scope,
        )
    )
    if row is None:
        row = OrgToolPolicy(
            id=new_id(),
            organization_id=actor.organization_id,
            tool_name=tool_name,
            scope=payload.scope,
            created_by=actor.user_id,
        )
        db.add(row)
    row.policy = payload.policy
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="org.policy_set",
        resource_type="organization",
        resource_id=actor.organization_id,
        detail={"tool": tool_name, "policy": payload.policy, "scope": payload.scope},
    )
    db.commit()
    db.refresh(row)
    return _policy_out(row)


@router.delete("/policies/{scope}/{tool_name}", status_code=204)
def clear_org_policy(
    scope: str,
    tool_name: str,
    actor: Actor = Depends(require_org_admin),
    db: Session = Depends(get_db),
) -> Response:
    """Remove a ceiling. Absent a row the org constrains nothing for that tool."""
    row = db.scalar(
        select(OrgToolPolicy).where(
            OrgToolPolicy.organization_id == actor.organization_id,
            OrgToolPolicy.tool_name == tool_name,
            OrgToolPolicy.scope == scope,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    db.delete(row)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="org.policy_cleared",
        resource_type="organization",
        resource_id=actor.organization_id,
        detail={"tool": tool_name, "scope": scope},
    )
    db.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------
# Members


class OrgMemberOut(ApiModel):
    membership_id: str
    user_id: str
    name: str
    email: str
    role: str
    is_self: bool
    joined_at: datetime


def _member_out(row: OrgMembership, user: User, actor: Actor) -> OrgMemberOut:
    return OrgMemberOut(
        membership_id=row.id,
        user_id=user.id,
        name=user.name,
        email=user.email,
        role=row.role,
        is_self=user.id == actor.user_id,
        joined_at=row.created_at,
    )


@router.get("/members", response_model=List[OrgMemberOut])
def list_org_members(
    actor: Actor = Depends(require_org_admin),
    db: Session = Depends(get_db),
) -> List[OrgMemberOut]:
    """Who holds standing in this org. Admin-only, like `/api/admin/members`."""
    rows = db.execute(
        select(OrgMembership, User)
        .join(User, User.id == OrgMembership.user_id)
        .where(OrgMembership.organization_id == actor.organization_id)
        .order_by(OrgMembership.created_at, OrgMembership.id)
    ).all()
    return [_member_out(row, user, actor) for row, user in rows]


class OrgMemberRoleIn(ApiModel):
    user_id: str = Field(max_length=36)
    role: str = Field(max_length=24)


@router.put("/members", response_model=OrgMemberOut)
def set_org_member_role(
    payload: OrgMemberRoleIn,
    actor: Actor = Depends(require_org_admin),
    db: Session = Depends(get_db),
) -> OrgMemberOut:
    """Grant or change someone's standing in this org. Admin-only, by design.

    This is the endpoint that would break the tier if it were reachable from
    `require_owner`, so it is worth naming what stops that: it is not the check
    below, it is that this route exists only here. `api/admin.py` writes
    `Membership.role` and nothing else, so a workspace owner with no org standing
    has no path to this handler at all.

    The candidate must already be in one of the org's workspaces. An org admin
    can promote a colleague, not conjure standing for an arbitrary user id — and
    a `user_id` naming somebody outside the org's workspaces is a 404, which is
    also what makes this route useless as a directory of everyone on the
    deployment.
    """
    if payload.role not in ORG_ROLES:
        raise HTTPException(
            status_code=422, detail=f"Role must be one of: {', '.join(ORG_ROLES)}"
        )
    user = db.get(User, payload.user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    reachable = db.scalar(
        select(func.count())
        .select_from(Workspace)
        .join(Membership, Membership.workspace_id == Workspace.id)
        .where(
            Workspace.organization_id == actor.organization_id,
            Membership.user_id == user.id,
        )
    )
    if not reachable:
        raise HTTPException(status_code=404, detail="User not found")
    row = db.scalar(
        select(OrgMembership).where(
            OrgMembership.organization_id == actor.organization_id,
            OrgMembership.user_id == user.id,
        )
    )
    if row is None:
        row = OrgMembership(
            id=new_id(), organization_id=actor.organization_id, user_id=user.id
        )
        db.add(row)
    elif row.role == ORG_ADMIN and payload.role != ORG_ADMIN:
        others = db.scalar(
            select(func.count())
            .select_from(OrgMembership)
            .where(
                OrgMembership.organization_id == actor.organization_id,
                OrgMembership.role == ORG_ADMIN,
                OrgMembership.id != row.id,
            )
        )
        if not others:
            raise HTTPException(status_code=409, detail=LAST_ADMIN_DETAIL)
    row.role = payload.role
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="org.role_changed",
        resource_type="organization",
        resource_id=actor.organization_id,
        detail={"user_id": user.id, "to": payload.role},
    )
    db.commit()
    db.refresh(row)
    return _member_out(row, user, actor)


# --------------------------------------------------------------------------
# Retrieval contract


class EmbeddingCoverageOut(ApiModel):
    table: str
    covered: int
    pending: int
    unembedded: int


class EmbeddingGenerationOut(ApiModel):
    id: str
    model: str
    #: What the provider answered with, which for OpenAI is currently the same
    #: alias it was asked for. Empty on a generation backfilled by 0068, because
    #: nothing recorded it at the time and inventing it would be worse.
    revision: str
    dimensions: int
    storage_dtype: str
    normalization: str
    input_format: str
    #: The cosine below which this generation's vectors do not enter fusion.
    #: Carried per generation because it does not survive a change of width.
    dense_floor: float
    status: str
    note: str
    created_at: datetime
    activated_at: Optional[datetime]
    #: Only populated for the active generation — coverage is several counting
    #: queries per table, and running them for every retired generation would
    #: make this endpoint quadratic in a history nobody is reading.
    coverage: List[EmbeddingCoverageOut]


@router.get("/retrieval-contract", response_model=List[EmbeddingGenerationOut])
def list_embedding_generations(
    actor: Actor = Depends(require_org_admin),
    db: Session = Depends(get_db),
) -> List[EmbeddingGenerationOut]:
    """How this deployment embeds text, and how much of the corpus agrees.

    Read-only, and that is a decision rather than an omission. Building and
    activating a generation is an operator action with a corpus-wide blast
    radius, it is guarded by a coverage check that wants a human reading the
    numbers, and it belongs to the deployment rather than to any one org — so it
    lives in `scripts/rebuild_embeddings.py`, where it is reviewable, scriptable
    and logged. What an admin needs from a console is the answer to "is a
    migration in progress, and how far along is it", which is exactly what a
    `building` generation with a pending count is.

    Deployment-wide rows on an org-scoped router, which is worth being explicit
    about: there is no `organization_id` here to filter on, because two orgs
    disagreeing about what a vector means is not a state the system can hold. The
    gate is still `require_org_admin` — the rows describe model names, widths and
    corpus sizes, which is posture, not tenant data, and the same class of thing
    `GET /api/org` already returns.
    """
    active_id = getattr(generations.active_generation(db), "id", None)
    out: List[EmbeddingGenerationOut] = []
    for row in generations.list_generations(db):
        coverage = (
            generations.coverage(db, row).tables if row.id == active_id else []
        )
        out.append(
            EmbeddingGenerationOut(
                id=row.id,
                model=row.model,
                revision=row.revision,
                dimensions=row.dimensions,
                storage_dtype=row.storage_dtype,
                normalization=row.normalization,
                input_format=row.input_format,
                dense_floor=row.dense_floor,
                status=row.status,
                note=row.note,
                created_at=row.created_at,
                activated_at=row.activated_at,
                coverage=[
                    EmbeddingCoverageOut(
                        table=item.table,
                        covered=item.covered,
                        pending=item.pending,
                        unembedded=item.unembedded,
                    )
                    for item in coverage
                ],
            )
        )
    return out
