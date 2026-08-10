"""Session lifecycle, quotas, and the tenancy boundary for sandboxes.

Three jobs live here, and they are in one module because they are the same job
seen from three angles: a `sandbox_sessions` row is the only thing that names a
machine, so whoever controls the row controls who can reach the machine, how
many machines a workspace may hold at once, and when a machine stops costing
money.

The load-bearing function is `resolve_session`. Every path that turns a session
id into a live handle goes through it — tools, the HTTP API, the reaper — and it
filters on `workspace_id` inside the query. ADR 0005 states the property it
buys: there is no code path that accepts a provider id from a caller, and no
unscoped `get(session_id)` for anyone to reach for by accident.

A second, quieter rule: the row records the egress policy that was in force when
the machine was created, and every later read uses the row. See `ensure_session`
for why that is a security property rather than bookkeeping.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import List, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...clock import utcnow
from ...config import Settings
from ...models import SandboxExecution, SandboxSession, new_id
from . import policy
from .provider import get_provider
from .types import (
    ExecResult,
    NetworkPolicy,
    SandboxError,
    SandboxHandle,
    SandboxQuotaError,
    SandboxSpec,
)

#: The statuses in which a row still names a machine at the provider, and so the
#: statuses that count against the concurrency quota. "killed" is terminal and
#: "error" never had a machine to begin with; neither costs anything.
LIVE_STATUSES: Tuple[str, ...] = ("running", "paused")

#: Appended to any field `clip` shortened, so a reader of the activity trail can
#: tell a quiet command from a truncated one. Mirrors the MCP client's marker
#: (services/mcp/client.py) rather than inventing a second convention.
TRUNCATION_NOTE = "\n…(truncated)"


def resolve_session(
    db: Session, *, workspace_id: str, session_id: str
) -> SandboxSession:
    """Turn a session id into a row. **This is the tenancy boundary.**

    Nothing else in the codebase selects `SandboxSession` by id. The workspace
    filter is part of the query rather than an `if` after it, because a row
    fetched by id and checked afterwards is one early return away from being
    returned unchecked — and the thing on the other side of the check is another
    tenant's live machine with their documents already loaded into it.

    Callers that legitimately hold a row from a broader query (the reaper) still
    come back through here with that row's own `workspace_id`, so there is
    exactly one function that hands out a session and exactly one place to audit.
    """
    row = db.scalars(
        select(SandboxSession)
        .where(SandboxSession.id == session_id)
        .where(SandboxSession.workspace_id == workspace_id)
    ).first()
    if row is None:
        # Deliberately the same message whether the id is unknown or belongs to
        # someone else: distinguishing them turns this into an oracle that
        # confirms a guessed id exists.
        raise SandboxError(f"No sandbox session “{session_id}” in this workspace")
    return row


def handle_for(session: SandboxSession) -> SandboxHandle:
    """Address the machine this row names.

    Refuses rows that do not name one. A killed session's `external_id` is a
    provider id that has been reused or reclaimed by now, and an "error" row
    never got one at all, so returning a handle for either would send an
    execution at whatever happens to answer to that id today.
    """
    if session.status not in LIVE_STATUSES or not session.external_id:
        raise SandboxError(
            f"Sandbox session is {session.status} and cannot run anything. "
            "Start a new session."
        )
    return SandboxHandle(provider=session.provider, external_id=session.external_id)


def allow_hosts_for(session: SandboxSession) -> List[str]:
    """The egress allowlist recorded on the row at creation time.

    Read this, never `settings.sandbox_allowed_hosts`, when describing or
    re-applying a live session's policy — see `ensure_session`.
    """
    try:
        parsed = json.loads(session.allow_hosts_json or "[]")
    except ValueError:
        # A corrupt column must fail closed: an empty allowlist under the
        # `allowlist` policy means "nothing", which is the safe reading.
        return []
    return [str(host) for host in parsed] if isinstance(parsed, list) else []


def list_sessions(db: Session, *, workspace_id: str) -> List[SandboxSession]:
    """Every session this workspace has, newest activity first.

    Killed and errored rows are included on purpose. A create that failed and
    left no trace is unexplainable in the UI, and hiding the failure is how a
    user ends up retrying the same broken configuration six times.
    """
    return list(
        db.scalars(
            select(SandboxSession)
            .where(SandboxSession.workspace_id == workspace_id)
            .order_by(SandboxSession.last_used_at.desc(), SandboxSession.created_at.desc())
        )
    )


def touch(db: Session, session: SandboxSession) -> None:
    """Mark the session used. Idleness is what the reaper measures, so a session
    that is being worked in must say so or it gets killed mid-analysis."""
    session.last_used_at = utcnow()
    db.commit()


def ensure_session(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    settings: Settings,
    project_id: str = "",
    label: str = "",
) -> SandboxSession:
    """Return this workspace's sandbox for `project_id`, creating one if needed.

    Reuse is the point, not an optimisation: the interpreter is persistent, so
    the second turn of "now plot the column you just cleaned" only works if it
    lands in the same machine. Creating a fresh one per tool call would also
    multiply the bill by the length of the conversation.
    """
    existing = db.scalars(
        select(SandboxSession)
        .where(SandboxSession.workspace_id == workspace_id)
        .where(SandboxSession.project_id == project_id)
        .where(SandboxSession.status.in_(LIVE_STATUSES))
        .order_by(SandboxSession.created_at.desc())
    ).first()
    if existing is not None:
        touch(db, existing)
        return existing

    # Quota first, provider second. Checking after creation would mean the
    # machine that breaks the limit is already running and already billable, and
    # ADR 0005 is explicit that quotas are enforced before creation rather than
    # discovered on the invoice.
    live = db.scalar(
        select(func.count())
        .select_from(SandboxSession)
        .where(SandboxSession.workspace_id == workspace_id)
        .where(SandboxSession.status.in_(LIVE_STATUSES))
    )
    limit = settings.sandbox_max_concurrent_per_workspace
    if (live or 0) >= limit:
        raise SandboxQuotaError(
            f"This workspace already has {live} sandbox sessions running "
            f"(limit {limit}). Stop one before starting another."
        )

    provider = get_provider(settings)
    spec = SandboxSpec(
        workspace_id=workspace_id,
        template=settings.sandbox_template,
        timeout_seconds=settings.sandbox_session_timeout_seconds,
        network=settings.sandbox_network_policy,
        allow_hosts=tuple(settings.sandbox_allowed_hosts),
        env=policy.sandbox_env(settings),
        metadata=policy.session_metadata(workspace_id=workspace_id, user_id=user_id),
    )

    row = SandboxSession(
        id=new_id(),
        workspace_id=workspace_id,
        project_id=project_id,
        created_by=user_id,
        provider=provider.name,
        external_id="",
        template=spec.template,
        label=label,
        status="running",
        # The policy is frozen onto the row here, and everything downstream reads
        # it from the row. Re-deriving it from `settings` later would mean that
        # relaxing the workspace default to `open` retroactively widens a machine
        # that is *already holding someone's documents* — a sandbox created under
        # `allowlist` must live and die under `allowlist`.
        network_policy=spec.network,
        allow_hosts_json=json.dumps(list(spec.allow_hosts)),
    )

    try:
        handle = provider.create(spec)
    except Exception as exc:  # noqa: BLE001 — narrowed to a safe message below
        # A failed create that leaves no trace is unexplainable in the UI: the
        # user sees a tool fail, retries, and has nothing to show support. So the
        # row is persisted with the failure before the exception continues.
        row.status = "error"
        row.error = _describe(exc)
        # (provider, external_id) is unique and the column is not nullable, so
        # errored rows get a placeholder that is obviously not a provider id
        # rather than colliding on "".
        row.external_id = f"error:{row.id}"
        db.add(row)
        db.commit()
        raise SandboxError(row.error) from exc

    row.provider = handle.provider
    row.external_id = handle.external_id
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def record_execution(
    db: Session,
    *,
    session: SandboxSession,
    run_id: str,
    tool_call_id: str,
    kind: str,
    source: str,
    result: ExecResult,
    settings: Settings,
) -> SandboxExecution:
    """Write one execution to the activity trail and bill it to the session.

    Output is clipped before it is stored: this table records what happened, not
    a place to park a 200 MB build log, and an unclipped `pip install -v` will
    happily supply one.
    """
    limit = settings.sandbox_max_output_bytes
    error = result.error
    if result.traceback:
        # Keep the traceback with the message — the pair is what makes a failed
        # run diagnosable after the conversation has moved on.
        error = f"{error}\n{result.traceback}".strip()

    row = SandboxExecution(
        workspace_id=session.workspace_id,
        session_id=session.id,
        run_id=run_id,
        tool_call_id=tool_call_id,
        kind=kind,
        # The source is model-authored and unbounded too, so it gets the same
        # budget rather than a separate one nobody would remember to tune.
        source=_clipped(source, limit),
        exit_code=result.exit_code,
        stdout=_clipped(result.stdout, limit),
        stderr=_clipped(result.stderr, limit),
        error=_clipped(error, limit),
        artifact_count=len(result.artifacts),
        duration_ms=result.duration_ms,
    )

    session.exec_count += 1
    # Clamp: a provider that reports a negative or absent duration must not be
    # able to hand a workspace back wall-clock it has already spent.
    session.wall_ms_used += max(0, result.duration_ms)
    session.last_used_at = utcnow()
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def pause_session(
    db: Session, *, workspace_id: str, session_id: str, settings: Settings
) -> SandboxSession:
    """Snapshot and stop the machine. Idempotent."""
    row = resolve_session(db, workspace_id=workspace_id, session_id=session_id)
    if row.status != "running":
        # Already paused, killed or errored — nothing to do, and re-pausing a
        # killed session must not resurrect it.
        return row
    _with_provider(row, settings, "pause")
    row.status = "paused"
    db.commit()
    db.refresh(row)
    return row


def kill_session(
    db: Session, *, workspace_id: str, session_id: str, settings: Settings
) -> SandboxSession:
    """Destroy the machine. Idempotent, because the reaper and a user's explicit
    delete race, and both are entitled to succeed."""
    row = resolve_session(db, workspace_id=workspace_id, session_id=session_id)
    if row.status == "killed":
        return row
    if row.status in LIVE_STATUSES:
        _with_provider(row, settings, "kill")
    row.status = "killed"
    row.killed_at = utcnow()
    db.commit()
    db.refresh(row)
    return row


def reap_idle(db: Session, *, settings: Settings) -> int:
    """Kill every session idle longer than `sandbox_session_idle_days`.

    Not optional hygiene: a paused sandbox is a filesystem snapshot the provider
    keeps indefinitely and charges for indefinitely, so without this a workspace
    that experimented once in March is still paying for it in December.

    The scan is deliberately cross-workspace — it is a background job, not a
    request — but each kill still goes back through `resolve_session` with the
    row's own workspace id, so there remains exactly one path that turns an id
    into a machine.
    """
    cutoff = utcnow() - timedelta(days=settings.sandbox_session_idle_days)
    stale = list(
        db.scalars(
            select(SandboxSession)
            .where(SandboxSession.status.in_(LIVE_STATUSES))
            .where(SandboxSession.last_used_at < cutoff)
        )
    )
    reaped = 0
    for row in stale:
        try:
            kill_session(
                db,
                workspace_id=row.workspace_id,
                session_id=row.id,
                settings=settings,
            )
        except SandboxError:
            # One unreachable provider must not stop the sweep; the next run
            # picks this row up again.
            continue
        reaped += 1
    return reaped


def clip(text: str, limit: int) -> Tuple[str, bool]:
    """Shorten `text` to `limit` bytes, returning (clipped, was_truncated).

    The budget is in bytes because that is what the database column and the
    provider's transport actually cost, but the cut is made on a codepoint
    boundary: slicing `"é"` in half yields a string that raises on decode in
    whatever reads the row next, which is a bug three layers away from here.
    """
    if limit <= 0:
        return "", bool(text)
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text, False
    # errors="ignore" drops the partial codepoint left at the cut.
    return raw[:limit].decode("utf-8", errors="ignore"), True


def _clipped(text: str, limit: int) -> str:
    """`clip`, with the truncation marked in the stored text itself."""
    shortened, truncated = clip(text, limit)
    return shortened + TRUNCATION_NOTE if truncated else shortened


def _with_provider(session: SandboxSession, settings: Settings, action: str) -> None:
    """Ask the provider to pause or kill, tolerating a machine it has forgotten.

    A sandbox that timed out is already gone, and a provider outage is not a
    reason to leave a row claiming `running` forever. The status transition
    happens regardless; the failure is recorded on the row so the UI can explain
    it, and the truth comes out on the next attempt to use the session.
    """
    try:
        provider = get_provider(settings)
        handle = handle_for(session)
        if action == "pause":
            provider.pause(handle)
        else:
            provider.kill(handle)
    except Exception as exc:  # noqa: BLE001 — reduced to a safe message
        session.error = _describe(exc)


def _describe(exc: Exception) -> str:
    """A message safe to show a user and to hand back to the model.

    `SandboxError` is already contractually safe. Anything else came out of a
    provider SDK and may carry a connection URL or a key in its repr, so it is
    replaced rather than trimmed — mirrors `mcp/client.py:_describe`, except
    that this one refuses to pass the original text through at all.
    """
    if isinstance(exc, SandboxError):
        return (str(exc).strip() or "The sandbox provider refused the request")[:400]
    return "The sandbox provider is unavailable right now"


def network_policy_of(session: SandboxSession) -> NetworkPolicy:
    """The policy recorded on the row, narrowed for type checking.

    Falls back to the strictest policy rather than the default one: a row with an
    unrecognised value is corrupt, and corrupt must not mean `open`.
    """
    recorded = session.network_policy
    if recorded in ("open", "allowlist", "none"):
        policy_value: NetworkPolicy = recorded  # type: ignore[assignment]
        return policy_value
    return "none"
