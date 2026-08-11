"""Session lifecycle, quotas, and the tenancy boundary (ADR 0005).

Everything here runs on `SANDBOX_PROVIDER=fake`. That is the point of the driver
seam: a test that needs a real microVM to assert a quota is a test nobody runs.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import create_identity

from app.clock import utcnow
from app.config import Settings
from app.database import SessionLocal
from app.models import SandboxExecution, SandboxSession
from app.services.sandbox import session as session_module
from app.services.sandbox.session import (
    TRUNCATION_NOTE,
    allow_hosts_for,
    clip,
    ensure_session,
    handle_for,
    kill_session,
    list_sessions,
    network_policy_of,
    pause_session,
    reap_idle,
    record_execution,
    resolve_session,
)
from app.services.sandbox.types import (
    ExecResult,
    SandboxError,
    SandboxHandle,
    SandboxQuotaError,
    SandboxSpec,
)


def _settings(**overrides) -> Settings:
    """A sandbox-enabled Settings on the fake provider.

    `_env_file=None` because the repo's .env carries a real OPENAI_API_KEY and a
    real provider selection; a test that reads it asserts the developer's laptop
    rather than the code.
    """
    base = dict(
        _env_file=None,
        app_env="development",
        model_provider="openai",
        openai_api_key="test-key",
        sandbox_enabled=True,
        sandbox_provider="fake",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture(scope="module")
def workspaces():
    """Two real tenants, so the cross-tenant assertion is against real rows."""
    return create_identity(workspace_name="Sandbox A"), create_identity(
        workspace_name="Sandbox B"
    )


@pytest.fixture
def db():
    """A session, with every sandbox row this test wrote removed afterwards.

    `reap_idle` scans across workspaces by design, so leftover rows from one test
    are visible to the next one.
    """
    handle = SessionLocal()
    try:
        yield handle
    finally:
        handle.query(SandboxExecution).delete()
        handle.query(SandboxSession).delete()
        handle.commit()
        handle.close()


class _RecordingProvider:
    """A provider that counts creations, so a test can assert one did not happen."""

    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.created: list[SandboxSpec] = []
        self.paused: list[SandboxHandle] = []
        self.killed: list[SandboxHandle] = []
        self.fail = fail

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        self.created.append(spec)
        if self.fail:
            raise SandboxError("The sandbox provider refused: out of capacity")
        return SandboxHandle(provider="fake", external_id=f"sbx-{len(self.created)}")

    def pause(self, handle: SandboxHandle) -> None:
        self.paused.append(handle)

    def kill(self, handle: SandboxHandle) -> None:
        self.killed.append(handle)


def _install(monkeypatch, provider) -> None:
    """Swap the provider `session.py` reaches for. `get_provider` is lru_cached
    on the settings triple, so patching the name is cleaner than fighting it."""
    monkeypatch.setattr(session_module, "get_provider", lambda settings: provider)


# --- the tenancy boundary ------------------------------------------------


def test_resolve_session_refuses_another_workspaces_session(db, workspaces):
    """THE cross-tenant test.

    `resolve_session` is the only function in the codebase that turns a session
    id into a row. If it can be made to return a row belonging to someone else,
    every tool built on top of it will happily run code inside that tenant's
    machine, with that tenant's documents already loaded. So: holding a valid id
    is not enough — the id must also be ours.
    """
    owner, attacker = workspaces
    settings = _settings()
    created = ensure_session(
        db,
        workspace_id=owner.workspace_id,
        user_id=owner.user_id,
        settings=settings,
    )

    assert resolve_session(
        db, workspace_id=owner.workspace_id, session_id=created.id
    ).id == created.id

    with pytest.raises(SandboxError):
        resolve_session(
            db, workspace_id=attacker.workspace_id, session_id=created.id
        )

    # And every function that acts on a session inherits the refusal, because
    # each of them goes through resolve_session rather than querying by id.
    for action in (pause_session, kill_session):
        with pytest.raises(SandboxError):
            action(
                db,
                workspace_id=attacker.workspace_id,
                session_id=created.id,
                settings=settings,
            )
    db.refresh(created)
    assert created.status == "running"

    # The refusal must not leak whether the id exists at all.
    with pytest.raises(SandboxError) as missing:
        resolve_session(
            db, workspace_id=attacker.workspace_id, session_id="no-such-id"
        )
    with pytest.raises(SandboxError) as foreign:
        resolve_session(
            db, workspace_id=attacker.workspace_id, session_id=created.id
        )
    assert str(missing.value).replace("no-such-id", "X") == str(
        foreign.value
    ).replace(created.id, "X")


def test_list_sessions_is_scoped_to_the_workspace(db, workspaces):
    owner, other = workspaces
    settings = _settings()
    mine = ensure_session(
        db, workspace_id=owner.workspace_id, user_id=owner.user_id, settings=settings
    )
    ensure_session(
        db, workspace_id=other.workspace_id, user_id=other.user_id, settings=settings
    )
    assert [row.id for row in list_sessions(db, workspace_id=owner.workspace_id)] == [
        mine.id
    ]


# --- reuse and quotas ----------------------------------------------------


def test_ensure_session_reuses_rather_than_multiplying(db, workspaces):
    """The interpreter is persistent, so "now plot what you just cleaned" only
    works if the second call lands in the first call's machine."""
    owner, _ = workspaces
    settings = _settings()
    first = ensure_session(
        db, workspace_id=owner.workspace_id, user_id=owner.user_id, settings=settings
    )
    second = ensure_session(
        db, workspace_id=owner.workspace_id, user_id=owner.user_id, settings=settings
    )
    assert second.id == first.id
    assert len(list_sessions(db, workspace_id=owner.workspace_id)) == 1

    # A paused session is still reusable — pausing is a cost measure, not a
    # deletion, and the filesystem survives it.
    pause_session(
        db,
        workspace_id=owner.workspace_id,
        session_id=first.id,
        settings=settings,
    )
    assert ensure_session(
        db, workspace_id=owner.workspace_id, user_id=owner.user_id, settings=settings
    ).id == first.id

    # A different project gets its own machine; a killed one is not reused.
    other_project = ensure_session(
        db,
        workspace_id=owner.workspace_id,
        user_id=owner.user_id,
        settings=settings,
        project_id="proj-1",
    )
    assert other_project.id != first.id
    kill_session(
        db,
        workspace_id=owner.workspace_id,
        session_id=other_project.id,
        settings=settings,
    )
    assert ensure_session(
        db,
        workspace_id=owner.workspace_id,
        user_id=owner.user_id,
        settings=settings,
        project_id="proj-1",
    ).id != other_project.id


