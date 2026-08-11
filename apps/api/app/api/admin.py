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
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from ..auth import Actor, require_owner
from ..clock import utcnow
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
    ModelUsage,
    Run,
    SandboxSession,
    Source,
    User,
    WorkspaceBudget,
)
from ..schemas import ApiModel
from ..services import budget
from ..services.agent_loop import (
    PAUSED_FOR_BUDGET,
    WORKFLOW_SCOPE,
    policy_scope_for_run,
)
from ..services.audit import record_audit
from ..services.runs import resume_run_after_budget
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
