"""Workspace administration: who is in it, what it is doing, what it holds.

This is deliberately *workspace* admin and not an organisation console. Every
panel here answers a question `Actor.workspace_id` is sufficient to scope, and it
stays that way now that an Organization does exist: the org console is
`api/org.py`, gated by `require_org_admin`, and the two files do not overlap.

That separation is load-bearing rather than tidy. **No route in this file writes
an `OrgMembership` row**, which is what makes "a workspace owner cannot grant
themselves org powers" true by construction instead of by a check somebody could
relax. An owner is the top of the workspace; the org is above them; and the only
endpoints that move someone between those tiers are in the file gated on already
being there.

Two rules shape every route below.

*Owner only.* The read surface aggregates things an ordinary member has no reason
to enumerate — every member's email, the full audit trail, every sandbox anyone
has opened. The check lives in `auth.require_owner` (one dependency, already used
by integrations and generated_apps), so no route here re-implements it and none
can forget it: `require_owner` is what supplies the `Actor`, so a route that
skipped it would have nothing to scope by.

*Nothing here is a credential.* The panels describe MCP servers, sandboxes and
integrations, all of which hold secrets — `mcp_servers.secrets_encrypted`,
`mcp_oauth_tokens`, and the provider-side `sandbox_sessions.external_id` that
names a live machine. None of those columns are selected anywhere in this file;
the response models carry booleans and counts in their place ("has_secrets", not
the secret). `tests/test_admin.py::test_no_admin_route_returns_a_secret` asserts
it against the actual rows rather than against this comment.

Workspaces are not listed here. `GET /api/auth/workspaces` already returns the
caller's memberships with the current one flagged, and a second endpoint over the
same query would be a second place for the membership filter to be wrong.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple, cast

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response
from pydantic import Field
from sqlalchemy import ColumnElement, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import InstrumentedAttribute, Session

from ..auth import Actor, require_owner
from ..clock import utcnow
from ..config import Settings, get_settings
from ..database import get_db
from ..models import (
    Agent,
    AgentToolCall,
    AuditEvent,
    Chunk,
    GraphEdge,
    GraphEntity,
    McpServer,
    McpTool,
    Membership,
    MemoryItem,
    ModelUsage,
    Run,
    RunEvent,
    SandboxSession,
    Source,
    User,
    UserSession,
    WorkspaceBudget,
    WorkspaceInvite,
)
from ..schemas import ApiModel
from ..services import budget
from ..services.agent_loop import (
    PAUSED_FOR_BUDGET,
    WORKFLOW_SCOPE,
    policy_scope_for_run,
)
from ..services.audit import record_audit
from ..services.auth import email as email_service
from ..services.auth import invites
from ..services.runs import TERMINAL_RUN_STATES, resume_run_after_budget
from ..services.sandbox import session as sessions
from ..services.sandbox.types import SandboxError

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Free text that exists to be recognised at a glance in a list, not read. Runs
# carry whole prompts and approvals carry whole diffs; either would make a page
# of "recent activity" larger than the rest of the response put together.
PREVIEW_CHARS = 200

# The furthest back paging may reach. Audit trails grow without bound and OFFSET
# is a scan, so the depth is capped rather than the page size alone — an admin
# looking for something 10k events old wants search, which this is not.
MAX_AUDIT_OFFSET = 10_000


def _clip(text: str, limit: int = PREVIEW_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _detail(raw: str) -> Dict[str, Any]:
    """Audit detail as an object, whatever the column happens to hold.

    A single unparseable row must not take the whole page down: this is the
    screen someone opens *because* something went wrong.
    """
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _counts(
    db: Session,
    column: InstrumentedAttribute[str],
    *filters: ColumnElement[bool],
) -> Dict[str, int]:
    """GROUP BY on a low-cardinality status column — bounded by the enum, not the
    table, which is why these are the one aggregate here with no LIMIT."""
    rows = db.execute(
        select(column, func.count()).where(*filters).group_by(column)
    ).all()
    return {str(key): int(count) for key, count in rows}


def _total(db: Session, model: Any, *filters: ColumnElement[bool]) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*filters)) or 0)


# --------------------------------------------------------------------------
# Members


class AdminMemberOut(ApiModel):
    user_id: str
    membership_id: str
    name: str
    email: str
    role: str
    status: str
    joined_at: datetime
    # True for the caller's own row. Not a permission bit: an owner *may* demote
    # or remove themselves, and the rule that stops the workspace becoming
    # unreachable is "the last owner", not "yourself" — `count_owners` decides
    # that, server-side. This is here so the panel can say "you" and ask a
    # different question before somebody leaves than before they remove a
    # colleague, without matching ids client-side.
    is_self: bool


@router.get("/members", response_model=List[AdminMemberOut])
def list_members(
    limit: int = Query(default=100, ge=1, le=500),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> List[AdminMemberOut]:
    """Everyone in this workspace, oldest membership first.

    Ordered by join date to match `_resolve_workspace` and
    `GET /api/auth/workspaces`, so "the first member" means the same thing
    everywhere — in practice the owner who created the workspace.
    """
    rows = db.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.workspace_id == actor.workspace_id)
        .order_by(Membership.created_at, Membership.id)
        .limit(limit)
    ).all()
    return [
        AdminMemberOut(
            user_id=user.id,
            membership_id=membership.id,
            name=user.name,
            email=user.email,
            role=membership.role,
            status=user.status,
            joined_at=membership.created_at,
            is_self=user.id == actor.user_id,
        )
        for membership, user in rows
    ]


class AdminMemberRoleIn(ApiModel):
    role: str = Field(max_length=24)


@router.patch("/members/{membership_id}", response_model=AdminMemberOut)
def set_member_role(
    membership_id: str,
    payload: AdminMemberRoleIn,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> AdminMemberOut:
    """Promote a member, or demote an owner — never the last one.

    The membership is fetched with the workspace filter in the WHERE clause, so
    a membership id belonging to another workspace is a 404 and not a role
    change performed on somebody else's tenant.
    """
    membership, user = _member_row(db, membership_id, actor.workspace_id)
    role = _validated_role(payload.role)
    if membership.role == role:
        return _member_out(membership, user, actor)
    if membership.role == invites.ROLE_OWNER and not invites.count_owners(
        db, actor.workspace_id, excluding=membership.id
    ):
        raise HTTPException(status_code=409, detail=LAST_OWNER_DETAIL)
    previous = membership.role
    membership.role = role
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="membership.role_changed",
        resource_type="membership",
        resource_id=membership.id,
        detail={"user_id": user.id, "from": previous, "to": role},
    )
    db.commit()
    return _member_out(membership, user, actor)


@router.delete("/members/{membership_id}", status_code=204)
def remove_member(
    membership_id: str,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> Response:
    """Take somebody out of this workspace.

    Their sessions are deliberately left alone. A session is an identity, not a
    place: the person may hold memberships in other workspaces, and revoking
    their login here would sign them out of those too. Losing the membership is
    already total — `_resolve_workspace` refuses the very next request naming
    this workspace, whether it names it by header or falls back to it — and what
    they authored stays, since every row is keyed on the workspace rather than
    on a membership that has gone.
    """
    membership, user = _member_row(db, membership_id, actor.workspace_id)
    if membership.role == invites.ROLE_OWNER and not invites.count_owners(
        db, actor.workspace_id, excluding=membership.id
    ):
        raise HTTPException(status_code=409, detail=LAST_OWNER_DETAIL)
    db.delete(membership)
    # Any approval still routed to them goes back to "anyone", in the same
    # transaction: a parked call assigned to a departed member would otherwise
    # 409 every remaining reviewer while listing in nobody's actionable queue —
    # parked forever. Only *proposed* rows are touched; decided rows keep the
    # assignment as history.
    released = cast(
        "CursorResult[Any]",
        db.execute(
            update(AgentToolCall)
            .where(
                AgentToolCall.workspace_id == actor.workspace_id,
                AgentToolCall.assigned_to == user.id,
                AgentToolCall.status == "proposed",
            )
            .values(assigned_to="")
        ),
    ).rowcount
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="membership.removed",
        resource_type="membership",
        resource_id=membership.id,
        detail={
            "user_id": user.id,
            "role": membership.role,
            "assignments_released": int(released),
        },
    )
    db.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------
# Invitations
#
# The write half of membership. Everything here is owner-only for one reason
# beyond privacy: these are the routes that hand out roles, so a member who
# could reach them could make themselves an owner, and `require_owner` would
# then guard nothing. `tests/test_workspace_invites.py` pins a member's 403 on
# every one of them.


class AdminInviteOut(ApiModel):
    id: str
    email: str
    role: str
    #: pending | accepted | revoked | expired — see `services.auth.invites`.
    status: str
    invited_by: str
    invited_by_name: str
    expires_at: datetime
    created_at: datetime


class AdminInviteCreatedOut(ApiModel):
    """The 201 body, and the only place the raw link ever appears.

    Returned to the owner who just minted it, so they can deliver it themselves
    — which is also what makes the flow usable in development, where
    `EMAIL_SENDER=console` is the default and `_guard_auth` refuses that setting
    anywhere else. That needs no dev-only branch and grants nothing: the owner
    is already the person the mail is sent on behalf of, and can re-invite (and
    so rotate the link) at will.

    It is *only* here. `GET /api/admin/invites` never returns it, the database
    holds a SHA-256 of it, and nothing logs it.
    """

    invite: AdminInviteOut
    accept_url: str


class AdminInviteCreate(ApiModel):
    email: str = Field(min_length=3, max_length=320)
    role: str = Field(default=invites.ROLE_MEMBER, max_length=24)


LAST_OWNER_DETAIL = (
    "This is the workspace's last owner. Promote somebody else first — a "
    "workspace with no owner cannot be administered by anyone."
)


def _validated_role(raw: str) -> str:
    role = raw.strip().lower()
    if role not in invites.ROLES:
        # `Membership.role` is free text, so this is the only thing stopping an
        # "admin" role that no check in the codebase honours: a role the panel
        # displays but nothing enforces reads as a restriction that is not there.
        raise HTTPException(
            status_code=422, detail=f"Role must be one of: {', '.join(invites.ROLES)}"
        )
    return role


def _member_row(
    db: Session, membership_id: str, workspace_id: str
) -> Tuple[Membership, User]:
    row = db.execute(
        select(Membership, User)
        .join(User, User.id == Membership.user_id)
        .where(Membership.id == membership_id, Membership.workspace_id == workspace_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Member not found")
    membership, user = row
    return membership, user


def _member_out(membership: Membership, user: User, actor: Actor) -> AdminMemberOut:
    return AdminMemberOut(
        user_id=user.id,
        membership_id=membership.id,
        name=user.name,
        email=user.email,
        role=membership.role,
        status=user.status,
        joined_at=membership.created_at,
        is_self=user.id == actor.user_id,
    )


def _invite_out(invite: WorkspaceInvite, inviter_names: Dict[str, str]) -> AdminInviteOut:
    return AdminInviteOut(
        id=invite.id,
        email=invite.email,
        role=invite.role,
        status=invites.invite_status(invite),
        invited_by=invite.invited_by,
        invited_by_name=inviter_names.get(invite.invited_by, ""),
        expires_at=invite.expires_at,
        created_at=invite.created_at,
    )


def _inviter_names(db: Session, rows: Sequence[WorkspaceInvite]) -> Dict[str, str]:
    ids = {invite.invited_by for invite in rows if invite.invited_by}
    if not ids:
        return {}
    return {
        user.id: user.name
        for user in db.scalars(select(User).where(User.id.in_(ids))).all()
    }


@router.get("/invites", response_model=List[AdminInviteOut])
def list_invites(
    limit: int = Query(default=100, ge=1, le=500),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> List[AdminInviteOut]:
    """Every invitation this workspace has issued, newest first.

    Spent and withdrawn ones are included rather than filtered: "we invited that
    address and it was never used" is the question this list is usually opened
    to answer. No token, hashed or otherwise, appears in the response.
    """
    rows = list(
        db.scalars(
            select(WorkspaceInvite)
            .where(WorkspaceInvite.workspace_id == actor.workspace_id)
            .order_by(WorkspaceInvite.created_at.desc(), WorkspaceInvite.id)
            .limit(limit)
        ).all()
    )
    names = _inviter_names(db, rows)
    return [_invite_out(invite, names) for invite in rows]


@router.post("/invites", response_model=AdminInviteCreatedOut, status_code=201)
def create_invite(
    payload: AdminInviteCreate,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AdminInviteCreatedOut:
    """Invite an address into this workspace.

    Refuses an address that is already in: "invited" and "member" are different
    states and an owner who cannot tell them apart will keep sending links to
    somebody sitting next to them. Changing what an existing member can do is
    `PATCH /api/admin/members/{id}`, which says so in the audit log.
    """
    email = email_service.normalize_email(payload.email)
    if not email_service.looks_like_email(email):
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    role = _validated_role(payload.role)

    already = db.scalar(
        select(Membership)
        .join(User, User.id == Membership.user_id)
        .where(Membership.workspace_id == actor.workspace_id, User.email == email)
    )
    if already is not None:
        raise HTTPException(
            status_code=409, detail="That address is already a member of this workspace"
        )

    invite, raw_token = invites.issue_invite(
        db,
        workspace_id=actor.workspace_id,
        email=email,
        role=role,
        invited_by=actor.user_id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="invite.created",
        resource_type="workspace_invite",
        resource_id=invite.id,
        # The address and the role, never the token — an audit trail is read by
        # more people, and kept for longer, than any other table here.
        detail={"email": email, "role": role},
    )
    db.commit()
    # After the commit, and best-effort: an invitation that exists but whose
    # mail bounced is recoverable (the link is in the response body). One that
    # was mailed and then rolled back is a live credential for a row that is not
    # there.
    email_service.send_quietly(
        email_service.get_email_sender(settings),
        invites.invite_email(
            settings,
            to=email,
            workspace_name=actor.workspace_name,
            inviter_name=actor.user_name,
            raw_token=raw_token,
        ),
    )
    return AdminInviteCreatedOut(
        invite=_invite_out(invite, {actor.user_id: actor.user_name}),
        accept_url=invites.invite_url(settings, raw_token),
    )


@router.delete("/invites/{invite_id}", response_model=AdminInviteOut)
def revoke_invite(
    invite_id: str,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> AdminInviteOut:
    """Stop a link working. Answers with the row so the panel can restate it."""
    invite = db.scalar(
        select(WorkspaceInvite).where(
            WorkspaceInvite.id == invite_id,
            WorkspaceInvite.workspace_id == actor.workspace_id,
        )
    )
    if invite is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if not invites.revoke_invite(db, invite):
        # Already spent or already withdrawn. Neither is an error worth an owner
        # reading a stack of red text over, but it is not a revocation either.
        raise HTTPException(
            status_code=409, detail="That invitation is no longer pending"
        )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="invite.revoked",
        resource_type="workspace_invite",
        resource_id=invite.id,
        detail={"email": invite.email, "role": invite.role},
    )
    db.commit()
    db.refresh(invite)
    return _invite_out(invite, _inviter_names(db, [invite]))


# --------------------------------------------------------------------------
# Audit log


class AdminAuditEntryOut(ApiModel):
    id: str
    action: str
    resource_type: str
    resource_id: str
    detail: Dict[str, Any]
    created_at: datetime
    actor_id: str
    # Empty when the actor is a background worker, or a user who has since been
    # deleted; the id is kept either way so the trail is still followable.
    actor_name: str
    actor_email: str


class AdminAuditPage(ApiModel):
    entries: List[AdminAuditEntryOut]
    total: int
    limit: int
    offset: int
    # Whether another page exists, computed from `total` so the client does not
    # have to fetch an empty page to find out.
    has_more: bool


@router.get("/audit-events", response_model=AdminAuditPage)
def list_audit_log(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=MAX_AUDIT_OFFSET),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> AdminAuditPage:
    """The workspace's audit trail, newest first.

    Richer than `GET /api/audit-events`, which is the activity feed: this one
    pages and resolves the actor to a person. The join is an outer join on
    purpose — `audit_events.actor_id` is a plain column, not a foreign key,
    because a worker writes rows with no user behind them.
    """
    total = _total(db, AuditEvent, AuditEvent.workspace_id == actor.workspace_id)
    rows = db.execute(
        select(AuditEvent, User)
        .outerjoin(User, User.id == AuditEvent.actor_id)
        .where(AuditEvent.workspace_id == actor.workspace_id)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    entries = [
        AdminAuditEntryOut(
            id=event.id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            detail=_detail(event.detail_json),
            created_at=event.created_at,
            actor_id=event.actor_id,
            actor_name=user.name if user is not None else "",
            actor_email=user.email if user is not None else "",
        )
        for event, user in rows
    ]
    return AdminAuditPage(
        entries=entries,
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(entries) < total,
    )


class AdminAuditExportEntryOut(ApiModel):
    """One raw trail row for an external ingester. No user join on purpose:
    an exporter pulling thousands of rows resolves actors once from the
    members list, not once per row."""

    id: str
    action: str
    resource_type: str
    resource_id: str
    actor_id: str
    detail: Dict[str, Any]
    created_at: datetime


class AdminAuditExportPage(ApiModel):
    events: List[AdminAuditExportEntryOut]
    #: Pass back to continue exactly where this page ended; null when the
    #: trail is drained. Stable under concurrent writes: keyset, not offset.
    next_cursor: Optional[str]


def _encode_audit_cursor(created_at: datetime, event_id: str) -> str:
    import base64

    raw = f"{created_at.isoformat()}|{event_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_audit_cursor(cursor: str) -> Tuple[datetime, str]:
    import base64

    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        stamp, _, event_id = raw.partition("|")
        if not event_id:
            raise ValueError
        return datetime.fromisoformat(stamp), event_id
    except (ValueError, UnicodeDecodeError) as error:
        raise HTTPException(
            status_code=422, detail="The export cursor is not one this API issued"
        ) from error


@router.get("/audit-events/export", response_model=AdminAuditExportPage)
def export_audit_log(
    cursor: Optional[str] = Query(default=None, max_length=200),
    limit: int = Query(default=500, ge=1, le=1000),
    since: Optional[datetime] = Query(default=None),
    action: Optional[str] = Query(default=None, max_length=100),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> AdminAuditExportPage:
    """The whole trail, oldest first, for SIEM ingestion and archival.

    The UI page above is capped-offset and newest-first — right for reading,
    structurally unable to drain a long trail. This one walks forward on a
    keyset cursor over (created_at, id): no offset cap, stable under
    concurrent writes (a row inserted behind the cursor is never skipped and
    never repeated), and `since` makes incremental pulls cheap. `action`
    filters by prefix, matching how the actions namespace themselves
    ("agent_tool.", "skill.", "run."). The keyset predicate is spelled as the
    two-clause OR rather than a row-value comparison so it runs identically
    on SQLite and Postgres.
    """
    conditions: List[ColumnElement[bool]] = [
        AuditEvent.workspace_id == actor.workspace_id
    ]
    if since is not None:
        conditions.append(AuditEvent.created_at >= since)
    if action:
        conditions.append(AuditEvent.action.like(f"{action}%"))
    if cursor:
        after_at, after_id = _decode_audit_cursor(cursor)
        conditions.append(
            (AuditEvent.created_at > after_at)
            | ((AuditEvent.created_at == after_at) & (AuditEvent.id > after_id))
        )
    rows = list(
        db.scalars(
            select(AuditEvent)
            .where(*conditions)
            .order_by(AuditEvent.created_at.asc(), AuditEvent.id.asc())
            .limit(limit + 1)
        )
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    events = [
        AdminAuditExportEntryOut(
            id=event.id,
            action=event.action,
            resource_type=event.resource_type,
            resource_id=event.resource_id,
            actor_id=event.actor_id,
            detail=_detail(event.detail_json),
            created_at=event.created_at,
        )
        for event in rows
    ]
    return AdminAuditExportPage(
        events=events,
        next_cursor=(
            _encode_audit_cursor(rows[-1].created_at, rows[-1].id)
            if has_more and rows
            else None
        ),
    )


# --------------------------------------------------------------------------
# Runs and approvals


class AdminRunOut(ApiModel):
    id: str
    conversation_id: str
    agent_id: str
    created_by: str
    status: str
    prompt_preview: str
    error: str
    created_at: datetime
    updated_at: datetime


class AdminApprovalOut(ApiModel):
    id: str
    run_id: str
    name: str
    status: str
    proposal_preview: str
    created_at: datetime


class AdminActivityOut(ApiModel):
    run_status_counts: Dict[str, int]
    tool_call_status_counts: Dict[str, int]
    recent_runs: List[AdminRunOut]
    # Only the calls actually parked waiting on a human — the queue an owner is
    # meant to act on, not a history of everything ever approved.
    pending_approvals: List[AdminApprovalOut]


@router.get("/activity", response_model=AdminActivityOut)
def get_activity(
    limit: int = Query(default=20, ge=1, le=100),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> AdminActivityOut:
    """What the agents are doing: the status breakdown, plus the head of each list.

    One route rather than three because the panel renders them together and the
    counts are meaningless next to a stale list of runs.
    """
    runs = list(
        db.scalars(
            select(Run)
            .where(Run.workspace_id == actor.workspace_id)
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(limit)
        )
    )
    approvals = list(
        db.scalars(
            select(AgentToolCall)
            .where(
                AgentToolCall.workspace_id == actor.workspace_id,
                AgentToolCall.status == "proposed",
            )
            .order_by(AgentToolCall.created_at.desc(), AgentToolCall.id.desc())
            .limit(limit)
        )
    )
    return AdminActivityOut(
        run_status_counts=_counts(
            db, Run.status, Run.workspace_id == actor.workspace_id
        ),
        tool_call_status_counts=_counts(
            db, AgentToolCall.status, AgentToolCall.workspace_id == actor.workspace_id
        ),
        recent_runs=[
            AdminRunOut(
                id=run.id,
                conversation_id=run.conversation_id,
                agent_id=run.agent_id,
                created_by=run.created_by,
                status=run.status,
                prompt_preview=_clip(run.prompt),
                error=run.error,
                created_at=run.created_at,
                updated_at=run.updated_at,
            )
            for run in runs
        ],
        pending_approvals=[
            AdminApprovalOut(
                id=call.id,
                run_id=call.run_id,
                name=call.name,
                status=call.status,
                proposal_preview=_clip(call.proposal_preview),
                created_at=call.created_at,
            )
            for call in approvals
        ],
    )


# --------------------------------------------------------------------------
# Sandbox sessions


class AdminSandboxSessionOut(ApiModel):
    """A sandbox as an administrator sees it.

    `external_id` is absent by construction, not by filtering. It is the
    provider's own name for a live machine, and ADR 0005's threat model is that
    it never appears in an API response — a leaked body would otherwise be enough to
    address someone's sandbox directly, bypassing this server entirely.
    """

    id: str
    project_id: str
    label: str
    provider: str
    status: str
    network_policy: str
    exec_count: int
    wall_ms_used: int
    error: str
    created_at: datetime
    last_used_at: datetime
    killed_at: Optional[datetime]


def _sandbox_out(session: SandboxSession) -> AdminSandboxSessionOut:
    return AdminSandboxSessionOut(
        id=session.id,
        project_id=session.project_id,
        label=session.label,
        provider=session.provider,
        status=session.status,
        network_policy=session.network_policy,
        exec_count=session.exec_count,
        wall_ms_used=session.wall_ms_used,
        error=session.error,
        created_at=session.created_at,
        last_used_at=session.last_used_at,
        killed_at=session.killed_at,
    )


@router.get("/sandbox-sessions", response_model=List[AdminSandboxSessionOut])
def list_sandbox_sessions(
    limit: int = Query(default=50, ge=1, le=200),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> List[AdminSandboxSessionOut]:
    """Every sandbox this workspace has opened, most recently used first.

    A bounded select rather than `sessions.list_sessions`, which returns the
    whole table: that is right for the quota accounting it serves and wrong for
    a page. Reading rows is not the tenancy boundary — turning an *id* into a row
    is, and that only happens in `resolve_session` (see the DELETE below).
    """
    rows = list(
        db.scalars(
            select(SandboxSession)
            .where(SandboxSession.workspace_id == actor.workspace_id)
            .order_by(
                SandboxSession.last_used_at.desc(), SandboxSession.created_at.desc()
            )
            .limit(limit)
        )
    )
    return [_sandbox_out(session) for session in rows]


@router.delete("/sandbox-sessions/{session_id}", response_model=AdminSandboxSessionOut)
def kill_sandbox_session(
    session_id: str,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AdminSandboxSessionOut:
    """Destroy a machine an owner does not want running.

    The id is resolved by `sessions.resolve_session` and by nothing else, which
    is what makes a foreign id indistinguishable from a missing one: both raise
    `SandboxError` and both answer 404. Resolving here *before* calling
    `kill_session` — which resolves again — is not redundant; without it the
    service layer's error would be mapped by the generic handler below and a
    stranger's probe would come back 502, which is an oracle.
    """
    try:
        sessions.resolve_session(
            db, workspace_id=actor.workspace_id, session_id=session_id
        )
    except SandboxError as exc:
        raise HTTPException(status_code=404, detail="Sandbox session not found") from exc
    try:
        killed = sessions.kill_session(
            db,
            workspace_id=actor.workspace_id,
            session_id=session_id,
            settings=settings,
        )
    except SandboxError as exc:
        # The provider failed, not the caller. The service layer's message is
        # already scrubbed of URLs and keys before it gets here.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="sandbox_session.killed",
        resource_type="sandbox_session",
        resource_id=killed.id,
        detail={"via": "admin", "exec_count": killed.exec_count},
    )
    db.commit()
    return _sandbox_out(killed)


# --------------------------------------------------------------------------
# MCP servers


class AdminMcpServerOut(ApiModel):
    """Health of a configured MCP server. Carries no part of its credentials:
    `secrets_encrypted` is never selected and `mcp_oauth_tokens` is never read."""

    id: str
    name: str
    transport: str
    enabled: bool
    status: str
    last_error: str
    last_connected_at: Optional[datetime]
    # Whether env/headers are stored, never the values — mirrors McpServerOut.
    has_secrets: bool
    tool_count: int
    created_at: datetime


@router.get("/mcp-servers", response_model=List[AdminMcpServerOut])
def list_mcp_servers(
    limit: int = Query(default=50, ge=1, le=200),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> List[AdminMcpServerOut]:
    """Configured servers and whether they are actually connecting.

    Tool counts come from one grouped query rather than a per-server COUNT, so
    the route stays two statements regardless of how many servers exist.
    """
    rows = list(
        db.scalars(
            select(McpServer)
            .where(McpServer.workspace_id == actor.workspace_id)
            .order_by(McpServer.name)
            .limit(limit)
        )
    )
    tool_counts: Dict[str, int] = {
        str(server_id): int(count)
        for server_id, count in db.execute(
            select(McpTool.server_id, func.count())
            .where(McpTool.workspace_id == actor.workspace_id)
            .group_by(McpTool.server_id)
        ).all()
    }
    return [
        AdminMcpServerOut(
            id=server.id,
            name=server.name,
            transport=server.transport,
            enabled=server.enabled,
            status=server.status,
            last_error=server.last_error,
            last_connected_at=server.last_connected_at,
            has_secrets=bool(server.secrets_encrypted),
            tool_count=tool_counts.get(server.id, 0),
            created_at=server.created_at,
        )
        for server in rows
    ]


# --------------------------------------------------------------------------
# Storage and indexing


class AdminStorageOut(ApiModel):
    """Counts only. Every figure is an aggregate over the caller's workspace, so
    this route's cost does not grow with the size of the workspace."""

    # Live sources by ingestion status; soft-deleted rows are excluded, matching
    # what GET /api/sources will show.
    sources_by_status: Dict[str, int]
    source_count: int
    source_bytes: int
    chunk_count: int
    memory_by_status: Dict[str, int]
    memory_item_count: int
    graph_entity_count: int
    graph_edge_count: int