def test_concurrency_quota_raises_before_a_sandbox_is_created(db, workspaces, monkeypatch):
    """Quotas are enforced before creation, not discovered on the invoice.

    The provider is a spy: the assertion is not merely that the call raised, but
    that nothing was started at the provider before it did.
    """
    owner, _ = workspaces
    settings = _settings(sandbox_max_concurrent_per_workspace=2)
    provider = _RecordingProvider()
    _install(monkeypatch, provider)

    for project in ("a", "b"):
        ensure_session(
            db,
            workspace_id=owner.workspace_id,
            user_id=owner.user_id,
            settings=settings,
            project_id=project,
        )
    assert len(provider.created) == 2

    with pytest.raises(SandboxQuotaError) as quota:
        ensure_session(
            db,
            workspace_id=owner.workspace_id,
            user_id=owner.user_id,
            settings=settings,
            project_id="c",
        )
    # The message must name the dial: a quota is not a fault, it is a setting.
    assert "limit 2" in str(quota.value)
    assert len(provider.created) == 2
    assert len(list_sessions(db, workspace_id=owner.workspace_id)) == 2

    # Killing one frees the slot: only live sessions count against the limit.
    kill_session(
        db,
        workspace_id=owner.workspace_id,
        session_id=list_sessions(db, workspace_id=owner.workspace_id)[0].id,
        settings=settings,
    )
    ensure_session(
        db,
        workspace_id=owner.workspace_id,
        user_id=owner.user_id,
        settings=settings,
        project_id="c",
    )
    assert len(provider.created) == 3


def test_quota_counts_only_this_workspace(db, workspaces):
    owner, other = workspaces
    settings = _settings(sandbox_max_concurrent_per_workspace=1)
    ensure_session(
        db, workspace_id=owner.workspace_id, user_id=owner.user_id, settings=settings
    )
    # The neighbour's session must not spend our budget.
    ensure_session(
        db, workspace_id=other.workspace_id, user_id=other.user_id, settings=settings
    )
    with pytest.raises(SandboxQuotaError):
        ensure_session(
            db,
            workspace_id=owner.workspace_id,
            user_id=owner.user_id,
            settings=settings,
            project_id="second",
        )


