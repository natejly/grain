"""REST surface over the execution sandboxes described in ADR 0005.

This is the *human* path into the same machines the agent uses: the panel where
someone pastes a snippet, watches it run, and kills the box afterwards. It shares
every guard with the tool path because it shares the service layer — the routes
below own no lookup logic of their own. In particular no route ever accepts a
provider-side sandbox id; the only way to name a machine is a `sandbox_sessions`
row id, and `session.resolve_session` filters that row by workspace before it
returns. A session belonging to another tenant is therefore indistinguishable
from one that does not exist, which is why the missing-row case answers 404 and
not 403 (see `_load`).

Nothing here reads or writes the provider except through `SandboxProvider`, and
the external id never leaves the server: the client has no use for it and it is
the one value that would let a leaked response address a live machine directly.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..config import Settings, get_settings
from ..database import get_db
from ..models import SandboxExecution, SandboxSession
from ..schemas import ApiModel, ToolArtifact
from ..services.audit import record_audit
from ..services.sandbox import outputs
from ..services.sandbox import provider as provider_module
from ..services.sandbox import session as sessions
from ..services.sandbox.types import ExecResult, Language, SandboxError, SandboxQuotaError

router = APIRouter(prefix="/api/sandbox", tags=["sandbox"])

# A UI history panel, not an audit export: the executions table is append-only
# and a chatty session accumulates thousands of rows, none of which anyone
# scrolls past the first screenful of.
MAX_HISTORY = 200

# Refuse oversized bodies here rather than paying a round trip to the provider to
# be told the same thing. Generous enough for a real notebook cell and small
# enough that it cannot be used to stuff the executions table.
MAX_SOURCE_CHARS = 100_000


class SandboxSessionOut(ApiModel):
    id: str
    project_id: str
    label: str
    provider: str
    template: str
    status: str
    network_policy: str
    allow_hosts: List[str]
    error: str
    exec_count: int
    wall_ms_used: int
    created_at: datetime
    last_used_at: datetime


class SandboxExecutionOut(ApiModel):
    id: str
    kind: str
    source: str
    exit_code: Optional[int]
    stdout: str
    stderr: str
    error: str
    #: How many the provider produced, including any the storage caps refused.
    artifact_count: int
    #: The ones that were stored, addressable. Carried on the row rather than
    #: only on the live run response, so reopening the panel shows the figures
    #: again instead of a count and three empty spaces.
    artifacts: List[ToolArtifact]
    duration_ms: int
    created_at: datetime


class SandboxRunOut(ApiModel):
    """One execution plus the machine it changed.

    The session travels back with the result so the panel can update the status
    and the exec counter without a second request — a run is the one action that
    reliably mutates both.
    """

    execution: SandboxExecutionOut
    #: Object-store descriptors from `outputs.persist_artifacts`. Typed now that
    #: the panel renders them: the reason no chart was ever visible is that the
    #: client's idea of this shape and the server's had drifted apart for a year
    #: with nothing in between able to notice.
    artifacts: List[ToolArtifact]
    truncated: bool
    session: SandboxSessionOut


class SessionRequest(BaseModel):
    # Empty means a scratch session not bound to a project. `ensure_session`
    # reuses the workspace's newest live session for the same project_id, so
    # this doubles as the reuse key.
    project_id: str = ""
    label: str = ""


class RunRequest(BaseModel):
    source: str = Field(..., min_length=1, max_length=MAX_SOURCE_CHARS)
    # "code" runs in the session's persistent kernel, so imports and dataframes
    # survive between calls; "command" is a one-shot shell, which is the
    # `pip install` path.
    kind: Literal["code", "command"] = "code"
    language: Language = "python"


def _session_out(session: SandboxSession) -> SandboxSessionOut:
    return SandboxSessionOut(
        id=session.id,
        project_id=session.project_id,
        label=session.label,
        provider=session.provider,
        template=session.template,
        status=session.status,
        network_policy=session.network_policy,
        # Read off the row, never off Settings: the row records the policy that
        # was in force when the machine was created, and that is the one it is
        # actually running under.
        allow_hosts=sessions.allow_hosts_for(session),
        error=session.error,
        exec_count=session.exec_count,
        wall_ms_used=session.wall_ms_used,
        created_at=session.created_at,
        last_used_at=session.last_used_at,
    )


def _stored_artifacts(raw: str) -> List[ToolArtifact]:
    """Descriptors off the row. A row written before the column existed holds
    "", which is not JSON and is also not a reason to fail a history listing."""
    try:
        return ToolArtifact.collect(json.loads(raw or "[]"))
    except ValueError:
        return []


def _execution_out(execution: SandboxExecution) -> SandboxExecutionOut:
    return SandboxExecutionOut(
        id=execution.id,
        kind=execution.kind,
        source=execution.source,
        exit_code=execution.exit_code,
        stdout=execution.stdout,
        stderr=execution.stderr,
        error=execution.error,
        artifact_count=execution.artifact_count,
        artifacts=_stored_artifacts(execution.artifacts_json),
        duration_ms=execution.duration_ms,
        created_at=execution.created_at,
    )


def _load(db: Session, actor: Actor, session_id: str) -> SandboxSession:
    """Resolve a session id inside the caller's workspace, or 404.

    `resolve_session` is the single chokepoint that turns an id into something
    addressable, and it raises for both "no such row" and "not yours". Collapsing
    them into one status is deliberate: a 403 would confirm that the id names a
    real sandbox in someone else's workspace.

    The service layer's message is dropped rather than forwarded, and the id is
    not echoed. Nothing is learned from the echo — the caller supplied it — but
    the cross-tenant sweep in tests/test_tenant_isolation.py scans response
    bodies for the victim's ids, and a route that reflects one is
    indistinguishable there from a route that leaked one. Matching mcp.py's flat
    "not found" keeps that check meaningful.
    """
    try:
        return sessions.resolve_session(
            db, workspace_id=actor.workspace_id, session_id=session_id
        )
    except SandboxError as exc:
        raise HTTPException(status_code=404, detail="Sandbox session not found") from exc


def _provider_failure(exc: SandboxError) -> HTTPException:
    """Map a service-layer failure to a status the client can act on.

    Quota first, because `SandboxQuotaError` is a subclass: a limit is not a
    fault and 429 tells the UI to say "you have three sandboxes open" rather
    than "the provider is down". Everything else is 502 — the failure is
    upstream, and the message the service layer produced is already safe to
    show (no provider tracebacks, URLs or keys reach here).
    """
    if isinstance(exc, SandboxQuotaError):
        return HTTPException(status_code=429, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


@router.get("", response_model=List[SandboxSessionOut])
def list_sandbox_sessions(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[SandboxSessionOut]:
    """Every session this workspace owns. Reads rows only — a provider outage
    must not make the panel itself unreachable."""
    return [
        _session_out(session)
        for session in sessions.list_sessions(db, workspace_id=actor.workspace_id)
    ]


@router.post("", response_model=SandboxSessionOut, status_code=201)
def create_sandbox_session(
    payload: SessionRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SandboxSessionOut:
    """Ensure a session exists for this project and hand it back.

    "Ensure", not "create": booting a second microVM because someone reloaded
    the page is a bill, not a feature, so `ensure_session` reuses the newest live
    one for the same project_id. 201 either way — the caller asked for a session
    to exist and now it does, and distinguishing the two would leak nothing
    useful while making the client branch.
    """
    try:
        session = sessions.ensure_session(
            db,
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            settings=settings,
            project_id=payload.project_id,
            label=payload.label,
        )
    except SandboxError as exc:
        raise _provider_failure(exc) from exc
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="sandbox_session.created",
        resource_type="sandbox_session",
        resource_id=session.id,
        detail={"provider": session.provider, "network_policy": session.network_policy},
    )
    db.commit()
    return _session_out(session)


@router.get("/{session_id}/executions", response_model=List[SandboxExecutionOut])
def list_sandbox_executions(
    session_id: str,
    limit: int = Query(default=50, ge=1, le=MAX_HISTORY),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[SandboxExecutionOut]:
    """Recent history, newest first.

    The session is resolved before the query so a foreign id never reaches the
    executions table; the redundant workspace filter below is defence in depth
    against that ordering being edited away.
    """
    session = _load(db, actor, session_id)
    rows = list(
        db.scalars(
            select(SandboxExecution)
            .where(
                SandboxExecution.session_id == session.id,
                SandboxExecution.workspace_id == actor.workspace_id,
            )
            .order_by(SandboxExecution.created_at.desc())
            .limit(limit)
        )
    )
    return [_execution_out(row) for row in rows]


@router.post("/{session_id}/run", response_model=SandboxRunOut)
def run_in_sandbox(
    session_id: str,
    payload: RunRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SandboxRunOut:
    """Run code or a shell command in an existing session.

    This is the direct path — a person typing into the panel — so there is no
    approval gate here the way there is on the agent's tools. The user *is* the
    approval: they wrote the code and pressed the button. What they do not get to
    choose is the machine, which is why the session comes from `_load`.

    User-code failure is a 200 with `error` set. Only infrastructure failure is a
    5xx: conflating "your syntax was wrong" with "the provider is down" makes the
    panel unable to tell the user which one to fix.
    """
    session = _load(db, actor, session_id)
    try:
        driver = provider_module.get_provider(settings)
        handle = sessions.handle_for(session)
        if payload.kind == "command":
            result: ExecResult = driver.run_command(
                handle,
                payload.source,
                timeout=settings.sandbox_exec_timeout_seconds,
            )
        else:
            result = driver.run_code(
                handle,
                payload.source,
                language=payload.language,
                timeout=settings.sandbox_exec_timeout_seconds,
            )
    except SandboxError as exc:
        raise _provider_failure(exc) from exc

    artifacts = outputs.persist_artifacts(
        db,
        workspace_id=actor.workspace_id,
        session_id=session.id,
        result=result,
        settings=settings,
    )
    execution = sessions.record_execution(
        db,
        session=session,
        run_id="",
        tool_call_id="",
        kind=payload.kind,
        source=payload.source,
        result=result,
        settings=settings,
        artifacts=artifacts,
    )
    sessions.touch(db, session)
    db.commit()
    return SandboxRunOut(
        execution=_execution_out(execution),
        artifacts=ToolArtifact.collect(artifacts),
        truncated=result.truncated,
        session=_session_out(session),
    )


@router.post("/{session_id}/pause", response_model=SandboxSessionOut)
def pause_sandbox_session(
    session_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SandboxSessionOut:
    """Snapshot and stop. The filesystem survives; the meter does not."""
    # Resolved first even though `pause_session` scopes by workspace too: its
    # miss raises SandboxError, which the generic mapping would turn into a 502.
    # A sandbox that is not yours must read as absent, not as broken.
    _load(db, actor, session_id)
    try:
        session = sessions.pause_session(
            db,
            workspace_id=actor.workspace_id,
            session_id=session_id,
            settings=settings,
        )
    except SandboxError as exc:
        raise _provider_failure(exc) from exc
    db.commit()
    return _session_out(session)


@router.delete("/{session_id}", response_model=SandboxSessionOut)
def kill_sandbox_session(
    session_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> SandboxSessionOut:
    """Destroy the machine.

    Answers with the killed row rather than 204 so the panel can render the
    terminal state — and its final exec count — without refetching the list.
    """
    # Same reason as pause: 404 for a foreign id has to come from here, before
    # the service layer's SandboxError gets mapped to a 502.
    _load(db, actor, session_id)
    try:
        session = sessions.kill_session(
            db,
            workspace_id=actor.workspace_id,
            session_id=session_id,
            settings=settings,
        )
    except SandboxError as exc:
        raise _provider_failure(exc) from exc
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="sandbox_session.killed",
        resource_type="sandbox_session",
        resource_id=session.id,
        detail={"exec_count": session.exec_count},
    )
    db.commit()
    return _session_out(session)
