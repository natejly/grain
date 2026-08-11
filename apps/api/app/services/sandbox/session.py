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
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Tuple

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

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

#: The statuses in which a row still names a machine at the provider. "killed"
#: is terminal and "error" never had a machine to begin with; neither costs
#: anything.
LIVE_STATUSES: Tuple[str, ...] = ("running", "paused")

#: A row that has claimed a quota slot but whose machine does not exist yet.
#: Deliberately outside LIVE_STATUSES: there is nothing to execute in, so
#: `handle_for` must keep refusing it and reuse must not hand it out.
STARTING_STATUS = "starting"

#: How long an unresolved claim keeps holding its slot. `ensure_session` resolves
#: its own row before returning, so a claim older than this belongs to a process
#: that died mid-create — and letting one crash cost a workspace its only slot
#: forever is worse than the bounded risk of admitting one extra session.
CLAIM_TTL = timedelta(minutes=10)

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


def _holds_a_slot(now: datetime) -> ColumnElement[bool]:
    """Rows that occupy one of a workspace's concurrent sandbox slots."""
    return or_(
        SandboxSession.status.in_(LIVE_STATUSES),
        and_(
            SandboxSession.status == STARTING_STATUS,
            SandboxSession.created_at >= now - CLAIM_TTL,
        ),
    )


def _release_slot(row: SandboxSession) -> None:
    """Give the workspace its slot back. The caller commits.

    Every transition out of "this row names, or is about to name, a machine"
    passes through here, because a slot that is not released is a slot the
    workspace never gets back: the unique index does not care that a row is
    killed, only that it still names a number.
    """
    row.slot_index = None


def _free_slots(db: Session, *, workspace_id: str, limit: int) -> List[int]:
    """Slot numbers this workspace could try to claim, lowest first.

    Two distinct jobs, and neither is the safety property — the unique index is
    that. This picks *candidates*, so that the common case takes one INSERT
    rather than `limit` of them, and it applies the one rule the index cannot
    express: a workspace already holding `limit` slots gets no candidates even if
    the numbers it holds are out of range, which is the state an operator leaves
    behind by lowering the limit under live sessions. A stale read here can only
    hand back a slot that has since been taken — and the INSERT refuses that.
    """
    held = list(
        db.scalars(
            select(SandboxSession.slot_index)
            .where(SandboxSession.workspace_id == workspace_id)
            .where(_holds_a_slot(utcnow()))
        )
    )
    if len(held) >= limit:
        return []
    # A live row with no slot number still spends the workspace's budget above;
    # it just cannot reserve a number for anyone to collide with.
    taken = {index for index in held if index is not None}
    return [index for index in range(limit) if index not in taken]