def test_a_slot_is_held_by_one_row_at_a_time_and_returned_when_it_dies(
    db, workspaces, monkeypatch
):
    """The quota is `limit` numbered slots, and the numbers are the enforcement.

    Two rows of one workspace may not name the same slot — the unique index
    refuses the second, which is what makes the limit hold under concurrency
    rather than under the assumption that everybody counted at the right moment.
    So the numbers are asserted here directly: what is claimed, what is released,
    and by whom.
    """
    owner, _ = workspaces
    settings = _settings(sandbox_max_concurrent_per_workspace=2)
    _install(monkeypatch, _RecordingProvider())

    first = ensure_session(
        db,
        workspace_id=owner.workspace_id,
        user_id=owner.user_id,
        settings=settings,
        project_id="a",
    )
    second = ensure_session(
        db,
        workspace_id=owner.workspace_id,
        user_id=owner.user_id,
        settings=settings,
        project_id="b",
    )
    assert {first.slot_index, second.slot_index} == {0, 1}

    # Pausing keeps the slot: a paused sandbox is a snapshot the provider still
    # stores and still charges for, so it has not stopped costing the workspace
    # anything.
    pause_session(
        db, workspace_id=owner.workspace_id, session_id=first.id, settings=settings
    )
    db.refresh(first)
    assert first.slot_index == 0

    # Killing returns it, and the next create takes the number back.
    kill_session(
        db, workspace_id=owner.workspace_id, session_id=first.id, settings=settings
    )
    db.refresh(first)
    assert first.slot_index is None
    third = ensure_session(
        db,
        workspace_id=owner.workspace_id,
        user_id=owner.user_id,
        settings=settings,
        project_id="c",
    )
    assert third.slot_index == 0

    # And another workspace's slot 0 is not this workspace's slot 0.
    _, neighbour = workspaces
    theirs = ensure_session(
        db,
        workspace_id=neighbour.workspace_id,
        user_id=neighbour.user_id,
        settings=settings,
    )
    assert theirs.slot_index == 0


def _abandoned_claim(workspace_id: str, *, age: timedelta) -> SandboxSession:
    """A claim from a process that died before the provider answered."""
    return SandboxSession(
        workspace_id=workspace_id,
        provider="fake",
        external_id=f"pending:abandoned-{age.total_seconds()}",
        status="starting",
        slot_index=0,
        created_at=utcnow() - age,
    )


def test_an_abandoned_claim_gives_its_slot_back_after_the_ttl(
    db, workspaces, monkeypatch
):
    """One crash must not cost a workspace a slot forever.

    A slot is a reservation now, so "the claim stops counting after CLAIM_TTL"
    has to be written to the row rather than merely left out of a count — the
    unique index does not know what a TTL is. It is the create that needs the
    slot which retires the dead claim, so recovery does not wait for the next
    sweep of the reaper.
    """
    owner, _ = workspaces
    settings = _settings(sandbox_max_concurrent_per_workspace=1)
    provider = _RecordingProvider()
    _install(monkeypatch, provider)

    stuck = _abandoned_claim(owner.workspace_id, age=timedelta(minutes=1))
    db.add(stuck)
    db.commit()

    # Inside the TTL the claim holds the workspace's only slot: the process that
    # made it may still be waiting on the provider, and a machine may yet exist.
    with pytest.raises(SandboxQuotaError):
        ensure_session(
            db,
            workspace_id=owner.workspace_id,
            user_id=owner.user_id,
            settings=settings,
            project_id="second",
        )
    assert provider.created == []

    stuck.created_at = utcnow() - session_module.CLAIM_TTL - timedelta(seconds=1)
    db.commit()

    revived = ensure_session(
        db,
        workspace_id=owner.workspace_id,
        user_id=owner.user_id,
        settings=settings,
        project_id="second",
    )
    assert revived.slot_index == 0
    assert revived.status == "running"

    # The dead claim is retired rather than left "starting" forever, so the
    # session list explains itself to whoever reads it.
    db.refresh(stuck)
    assert stuck.status == "error"
    assert stuck.slot_index is None
    assert "never finished starting" in stuck.error


