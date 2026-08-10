"""Workspace administration: who is in it, what it is doing, what it holds.

This is deliberately *workspace* admin and not an organisation console. There is
no Organization entity in this codebase — no billing account, no cross-workspace
role, no tenant above the workspace — so every panel here answers a question that
`Actor.workspace_id` is already sufficient to scope. Inventing an org-shaped API
over data that has no org would produce endpoints that could only ever return the
caller's own workspace while implying they might one day return more.

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
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from ..auth import Actor, require_owner
from ..config import Settings, get_settings
from ..database import get_db
from ..models import (
    AgentToolCall,
    AuditEvent,
    Chunk,
    GraphEdge,
    GraphEntity,
    McpServer,
    McpTool,
    Membership,
    MemoryItem,
    Run,
    SandboxSession,
    Source,
    User,
)
from ..schemas import ApiModel
from ..services.audit import record_audit
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
    # True for the caller's own row, so the UI can stop an owner demoting or
    # removing themselves without matching ids client-side.
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