def _claim_a_slot(
    db: Session,
    *,
    workspace_id: str,
    limit: int,
    build: Callable[[int], SandboxSession],
) -> SandboxSession:
    """Insert a claim that holds one of the workspace's `limit` slots.

    **This is the quota.** Not the count above it — the INSERT. A workspace's
    concurrency is expressed as `limit` numbered slots, `(workspace_id,
    slot_index)` is unique, and so the second transaction to claim a number is
    refused by the database itself. That is the property counting could never
    have: a count answers a question about rows that were visible when it ran,
    and two racers can both be told "nothing ahead of you" when neither has
    committed yet. Ranking the committed claims instead — which is what this
    replaced — moves the flaw rather than fixing it, because the ordering key is
    stamped before the write and the write becomes visible after it, so the row
    that sorts second can commit and be admitted first, and then the row that
    sorts first sees nothing ahead of it and is admitted too.

    Uniqueness is the one serialisation primitive both engines really share.
    `SELECT ... FOR UPDATE` is a no-op on SQLite and SQLite's single-writer lock
    is absent on Postgres, but a unique index refuses a duplicate on both,
    whatever the isolation level and however the transactions interleave. The
    loser learns it lost from an `IntegrityError` on its own INSERT — before the
    provider is reached, so the machine that would have broken the limit is never
    created and never billed.
    """
    # An abandoned claim stops counting at CLAIM_TTL, and now that a slot is a
    # reservation rather than a tally, "stops counting" has to be written down:
    # the row must give the number back or one crash costs the workspace a slot
    # until the reaper next runs.
    _retire_abandoned_claims(db, workspace_id=workspace_id)

    conflict: Optional[IntegrityError] = None
    for index in _free_slots(db, workspace_id=workspace_id, limit=limit):
        row = build(index)
        db.add(row)
        try:
            db.commit()
        except IntegrityError as exc:
            # Someone committed this number between the read above and here.
            # Roll back — the failed INSERT left nothing behind — and try the
            # next one; running out is what "at the limit" means.
            db.rollback()
            conflict = exc
            continue
        return row

    if conflict is not None and len(_free_slots(db, workspace_id=workspace_id, limit=limit)) > 0:
        # Every candidate was refused, yet the workspace is not full: the
        # constraint that rejected the row was not the slot. Reporting that as a
        # quota would send the operator to look at a dial that is fine.
        raise SandboxError("The sandbox session could not be started.") from conflict
    raise SandboxQuotaError(
        f"This workspace already has {limit} sandbox sessions running "
        f"(limit {limit}). Stop one before starting another."
    )


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
    #
    # Counting and then creating is not "first": the window between the two
    # contains a provider round trip, so every overlapping request read the same
    # count, all of them passed, and all of them created a machine. Nor is
    # counting under any other arrangement — see `_claim_a_slot`, which reserves
    # one of the workspace's numbered slots with an INSERT the database will
    # refuse if the number is taken.
    limit = settings.sandbox_max_concurrent_per_workspace
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

    def _claim(slot_index: int) -> SandboxSession:
        # A fresh id per attempt: the row that lost a slot is gone from the
        # session as well as from the table, and reusing one that a failed flush
        # touched is a subtlety nobody should have to hold in their head.
        session_id = new_id()
        return SandboxSession(
            id=session_id,
            workspace_id=workspace_id,
            project_id=project_id,
            created_by=user_id,
            provider=provider.name,
            # (provider, external_id) is unique and the column is not nullable,
            # so the claim carries a placeholder that is obviously not a provider
            # id until the provider supplies the real one.
            external_id=f"pending:{session_id}",
            template=spec.template,
            label=label,
            status=STARTING_STATUS,
            slot_index=slot_index,
            # The policy is frozen onto the row here, and everything downstream
            # reads it from the row. Re-deriving it from `settings` later would
            # mean that relaxing the workspace default to `open` retroactively
            # widens a machine that is *already holding someone's documents* — a
            # sandbox created under `allowlist` must live and die under
            # `allowlist`.
            network_policy=spec.network,
            allow_hosts_json=json.dumps(list(spec.allow_hosts)),
        )

    # Raises SandboxQuotaError before the provider is touched, which is the whole
    # point: the machine that would have broken the limit is never created, so it
    # is never billed for.
    row = _claim_a_slot(db, workspace_id=workspace_id, limit=limit, build=_claim)

    try:
        handle = provider.create(spec)
    except Exception as exc:  # noqa: BLE001 — narrowed to a safe message below
        # A failed create that leaves no trace is unexplainable in the UI: the
        # user sees a tool fail, retries, and has nothing to show support. So the
        # row is kept with the failure recorded before the exception continues.
        row.status = "error"
        row.error = _describe(exc)
        row.external_id = f"error:{row.id}"
        # There is no machine, so the slot goes straight back to the workspace
        # rather than waiting out CLAIM_TTL.
        _release_slot(row)
        db.commit()
        raise SandboxError(row.error) from exc

    row.provider = handle.provider
    row.external_id = handle.external_id
    # Only now is there a machine to reach, so only now does the row leave the
    # claim state that `handle_for` refuses.
    row.status = "running"
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
    # The machine is gone, so the slot is free — this is what makes "stop one
    # before starting another" true immediately rather than eventually.
    _release_slot(row)
    db.commit()
    db.refresh(row)
    return row


def _retire_abandoned_claims(db: Session, *, workspace_id: str = "") -> None:
    """Close out slot claims whose creator died before the provider answered.

    Two callers, one statement. The reaper sweeps every workspace so the session
    list does not fill up with rows that are permanently "starting"; a create
    sweeps its own workspace first, because the slot a dead claim reserved is one
    the constraint will keep refusing until the number is handed back. Letting a
    crash cost a workspace a slot until the next sweep is exactly the failure
    CLAIM_TTL exists to bound.

    One `UPDATE ... WHERE`, so two racers running it concurrently is not a
    problem: the second finds nothing left to retire, and neither can retire a
    claim that is still inside its TTL.
    """
    statement = (
        update(SandboxSession)
        .where(SandboxSession.status == STARTING_STATUS)
        .where(SandboxSession.created_at < utcnow() - CLAIM_TTL)
        .values(
            status="error",
            error="The sandbox never finished starting.",
            slot_index=None,
        )
    )
    if workspace_id:
        statement = statement.where(SandboxSession.workspace_id == workspace_id)
    db.execute(statement)
    db.commit()


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
    _retire_abandoned_claims(db)
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