def test_the_reaper_still_retires_abandoned_claims(db, workspaces, monkeypatch):
    """The sweep is the other half: a workspace nobody creates in again must not
    keep a permanently "starting" row, and the reaper is what notices."""
    owner, _ = workspaces
    settings = _settings()
    _install(monkeypatch, _RecordingProvider())

    fresh = _abandoned_claim(owner.workspace_id, age=timedelta(minutes=1))
    dead = _abandoned_claim(owner.workspace_id, age=session_module.CLAIM_TTL * 2)
    dead.slot_index = 1
    db.add_all([fresh, dead])
    db.commit()

    reap_idle(db, settings=settings)

    db.refresh(fresh)
    db.refresh(dead)
    # A claim inside its TTL is none of the reaper's business — the create it
    # belongs to may be one provider round trip from finishing.
    assert (fresh.status, fresh.slot_index) == ("starting", 0)
    assert (dead.status, dead.slot_index) == ("error", None)


# --- failures leave a trace ----------------------------------------------


def test_provider_failure_leaves_an_error_row_and_raises(db, workspaces, monkeypatch):
    """A create that fails silently is unexplainable in the UI."""
    owner, _ = workspaces
    settings = _settings()
    _install(monkeypatch, _RecordingProvider(fail=True))

    with pytest.raises(SandboxError) as failure:
        ensure_session(
            db,
            workspace_id=owner.workspace_id,
            user_id=owner.user_id,
            settings=settings,
        )
    assert "capacity" in str(failure.value)

    rows = list_sessions(db, workspace_id=owner.workspace_id)
    assert len(rows) == 1
    assert rows[0].status == "error"
    assert rows[0].error
    # An errored row never named a machine, so it must not hand out a handle...
    with pytest.raises(SandboxError):
        handle_for(rows[0])
    # ...and it must not count against the concurrency quota either.
    assert rows[0].status not in session_module.LIVE_STATUSES


def test_a_second_failed_create_does_not_collide_on_the_unique_index(
    db, workspaces, monkeypatch
):
    """(provider, external_id) is unique and the column is not nullable, so two
    errored rows must not both claim ""."""
    owner, _ = workspaces
    settings = _settings()
    _install(monkeypatch, _RecordingProvider(fail=True))
    for _ in range(2):
        with pytest.raises(SandboxError):
            ensure_session(
                db,
                workspace_id=owner.workspace_id,
                user_id=owner.user_id,
                settings=settings,
            )
    assert len(list_sessions(db, workspace_id=owner.workspace_id)) == 2


def test_provider_internals_never_reach_the_error_message(db, workspaces, monkeypatch):
    """A raw SDK exception may carry a URL or a key in its repr, so it is
    replaced rather than trimmed."""
    owner, _ = workspaces

    class _Leaky:
        name = "fake"

        def create(self, spec: SandboxSpec) -> SandboxHandle:
            raise RuntimeError("connect https://user:hunter2@api.e2b.dev failed")

    _install(monkeypatch, _Leaky())
    with pytest.raises(SandboxError) as failure:
        ensure_session(
            db,
            workspace_id=owner.workspace_id,
            user_id=owner.user_id,
            settings=_settings(),
        )
    assert "hunter2" not in str(failure.value)
    assert "e2b.dev" not in str(failure.value)
    assert "hunter2" not in list_sessions(db, workspace_id=owner.workspace_id)[0].error


# --- the row is the authority --------------------------------------------


def test_network_policy_is_read_from_the_row_not_from_settings(db, workspaces):
    """Relaxing the workspace default must not retroactively widen a machine
    that is already holding someone's documents."""
    owner, _ = workspaces
    strict = _settings(
        sandbox_network_policy="allowlist", sandbox_host_allowlist="pypi.org"
    )
    created = ensure_session(
        db, workspace_id=owner.workspace_id, user_id=owner.user_id, settings=strict
    )
    assert created.network_policy == "allowlist"
    assert allow_hosts_for(created) == ["pypi.org"]

    # The operator now opens the workspace up. The live sandbox must not follow.
    relaxed = _settings(sandbox_network_policy="open", sandbox_host_allowlist="")
    reused = ensure_session(
        db, workspace_id=owner.workspace_id, user_id=owner.user_id, settings=relaxed
    )
    assert reused.id == created.id
    assert reused.network_policy == "allowlist"
    assert allow_hosts_for(reused) == ["pypi.org"]
    assert network_policy_of(reused) == "allowlist"

    # Only a new machine gets the new policy.
    fresh = ensure_session(
        db,
        workspace_id=owner.workspace_id,
        user_id=owner.user_id,
        settings=relaxed,
        project_id="new",
    )
    assert fresh.network_policy == "open"