@router.get("/storage", response_model=AdminStorageOut)
def get_storage(
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> AdminStorageOut:
    """How much this workspace is holding, and how much of it is indexed."""
    workspace = actor.workspace_id
    live_sources = (Source.workspace_id == workspace, Source.deleted_at.is_(None))
    sources_by_status = _counts(db, Source.status, *live_sources)
    memory_by_status = _counts(
        db, MemoryItem.status, MemoryItem.workspace_id == workspace
    )
    total_bytes = db.scalar(
        select(func.coalesce(func.sum(Source.byte_size), 0)).where(*live_sources)
    )
    return AdminStorageOut(
        sources_by_status=sources_by_status,
        # Summed from the breakdown rather than counted again: one query, and the
        # two figures cannot disagree if a row lands between them.
        source_count=sum(sources_by_status.values()),
        source_bytes=int(total_bytes or 0),
        chunk_count=_total(db, Chunk, Chunk.workspace_id == workspace),
        memory_by_status=memory_by_status,
        memory_item_count=sum(memory_by_status.values()),
        graph_entity_count=_total(db, GraphEntity, GraphEntity.workspace_id == workspace),
        graph_edge_count=_total(db, GraphEdge, GraphEdge.workspace_id == workspace),
    )


# --------------------------------------------------------------------------
# Model usage and cost
#
# The one panel here that is about money. Three things shape it.
#
# *Every figure is bounded by a window.* `model_usage` grows with every model
# call the workspace makes, which is by far the fastest-growing table in the
# schema; an all-time aggregate would turn this route into a full scan that gets
# slower every day it is looked at.
#
# *Tokens and cost are reported separately, not merged.* A row whose model had
# no configured rate contributes its tokens and no cost, and is counted in
# `unpriced_calls` rather than folded in as zero. Summing nulls as zero is how a
# dashboard ends up quietly under-reporting a bill; `unpriced_models` names the
# models that would need a rate in MODEL_PRICES for the number to be complete.
#
# *No content, because the table holds none.* Everything below is counts,
# identifiers and money.


# A year is long enough for "what did we spend last quarter" and short enough
# that the index still bounds the scan.
MAX_USAGE_WINDOW_DAYS = 365
# Breakdown rows returned per axis. Models and operations are bounded by their
# own cardinality; users are bounded by the workspace, which is not, so the cut
# is applied in SQL and the heaviest spenders are the ones kept.
USAGE_GROUP_LIMIT = 25
TOP_RUNS_LIMIT = 10


class AdminUsageTotalsOut(ApiModel):
    calls: int
    input_tokens: int
    # A subset of input_tokens, reported by the provider and billed at its own
    # rate — not an extra amount to add on.
    cached_input_tokens: int
    output_tokens: int
    # Likewise a subset of output_tokens.
    reasoning_tokens: int
    total_tokens: int
    # Summed over priced rows only. Read it next to `unpriced_calls`: a large
    # unpriced count means this figure is a floor, not the bill.
    cost_usd: float
    priced_calls: int
    unpriced_calls: int


class AdminUsageGroupOut(ApiModel):
    """One row of a breakdown: a model, a user, or an operation."""

    key: str
    # The same string for models and operations; a name or email for a user, so
    # the panel does not have to join ids back to people itself.
    label: str
    calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float
    unpriced_calls: int


class AdminUsageRunOut(ApiModel):
    """One run's spend. The runaway-loop question, asked in money."""

    run_id: str
    conversation_id: str
    calls: int
    total_tokens: int
    cost_usd: float
    unpriced_calls: int
    last_call_at: datetime


class AdminUsageOut(ApiModel):
    window_days: int
    since: datetime
    totals: AdminUsageTotalsOut
    by_model: List[AdminUsageGroupOut]
    by_user: List[AdminUsageGroupOut]
    by_operation: List[AdminUsageGroupOut]
    # Which agent's turns spent it. A row keyed "" is the background work no
    # agent ran (embeddings, ingest, compiles); a deleted agent keeps its id as
    # the key with no label, because its spend already happened.
    by_agent: List[AdminUsageGroupOut]
    # Ordered by cost, then tokens — so a run that burned tokens on an unpriced
    # model still surfaces rather than sorting to the bottom on a null cost.
    top_runs: List[AdminUsageRunOut]
    # Models seen in this window with no rate in MODEL_PRICES. Their tokens are
    # in every count above and their cost is in none of them.
    unpriced_models: List[str]
    # False when MODEL_PRICES is empty, i.e. no cost figure here can be anything
    # but zero. Stated explicitly so a panel can say "not configured" instead of
    # showing a confident $0.00.
    pricing_configured: bool


# Selected in this order by every aggregate below, so one row builder can read
# them positionally.
_USAGE_SUMS = (
    func.count(),
    func.coalesce(func.sum(ModelUsage.input_tokens), 0),
    func.coalesce(func.sum(ModelUsage.cached_input_tokens), 0),
    func.coalesce(func.sum(ModelUsage.output_tokens), 0),
    func.coalesce(func.sum(ModelUsage.reasoning_tokens), 0),
    func.coalesce(func.sum(ModelUsage.total_tokens), 0),
    func.coalesce(func.sum(ModelUsage.cost_usd), 0.0),
    # COUNT ignores nulls, so this counts the rows that *have* a cost; the
    # unpriced remainder is the difference. Doing it this way rather than with a
    # CASE keeps it one portable expression on both backends.
    func.count(ModelUsage.cost_usd),
)


def _usage_window(
    workspace_id: str, days: int
) -> Tuple[datetime, Tuple[ColumnElement[bool], ...]]:
    since = utcnow() - timedelta(days=days)
    return since, (
        ModelUsage.workspace_id == workspace_id,
        ModelUsage.created_at >= since,
    )


def _usage_groups(
    db: Session,
    column: InstrumentedAttribute[str],
    *filters: ColumnElement[bool],
    limit: int = USAGE_GROUP_LIMIT,
) -> List[Tuple[str, Sequence[Any]]]:
    """(key, aggregate row) for one breakdown axis, biggest spender first.

    Sorted on cost first and tokens second so an unpriced model — cost 0 by
    arithmetic, not by fact — is still ranked by the one measurement it does
    have, instead of being pushed off the end of the list by the cut below.
    """
    rows = db.execute(
        select(column, *_USAGE_SUMS)
        .where(*filters)
        .group_by(column)
        .order_by(
            func.coalesce(func.sum(ModelUsage.cost_usd), 0.0).desc(),
            func.coalesce(func.sum(ModelUsage.total_tokens), 0).desc(),
            column,
        )
        .limit(limit)
    ).all()
    return [(str(row[0]), row[1:]) for row in rows]


def _group_out(key: str, label: str, sums: Sequence[Any]) -> AdminUsageGroupOut:
    calls, input_tokens, _cached, output_tokens, _reasoning, total, cost, priced = sums
    return AdminUsageGroupOut(
        key=key,
        label=label or key,
        calls=int(calls),
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        total_tokens=int(total),
        cost_usd=float(cost or 0.0),
        unpriced_calls=int(calls) - int(priced),
    )


@router.get("/usage", response_model=AdminUsageOut)
def get_usage(
    days: int = Query(default=30, ge=1, le=MAX_USAGE_WINDOW_DAYS),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AdminUsageOut:
    """What this workspace spent on models, and on what.

    Five aggregates over one indexed window: the totals, three breakdowns, and
    the costliest runs. Each is a single GROUP BY, so the route's cost tracks the
    window rather than the size of the workspace.
    """
    since, window = _usage_window(actor.workspace_id, days)

    totals_row = db.execute(select(*_USAGE_SUMS).where(*window)).one()
    calls, inputs, cached, outputs, reasoning, total, cost, priced = totals_row
    totals = AdminUsageTotalsOut(
        calls=int(calls),
        input_tokens=int(inputs),
        cached_input_tokens=int(cached),
        output_tokens=int(outputs),
        reasoning_tokens=int(reasoning),
        total_tokens=int(total),
        cost_usd=float(cost or 0.0),
        priced_calls=int(priced),
        unpriced_calls=int(calls) - int(priced),
    )

    by_model = [
        _group_out(key, key, sums)
        for key, sums in _usage_groups(db, ModelUsage.model, *window)
    ]
    by_operation = [
        _group_out(key, key, sums)
        for key, sums in _usage_groups(db, ModelUsage.operation, *window)
    ]

    user_groups = _usage_groups(db, ModelUsage.user_id, *window)
    # One lookup for the whole page, and scoped to this workspace's membership:
    # a stale user id from a former member resolves to no name rather than to
    # someone else's, and never to a person outside the workspace.
    names: Dict[str, str] = {
        str(user_id): (name or email)
        for user_id, name, email in db.execute(
            select(User.id, User.name, User.email)
            .join(Membership, Membership.user_id == User.id)
            .where(
                Membership.workspace_id == actor.workspace_id,
                User.id.in_([key for key, _ in user_groups] or [""]),
            )
        ).all()
    }
    by_user = [
        _group_out(key, names.get(key, ""), sums) for key, sums in user_groups
    ]

    agent_groups = _usage_groups(db, ModelUsage.agent_id, *window)
    # Same one-lookup shape as the user names, and scoped the same way: only
    # this workspace's agents can label a row, so a stale or foreign agent id
    # stays an id. A deleted agent's spend still happened — the panel shows
    # the id rather than dropping the row.
    agent_names: Dict[str, str] = {
        str(agent_id): name
        for agent_id, name in db.execute(
            select(Agent.id, Agent.name).where(
                Agent.workspace_id == actor.workspace_id,
                Agent.id.in_([key for key, _ in agent_groups] or [""]),
            )
        ).all()
    }
    by_agent = [
        _group_out(key, agent_names.get(key, ""), sums) for key, sums in agent_groups
    ]

    run_rows = db.execute(
        select(
            ModelUsage.run_id,
            # Any conversation id on the run's rows; they all carry the same one.
            func.max(ModelUsage.conversation_id),
            func.count(),
            func.coalesce(func.sum(ModelUsage.total_tokens), 0),
            func.coalesce(func.sum(ModelUsage.cost_usd), 0.0),
            func.count(ModelUsage.cost_usd),
            func.max(ModelUsage.created_at),
        )
        .where(*window, ModelUsage.run_id != "")
        .group_by(ModelUsage.run_id)
        .order_by(
            func.coalesce(func.sum(ModelUsage.cost_usd), 0.0).desc(),
            func.coalesce(func.sum(ModelUsage.total_tokens), 0).desc(),
            ModelUsage.run_id,
        )
        .limit(TOP_RUNS_LIMIT)
    ).all()
    top_runs = [
        AdminUsageRunOut(
            run_id=str(run_id),
            conversation_id=str(conversation_id or ""),
            calls=int(run_calls),
            total_tokens=int(run_tokens),
            cost_usd=float(run_cost or 0.0),
            unpriced_calls=int(run_calls) - int(run_priced),
            last_call_at=last_at,
        )
        for run_id, conversation_id, run_calls, run_tokens, run_cost, run_priced, last_at
        in run_rows
    ]

    unpriced = db.scalars(
        select(ModelUsage.model)
        .where(*window, ModelUsage.cost_usd.is_(None))
        .group_by(ModelUsage.model)
        .order_by(func.count().desc(), ModelUsage.model)
        .limit(USAGE_GROUP_LIMIT)
    ).all()

    return AdminUsageOut(
        window_days=days,
        since=since,
        totals=totals,
        by_model=by_model,
        by_user=by_user,
        by_operation=by_operation,
        by_agent=by_agent,
        top_runs=top_runs,
        unpriced_models=[str(model) for model in unpriced],
        pricing_configured=bool(settings.model_prices),
    )


# --------------------------------------------------------------------------
# The spend ceiling (ADR 0008)
#
# `/usage` answers "what did this cost"; these two answer "what may it cost".
# The panel they serve has to make three things visible at once, because an
# owner looking at it is usually looking because something stopped:
#
# *The limit and the spend, side by side.* A ceiling with no current figure
# beside it cannot be raised by the right amount.
#
# *Whether the limit can actually see the spend.* `unpriced_calls` is the number
# that decides whether a dollar ceiling means anything here, and it is reported
# next to the ceiling rather than one page away in /usage.
#
# *What is parked on it right now.* Raising a limit that releases nothing is
# indistinguishable from raising one that releases six automations, and the
# owner should not have to guess which they just did.


# Bounds both the listing and the release. A workspace that has parked more runs
# than this has a runaway the ceiling is doing its job on, and resuming a
# thousand turns from one HTTP request would be its own outage — the remainder
# stay parked and a second PUT takes the next batch.
MAX_PARKED_RUNS = 50


class AdminBudgetCeilingOut(ApiModel):
    """One ceiling. Nulls mean no limit of that kind, never zero."""

    window_hours: int
    usd_per_window: Optional[float]
    tokens_per_window: Optional[int]
    # workspace — an owner set it here; settings — the deployment configured it.
    source: str


class AdminBudgetSpendOut(ApiModel):
    calls: int
    cost_usd: float
    total_tokens: int
    # Calls whose model had no configured rate. While this is above zero,
    # `cost_usd` is a floor rather than the bill, and a USD ceiling alone cannot
    # bound this workspace — see `budget.exceeds`.
    unpriced_calls: int


class AdminBudgetParkedRunOut(ApiModel):
    run_id: str
    conversation_id: str
    created_at: datetime


class AdminBudgetOut(ApiModel):
    ceiling: AdminBudgetCeilingOut
    # The same ceiling scaled by UNATTENDED_BUDGET_FRACTION — what a workflow
    # node may spend, measured over unattended spend alone.
    unattended_ceiling: AdminBudgetCeilingOut
    spend: AdminBudgetSpendOut
    unattended_spend: AdminBudgetSpendOut
    # False when neither limit is set: this workspace has no ceiling at all.
    enforced: bool
    pricing_configured: bool
    runs_parked_on_budget: List[AdminBudgetParkedRunOut]
    # Populated by PUT only: runs the new ceiling released. Empty on GET.
    resumed_run_ids: List[str] = []


class AdminBudgetRequest(ApiModel):
    """Replace this workspace's ceiling.

    Replace, not patch. An omitted field is *no limit of that kind*, so the body
    always states the whole ceiling and there is no way to raise a limit while
    silently keeping one you have forgotten about.
    """

    window_hours: int = Field(default=24, ge=1, le=8760)
    usd_per_window: Optional[float] = Field(default=None, ge=0.0)
    tokens_per_window: Optional[int] = Field(default=None, ge=0)


def _ceiling_out(ceiling: budget.Ceiling) -> AdminBudgetCeilingOut:
    return AdminBudgetCeilingOut(
        window_hours=ceiling.window_hours,
        usd_per_window=ceiling.usd,
        tokens_per_window=ceiling.tokens,
        source=ceiling.source,
    )


def _spend_out(spend: budget.Spend) -> AdminBudgetSpendOut:
    return AdminBudgetSpendOut(
        calls=spend.calls,
        cost_usd=round(spend.cost_usd, 6),
        total_tokens=spend.total_tokens,
        unpriced_calls=spend.unpriced_calls,
    )


def _parked_on_budget(db: Session, workspace_id: str) -> List[Run]:
    """Runs this workspace's ceiling is currently holding.

    Filtered on `paused_reason` as well as status, which is the whole reason
    that column exists: a run parked on an approval is waiting on a decision, and
    raising a spend limit must not touch it.
    """
    return list(
        db.scalars(
            select(Run)
            .where(
                Run.workspace_id == workspace_id,
                Run.status == "waiting_for_approval",
                Run.paused_reason == PAUSED_FOR_BUDGET,
            )
            .order_by(Run.created_at)
            .limit(MAX_PARKED_RUNS)
        )
    )


def _budget_out(
    db: Session, *, workspace_id: str, settings: Settings, resumed: List[str]
) -> AdminBudgetOut:
    ceiling = budget.effective_ceiling(
        db, workspace_id=workspace_id, settings=settings
    )
    since = utcnow() - timedelta(hours=ceiling.window_hours)
    parked = _parked_on_budget(db, workspace_id)
    return AdminBudgetOut(
        ceiling=_ceiling_out(ceiling),
        unattended_ceiling=_ceiling_out(
            ceiling.scaled(settings.unattended_budget_fraction)
        ),
        spend=_spend_out(
            budget.window_spend(db, workspace_id=workspace_id, since=since)
        ),
        unattended_spend=_spend_out(
            budget.window_spend(
                db,
                workspace_id=workspace_id,
                since=since,
                operations=budget.UNATTENDED_OPERATIONS,
            )
        ),
        enforced=not ceiling.unlimited,
        pricing_configured=bool(settings.model_prices),
        runs_parked_on_budget=[
            AdminBudgetParkedRunOut(
                run_id=run.id,
                conversation_id=run.conversation_id,
                created_at=run.created_at,
            )
            for run in parked
        ],
        resumed_run_ids=resumed,
    )


@router.get("/budget", response_model=AdminBudgetOut)
def get_budget(
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AdminBudgetOut:
    """This workspace's spend ceiling, what it has spent against it, and what
    it is currently holding."""
    return _budget_out(
        db, workspace_id=actor.workspace_id, settings=settings, resumed=[]
    )


@router.put("/budget", response_model=AdminBudgetOut)
def set_budget(
    payload: AdminBudgetRequest,
    background_tasks: BackgroundTasks,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AdminBudgetOut:
    """Set the ceiling, and release whatever the new one no longer stops.

    Raising a limit and releasing the runs it was holding is one gesture on
    purpose. Splitting them would leave an owner who has fixed the problem
    staring at a still-parked run, hunting for a second button — and the
    interesting case, a raise that is still not enough, is not a special case
    here: `budget.evaluate` is asked again per run, so a run that is still over
    is simply not released.

    The re-check is the same predicate the loop enforces, called from the same
    module, so this route cannot be more generous than the ceiling itself.
    """
    row = db.scalar(
        select(WorkspaceBudget).where(
            WorkspaceBudget.workspace_id == actor.workspace_id
        )
    )
    if row is None:
        row = WorkspaceBudget(workspace_id=actor.workspace_id)
        db.add(row)
    row.window_hours = payload.window_hours
    row.usd_per_window = payload.usd_per_window
    row.tokens_per_window = payload.tokens_per_window
    row.updated_by = actor.user_id
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="workspace_budget.updated",
        resource_type="workspace_budget",
        resource_id=actor.workspace_id,
        detail={
            "window_hours": payload.window_hours,
            "usd_per_window": payload.usd_per_window,
            "tokens_per_window": payload.tokens_per_window,
        },
    )
    db.commit()

    resumed: List[str] = []
    for run in _parked_on_budget(db, actor.workspace_id):
        verdict = budget.evaluate(
            db,
            workspace_id=actor.workspace_id,
            unattended=policy_scope_for_run(db, run) == WORKFLOW_SCOPE,
            settings=settings,
        )
        if not verdict.allowed:
            continue
        resumed.append(run.id)
        background_tasks.add_task(resume_run_after_budget, run.id)
    return _budget_out(
        db, workspace_id=actor.workspace_id, settings=settings, resumed=resumed
    )


# --------------------------------------------------------------------------
# Observability (latency, throughput, errors, liveness, retention)
#
# Everything here is *derived* from rows the app already writes — no new column,
# no write on the hot path, so a run behaves identically whether or not this page
# is ever opened. Total wall latency is `updated_at − created_at` on a terminal
# run (the terminal write is the last write). TTFT is the first `message.delta`
# event's timestamp minus the run's `created_at`, measured from the same origin
# as total so queue + retrieval + recall latency is folded into the
# user-perceived number and no second event query is needed.
#
# The cost is entirely at read time and bounded on both axes: the window is
# capped at 30 days and the runs sampled for percentiles are capped at
# MAX_LATENCY_RUNS, so the one scan over the highest-volume table (run_events for
# TTFT) is restricted to that already-bounded set of run ids. Like every other
# panel here it carries only ids, counts and timings — no content, no secrets,
# and retention is counts of distinct users, never their ids.

# Runs sampled for the latency distributions and throughput buckets. The true
# count of runs in the window is reported separately (`runs_in_window`); this
# only bounds the per-run millisecond lists and the single run_events scan.
MAX_LATENCY_RUNS = 2000
RECENT_FAILURES_LIMIT = 20
LIVE_RUNS_LIMIT = 50
# Equal-width time buckets for the throughput sparkline, computed in Python so
# the route is portable across SQLite and Postgres (no date_trunc).
THROUGHPUT_BUCKETS = 24
# A run parked on an approval or a budget ceiling is waiting on a person, not
# occupying a worker; "live" is only what is actually executing or draining.
LIVE_RUN_STATES = ("queued", "running", "cancelling")


class AdminLatencyStatsOut(ApiModel):
    """One metric's distribution. Every percentile is None on an empty sample —
    never 0, which would read as an instant response rather than no data."""

    samples: int
    p50_ms: Optional[int]
    p90_ms: Optional[int]
    p99_ms: Optional[int]
    max_ms: Optional[int]


class AdminThroughputBucketOut(ApiModel):
    start: datetime
    count: int


class AdminLiveRunOut(ApiModel):
    run_id: str
    conversation_id: str
    agent_id: str
    status: str
    created_at: datetime
    age_seconds: int


class AdminFailedRunOut(ApiModel):
    run_id: str
    conversation_id: str
    # Clipped like every other free-text preview on this router.
    error: str
    created_at: datetime
    updated_at: datetime


class AdminRetentionOut(ApiModel):
    """Distinct active members over three windows. Counts only — a user id here
    would be a foreign identifier this route has no reason to emit."""

    dau: int
    wau: int
    mau: int


class AdminObservabilityOut(ApiModel):
    window_hours: int
    since: datetime
    # Completed/terminal runs that produced at least one streamed delta.
    ttft: AdminLatencyStatsOut
    # Terminal runs (completed, failed, cancelled).
    total: AdminLatencyStatsOut
    runs_in_window: int
    throughput: List[AdminThroughputBucketOut]
    completed: int
    failed: int
    cancelled: int
    # failed / (completed + failed + cancelled); 0.0 when there are no terminal
    # runs, so the panel never divides by zero.
    error_rate: float
    recent_failures: List[AdminFailedRunOut]
    live_runs: List[AdminLiveRunOut]
    retention: AdminRetentionOut


def _latency_stats(values: List[int]) -> AdminLatencyStatsOut:
    """Nearest-rank percentiles over a per-run millisecond list, in Python.

    SQLite has no `percentile_cont` and this app runs on both backends, so the
    distribution is computed here rather than in SQL. An empty sample yields
    None everywhere — the caller must be able to tell "no runs" from "instant".
    """
    if not values:
        return AdminLatencyStatsOut(
            samples=0, p50_ms=None, p90_ms=None, p99_ms=None, max_ms=None
        )
    ordered = sorted(values)
    count = len(ordered)

    def rank(percentile: int) -> int:
        # Nearest-rank: the value at ceil(p/100 * n), 1-indexed, clamped in.
        index = math.ceil(percentile / 100 * count) - 1
        return ordered[min(max(index, 0), count - 1)]

    return AdminLatencyStatsOut(
        samples=count,
        p50_ms=rank(50),
        p90_ms=rank(90),
        p99_ms=rank(99),
        max_ms=ordered[-1],
    )


@router.get("/observability", response_model=AdminObservabilityOut)
def get_observability(
    hours: int = Query(default=24, ge=1, le=720),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> AdminObservabilityOut:
    """Latency, throughput, errors, live runs and retention over a window.

    One capped select of the window's runs feeds latency, throughput and the
    error counts; a single grouped MIN over `run_events` — restricted to those
    run ids — gives TTFT; retention reaches `user_sessions` only through the
    memberships join, so it stays workspace-scoped over a table that carries no
    workspace of its own.
    """
    workspace = actor.workspace_id
    now = utcnow()
    since = now - timedelta(hours=hours)

    windowed = list(
        db.scalars(
            select(Run)
            .where(Run.workspace_id == workspace, Run.created_at >= since)
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(MAX_LATENCY_RUNS)
        )
    )
    runs_in_window = _total(
        db, Run, Run.workspace_id == workspace, Run.created_at >= since
    )

    # Total wall latency: terminal runs only, where updated_at is the completion.
    # This is wall-clock, so a run that streamed its first token in 200ms but then
    # parked for hours on an approval or the budget ceiling contributes those hours
    # here — deliberately, since it is the true end-to-end time. TTFT below is the
    # responsiveness metric that is not distorted by a park.
    total_values = [
        int((run.updated_at - run.created_at).total_seconds() * 1000)
        for run in windowed
        if run.status in TERMINAL_RUN_STATES
    ]

    # TTFT: first streamed delta minus the run's origin, for the windowed runs
    # that produced one. A denied or parked run emits no delta and is excluded.
    run_ids = [run.id for run in windowed]
    first_delta: Dict[str, datetime] = {}
    if run_ids:
        first_delta = {
            str(rid): stamp
            for rid, stamp in db.execute(
                select(RunEvent.run_id, func.min(RunEvent.created_at))
                .where(
                    RunEvent.workspace_id == workspace,
                    RunEvent.event_type == "message.delta",
                    RunEvent.run_id.in_(run_ids),
                )
                .group_by(RunEvent.run_id)
            ).all()
        }
    ttft_values = [
        max(0, int((first_delta[run.id] - run.created_at).total_seconds() * 1000))
        for run in windowed
        if run.id in first_delta
    ]

    # Throughput: an exact count per equal-width bucket over the WHOLE window.
    # Not derived from `windowed` — that list is capped at MAX_LATENCY_RUNS for the
    # percentile sample, so bucketing it would silently undercount a busy workspace
    # while the sparkline claims exact counts. One bounded COUNT per bucket
    # (THROUGHPUT_BUCKETS of them) is exact and portable, and never loads a row.
    span = (now - since) / THROUGHPUT_BUCKETS
    throughput = []
    for i in range(THROUGHPUT_BUCKETS):
        start = since + span * i
        conditions = [Run.workspace_id == workspace, Run.created_at >= start]
        # The last bucket stays open-ended so a run created at the query instant
        # (created_at == now) is counted rather than dropped by a strict upper bound.
        if i < THROUGHPUT_BUCKETS - 1:
            conditions.append(Run.created_at < since + span * (i + 1))
        throughput.append(
            AdminThroughputBucketOut(start=start, count=_total(db, Run, *conditions))
        )

    status_counts = _counts(
        db, Run.status, Run.workspace_id == workspace, Run.created_at >= since
    )
    completed = status_counts.get("completed", 0)
    failed = status_counts.get("failed", 0)
    cancelled = status_counts.get("cancelled", 0)
    terminal = completed + failed + cancelled
    error_rate = failed / terminal if terminal else 0.0

    failures = list(
        db.scalars(
            select(Run)
            .where(Run.workspace_id == workspace, Run.status == "failed")
            .order_by(Run.updated_at.desc(), Run.id.desc())
            .limit(RECENT_FAILURES_LIMIT)
        )
    )
    live = list(
        db.scalars(
            select(Run)
            .where(
                Run.workspace_id == workspace,
                Run.status.in_(LIVE_RUN_STATES),
            )
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(LIVE_RUNS_LIMIT)
        )
    )

    def _active_members(days: int) -> int:
        # `user_sessions` carries no workspace, so activity is joined to this
        # workspace's memberships. A user active in *another* workspace still counts
        # here if they are also a member of this one — so for a person in several
        # workspaces this is an upper bound on this workspace's own activity, not an
        # exact attribution. Named here rather than hidden.
        cutoff = now - timedelta(days=days)
        return int(
            db.scalar(
                select(func.count(func.distinct(UserSession.user_id)))
                .join(Membership, Membership.user_id == UserSession.user_id)
                .where(
                    Membership.workspace_id == workspace,
                    UserSession.last_seen_at >= cutoff,
                )
            )
            or 0
        )

    return AdminObservabilityOut(
        window_hours=hours,
        since=since,
        ttft=_latency_stats(ttft_values),
        total=_latency_stats(total_values),
        runs_in_window=runs_in_window,
        throughput=throughput,
        completed=completed,
        failed=failed,
        cancelled=cancelled,
        error_rate=error_rate,
        recent_failures=[
            AdminFailedRunOut(
                run_id=run.id,
                conversation_id=run.conversation_id,
                error=_clip(run.error),
                created_at=run.created_at,
                updated_at=run.updated_at,
            )
            for run in failures
        ],
        live_runs=[
            AdminLiveRunOut(
                run_id=run.id,
                conversation_id=run.conversation_id,
                agent_id=run.agent_id,
                status=run.status,
                created_at=run.created_at,
                age_seconds=int((now - run.created_at).total_seconds()),
            )
            for run in live
        ],
        retention=AdminRetentionOut(
            dau=_active_members(1),
            wau=_active_members(7),
            mau=_active_members(30),
        ),
    )


# --------------------------------------------------------------------------
# Per-agent scorecard
# --------------------------------------------------------------------------


class AdminAgentStatsOut(ApiModel):
    """One authored agent's window: what it ran, what it spent, what it asked.

    The trust columns are the point. `denied_calls` and `mode_approved_calls`
    say how often this agent's writes met a human "no" or rode a bypass, and
    `flagged_runs` counts turns the injection screen flagged — the three
    numbers an org admin reads before widening an agent's tool subset.
    """

    agent_id: str
    name: str
    enabled: bool
    runs: int
    completed_runs: int
    failed_runs: int
    tool_calls: int
    denied_calls: int
    #: Calls that executed on a mode's say-so (`decided_by` = "mode:…" —
    #: auto_writes bypasses and guardian approvals alike), i.e. writes no
    #: person reviewed before they ran.
    mode_approved_calls: int
    flagged_runs: int
    usage_calls: int
    total_tokens: int
    cost_usd: float
    unpriced_calls: int


class AdminAgentsOut(ApiModel):
    window_days: int
    since: datetime
    agents: List[AdminAgentStatsOut]


@router.get("/agents", response_model=AdminAgentsOut)
def get_agent_stats(
    days: int = Query(default=30, ge=1, le=MAX_USAGE_WINDOW_DAYS),
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> AdminAgentsOut:
    """Every authored agent's activity, cost, and trust posture in one window.

    Five GROUP BYs over indexed windows — runs by status, tool calls by
    status, mode-approved calls, screen-flagged runs, and the usage ledger by
    its own `agent_id` column (0046) — so the route's cost tracks the window,
    not the workspace. Retired (disabled) agents stay on the list: their
    history is exactly what retirement preserves.
    """
    since = utcnow() - timedelta(days=days)
    agents = list(
        db.scalars(
            select(Agent)
            .where(Agent.workspace_id == actor.workspace_id)
            .order_by(Agent.created_at, Agent.id)
        )
    )

    run_counts: Dict[str, Dict[str, int]] = {}
    for agent_id, status, count in db.execute(
        select(Run.agent_id, Run.status, func.count())
        .where(Run.workspace_id == actor.workspace_id, Run.created_at >= since)
        .group_by(Run.agent_id, Run.status)
    ):
        run_counts.setdefault(str(agent_id), {})[str(status)] = int(count)

    call_counts: Dict[str, Dict[str, int]] = {}
    for agent_id, status, count in db.execute(
        select(Run.agent_id, AgentToolCall.status, func.count())
        .select_from(AgentToolCall)
        .join(Run, AgentToolCall.run_id == Run.id)
        .where(
            AgentToolCall.workspace_id == actor.workspace_id,
            AgentToolCall.created_at >= since,
        )
        .group_by(Run.agent_id, AgentToolCall.status)
    ):
        call_counts.setdefault(str(agent_id), {})[str(status)] = int(count)

    mode_approved: Dict[str, int] = {
        str(agent_id): int(count)
        for agent_id, count in db.execute(
            select(Run.agent_id, func.count())
            .select_from(AgentToolCall)
            .join(Run, AgentToolCall.run_id == Run.id)
            .where(
                AgentToolCall.workspace_id == actor.workspace_id,
                AgentToolCall.created_at >= since,
                AgentToolCall.decided_by.like("mode:%"),
            )
            .group_by(Run.agent_id)
        )
    }

    flagged: Dict[str, int] = {
        str(agent_id): int(count)
        for agent_id, count in db.execute(
            select(Run.agent_id, func.count(func.distinct(RunEvent.run_id)))
            .select_from(RunEvent)
            .join(Run, RunEvent.run_id == Run.id)
            .where(
                RunEvent.workspace_id == actor.workspace_id,
                RunEvent.event_type == "screen.flagged",
                RunEvent.created_at >= since,
            )
            .group_by(Run.agent_id)
        )
    }

    usage_by_agent: Dict[str, Sequence[Any]] = {
        key: sums
        for key, sums in _usage_groups(
            db,
            ModelUsage.agent_id,
            ModelUsage.workspace_id == actor.workspace_id,
            ModelUsage.created_at >= since,
            limit=1000,
        )
        if key
    }

    rows: List[AdminAgentStatsOut] = []
    for agent in agents:
        runs = run_counts.get(agent.id, {})
        calls = call_counts.get(agent.id, {})
        sums = usage_by_agent.get(agent.id)
        usage_calls, _i, _c, _o, _r, total, cost, priced = sums or (0,) * 8
        rows.append(
            AdminAgentStatsOut(
                agent_id=agent.id,
                name=agent.name,
                enabled=bool(agent.enabled),
                runs=sum(runs.values()),
                completed_runs=runs.get("completed", 0),
                failed_runs=runs.get("failed", 0),
                tool_calls=sum(calls.values()),
                denied_calls=calls.get("denied", 0),
                mode_approved_calls=mode_approved.get(agent.id, 0),
                flagged_runs=flagged.get(agent.id, 0),
                usage_calls=int(usage_calls),
                total_tokens=int(total),
                cost_usd=float(cost or 0.0),
                unpriced_calls=int(usage_calls) - int(priced),
            )
        )
    return AdminAgentsOut(window_days=days, since=since, agents=rows)