def test_a_corrupt_policy_column_fails_closed(db, workspaces):
    owner, _ = workspaces
    created = ensure_session(
        db, workspace_id=owner.workspace_id, user_id=owner.user_id, settings=_settings()
    )
    created.network_policy = "wide-open-please"
    created.allow_hosts_json = "{not json"
    db.commit()
    assert network_policy_of(created) == "none"
    assert allow_hosts_for(created) == []


# --- executions ----------------------------------------------------------


def test_record_execution_clips_oversize_output_and_bills_the_session(db, workspaces):
    owner, _ = workspaces
    settings = _settings(sandbox_max_output_bytes=64)
    created = ensure_session(
        db, workspace_id=owner.workspace_id, user_id=owner.user_id, settings=settings
    )
    result = ExecResult(
        stdout="o" * 5000,
        stderr="e" * 5000,
        exit_code=0,
        duration_ms=1200,
    )
    row = record_execution(
        db,
        session=created,
        run_id="run-1",
        tool_call_id="call-1",
        kind="code",
        source="print('x')",
        result=result,
        settings=settings,
    )
    for field in (row.stdout, row.stderr):
        assert len(field.encode("utf-8")) <= 64 + len(TRUNCATION_NOTE.encode("utf-8"))
        assert field.endswith("(truncated)")

    db.refresh(created)
    assert created.exec_count == 1
    assert created.wall_ms_used == 1200

    # Short output is stored verbatim, with no marker to reason about.
    quiet = record_execution(
        db,
        session=created,
        run_id="run-1",
        tool_call_id="call-2",
        kind="command",
        source="ls",
        result=ExecResult(stdout="ok\n", exit_code=0, duration_ms=5),
        settings=settings,
    )
    assert quiet.stdout == "ok\n"
    db.refresh(created)
    assert created.exec_count == 2
    assert created.wall_ms_used == 1205


def test_record_execution_keeps_the_traceback_with_the_error(db, workspaces):
    owner, _ = workspaces
    settings = _settings()
    created = ensure_session(
        db, workspace_id=owner.workspace_id, user_id=owner.user_id, settings=settings
    )
    row = record_execution(
        db,
        session=created,
        run_id="run-1",
        tool_call_id="call-1",
        kind="code",
        source="1/0",
        result=ExecResult(
            error="ZeroDivisionError: division by zero",
            traceback="Traceback (most recent call last): ...",
            duration_ms=-4,
        ),
        settings=settings,
    )
    assert "ZeroDivisionError" in row.error
    assert "Traceback" in row.error
    db.refresh(created)
    # A negative duration from a misbehaving provider must not refund wall-clock.
    assert created.wall_ms_used == 0


# --- lifecycle -----------------------------------------------------------


def test_pause_and_kill_are_idempotent(db, workspaces, monkeypatch):
    owner, _ = workspaces
    settings = _settings()
    provider = _RecordingProvider()
    _install(monkeypatch, provider)
    created = ensure_session(
        db, workspace_id=owner.workspace_id, user_id=owner.user_id, settings=settings
    )

    for _ in range(2):
        paused = pause_session(
            db,
            workspace_id=owner.workspace_id,
            session_id=created.id,
            settings=settings,
        )
        assert paused.status == "paused"
    assert len(provider.paused) == 1  # the second pause is a no-op, not a call

    killed = kill_session(
        db, workspace_id=owner.workspace_id, session_id=created.id, settings=settings
    )
    assert killed.status == "killed"
    assert killed.killed_at is not None
    first_killed_at = killed.killed_at

    again = kill_session(
        db, workspace_id=owner.workspace_id, session_id=created.id, settings=settings
    )
    assert again.status == "killed"
    assert again.killed_at == first_killed_at
    assert len(provider.killed) == 1

    # And pausing a corpse must not resurrect it.
    assert (
        pause_session(
            db,
            workspace_id=owner.workspace_id,
            session_id=created.id,
            settings=settings,
        ).status
        == "killed"
    )


def test_kill_tolerates_a_provider_that_forgot_the_machine(db, workspaces, monkeypatch):
    """A sandbox that already timed out is gone; that is not a reason to leave a
    row claiming `running` forever."""
    owner, _ = workspaces
    settings = _settings()

    class _Forgetful(_RecordingProvider):
        def kill(self, handle: SandboxHandle) -> None:
            raise SandboxError("No such sandbox")

    _install(monkeypatch, _Forgetful())
    created = ensure_session(
        db, workspace_id=owner.workspace_id, user_id=owner.user_id, settings=settings
    )
    killed = kill_session(
        db, workspace_id=owner.workspace_id, session_id=created.id, settings=settings
    )
    assert killed.status == "killed"
    assert "No such sandbox" in killed.error


def test_reap_idle_kills_only_the_stale_ones(db, workspaces, monkeypatch):
    """A paused sandbox is a snapshot the provider stores — and bills — forever."""
    owner, other = workspaces
    settings = _settings(sandbox_session_idle_days=7)
    provider = _RecordingProvider()
    _install(monkeypatch, provider)

    stale = ensure_session(
        db,
        workspace_id=owner.workspace_id,
        user_id=owner.user_id,
        settings=settings,
        project_id="old",
    )
    stale_paused = ensure_session(
        db,
        workspace_id=other.workspace_id,
        user_id=other.user_id,
        settings=settings,
        project_id="old-paused",
    )
    fresh = ensure_session(
        db,
        workspace_id=owner.workspace_id,
        user_id=owner.user_id,
        settings=settings,
        project_id="current",
    )
    pause_session(
        db,
        workspace_id=other.workspace_id,
        session_id=stale_paused.id,
        settings=settings,
    )
    long_ago = utcnow() - timedelta(days=30)
    stale.last_used_at = long_ago
    stale_paused.last_used_at = long_ago
    db.commit()

    assert reap_idle(db, settings=settings) == 2

    db.refresh(stale)
    db.refresh(stale_paused)
    db.refresh(fresh)
    assert stale.status == "killed"
    # A paused session is reaped too: that is the one that costs storage.
    assert stale_paused.status == "killed"
    assert fresh.status == "running"

    # Idempotent: a second sweep finds nothing left to do.
    assert reap_idle(db, settings=settings) == 0


def test_touch_defers_the_reaper(db, workspaces):
    owner, _ = workspaces
    settings = _settings(sandbox_session_idle_days=7)
    created = ensure_session(
        db, workspace_id=owner.workspace_id, user_id=owner.user_id, settings=settings
    )
    created.last_used_at = utcnow() - timedelta(days=30)
    db.commit()
    # ensure_session touches on reuse, which is what keeps a session that is
    # actively being worked in from being killed mid-analysis.
    ensure_session(
        db, workspace_id=owner.workspace_id, user_id=owner.user_id, settings=settings
    )
    assert reap_idle(db, settings=settings) == 0


# --- clip ----------------------------------------------------------------


def test_clip_respects_the_byte_budget():
    assert clip("hello", 64) == ("hello", False)
    clipped, truncated = clip("x" * 100, 10)
    assert (clipped, truncated) == ("x" * 10, True)
    assert clip("", 0) == ("", False)
    assert clip("x", 0) == ("", True)


def test_clip_never_splits_a_codepoint():
    """A half-written "é" raises on decode three layers away from here."""
    clipped, truncated = clip("é" * 10, 5)
    assert truncated is True
    assert clipped == "éé"  # 4 bytes: the 5th byte would be half a codepoint
    assert len(clipped.encode("utf-8")) <= 5
    # A multi-byte emoji at the boundary is dropped whole, not halved.
    clipped, _ = clip("ab🙂cd", 4)
    assert clipped == "ab"


def test_handle_for_only_addresses_a_live_machine(db, workspaces):
    owner, _ = workspaces
    settings = _settings()
    created = ensure_session(
        db, workspace_id=owner.workspace_id, user_id=owner.user_id, settings=settings
    )
    assert handle_for(created).external_id == created.external_id

    kill_session(
        db, workspace_id=owner.workspace_id, session_id=created.id, settings=settings
    )
    db.refresh(created)
    with pytest.raises(SandboxError):
        handle_for(created)
