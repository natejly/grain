"""Adversarial stress on the sandbox, from the position of hostile code inside it.

`test_sandbox_local.py` proves the paths a *caller* names cannot leave the
session. This file attacks the other direction: what the code running inside the
session can do to the host, and what a hostile *result* — hostile bytes, hostile
volume, hostile timing — does on the way back out.

The container driver is the one that is meant to be a boundary. Its confinement
is docker's (`--network none`, `--read-only`, `--user 65534`, `--cap-drop ALL`),
but the *harvest* that follows every execution runs on the host, in the API
process, over the bind-mounted session directory. So every host-side traversal
in `local_exec` is reachable by a process the container flags were supposed to
have contained, and that is the surface these tests aim at.

Nothing here needs Docker, a provider, or the network: `snapshot`,
`harvest_artifacts`, `session_root` and `LocalProvider._resolve` are pure host
functions over a directory, which is exactly why they can be attacked directly.
"""
from __future__ import annotations

import os
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import create_identity
from sqlalchemy import event

from app.clock import utcnow
from app.database import SessionLocal
from app.models import SandboxSession
from app.services.sandbox import local_exec
from app.services.sandbox import session as session_service
from app.services.sandbox.fake import FakeProvider
from app.services.sandbox.subprocess_provider import SubprocessProvider
from app.services.sandbox.types import SandboxError, SandboxQuotaError, SandboxSpec

SPEC = SandboxSpec(workspace_id="stress")


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "sandboxes"
    root.mkdir()
    return root


@pytest.fixture
def local(workdir: Path) -> SubprocessProvider:
    import sys

    return SubprocessProvider(
        workdir=workdir, env={"GRAIN_SANDBOX": "1"}, python_binary=sys.executable
    )


def _session(local: SubprocessProvider, workdir: Path):
    handle = local.create(SPEC)
    return handle, local_exec.session_root(workdir, handle.external_id)


# --------------------------------------------------------------------------
# Symlinks: the harvest is the hole the file operations are not


def test_read_file_refuses_a_symlink_that_points_out_of_the_session(
    local: SubprocessProvider, workdir: Path, tmp_path: Path
) -> None:
    """The baseline the harvest test below is measured against.

    `_resolve` calls `Path.resolve()`, which follows symlinks, and then re-checks
    containment against the resolved root — so a link planted inside the session
    is refused rather than followed. Removing the containment check in
    `local_exec.LocalProvider._resolve` makes this test read the host file.
    """
    secret = tmp_path / "outside" / "id_rsa"
    secret.parent.mkdir()
    secret.write_bytes(b"HOST PRIVATE KEY")

    handle, root = _session(local, workdir)
    (root / "escape.txt").symlink_to(secret)

    with pytest.raises(SandboxError):
        local.read_file(handle, "escape.txt")


# Fixed: harvest and listing now reject symlinks (local_exec._is_contained_regular_file).
def test_harvest_must_not_follow_a_symlink_out_of_the_session(
    local: SubprocessProvider, workdir: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "outside" / "host-secret.png"
    secret.parent.mkdir()
    secret.write_bytes(b"HOST SECRET BYTES")

    _handle, root = _session(local, workdir)
    before = local_exec.snapshot(root)
    # The name is what the glob matches on; the target is what gets read.
    (root / "chart.png").symlink_to(secret)

    artifacts, _dropped = local_exec.harvest_artifacts(root, before)
    payloads = [artifact.data for artifact in artifacts]
    assert not any("SE9TVCBTRUNSRVQ" in payload for payload in payloads), (
        "the harvest read a host file through a symlink planted in the session"
    )


# Fixed: harvest and listing now reject symlinks (local_exec._is_contained_regular_file).
def test_list_files_must_not_report_the_size_of_a_host_file(
    local: SubprocessProvider, workdir: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "outside" / "big"
    secret.parent.mkdir()
    secret.write_bytes(b"x" * 4096)

    handle, root = _session(local, workdir)
    (root / "probe.txt").symlink_to(secret)

    sizes = {entry.name: entry.size for entry in local.list_files(handle, "/")}
    assert sizes.get("probe.txt") != 4096, (
        "list_files leaked the size of a file outside the session"
    )


# --------------------------------------------------------------------------
# Path mapping under adversarial input


@pytest.mark.parametrize(
    "hostile",
    [
        "/etc/passwd",
        "//etc/passwd",
        "///../../etc/passwd",
        "/workspace/../../../etc/passwd",
        "/home/user/../../../../etc/passwd",
        "workspace/home/user/../../../../etc/shadow",
        "a/../../../../../../../../etc/passwd",
        "./././../out.txt",
        "/workspace/./../../out.txt",
        "\\..\\..\\etc\\passwd",
        "." * 200 + "/out.txt",
        "sub/" * 200 + "out.txt",
        "‮/etc/passwd",
        "﻿../out.txt",
    ],
)
def test_every_resolved_path_stays_inside_the_session(
    local: SubprocessProvider, workdir: Path, hostile: str
) -> None:
    """Whatever a hostile path does, the host path it names is inside the session.

    Two legal answers only: refuse with `SandboxError`, or resolve to something
    under the session root. Anything else — an escape, or an exception type the
    callers do not catch — is a finding. Deleting the containment check in
    `_resolve` fails this on `/workspace/../../../etc/passwd`.
    """
    handle, root = _session(local, workdir)
    try:
        target = local._resolve(root, hostile)
    except SandboxError:
        return
    assert target == root or str(target).startswith(str(root) + os.sep), (
        f"{hostile!r} resolved to {target}, outside {root}"
    )


def test_absolute_host_paths_are_confined_rather_than_honoured(
    local: SubprocessProvider, workdir: Path
) -> None:
    """`/etc/passwd` is not refused — `lstrip('/')` turns it into a relative
    path — so what stops it is confinement, not rejection. Pin that, because a
    reader of `_resolve` could reasonably believe absolute paths are refused and
    "silently writes <session>/etc/passwd" is a different contract."""
    handle, root = _session(local, workdir)
    local.write_files(handle, {"/etc/passwd": b"not the host one"})
    assert (root / "etc" / "passwd").read_bytes() == b"not the host one"
    assert local.read_file(handle, "/etc/passwd") == b"not the host one"


def test_the_prefix_mapping_needs_a_trailing_slash_to_apply(
    local: SubprocessProvider, workdir: Path
) -> None:
    """`/workspace/x` maps onto the session, but bare `/workspace` does not.

    Documented rather than asserted-as-correct: `tools._reported_size` builds a
    *parent* path (`/home/user`) and hands it to `list_files`, which is why the
    pre-read size guard never fires on these drivers. See the report entry for
    `tools.py:565`.
    """
    handle, root = _session(local, workdir)
    assert local._resolve(root, "/workspace/a.txt") == root / "a.txt"
    assert local._resolve(root, "/home/user/a.txt") == root / "a.txt"
    assert local._resolve(root, "/workspace") == root / "workspace"
    assert local._resolve(root, "/home/user") == root / "home" / "user"


def test_a_session_id_with_a_separator_is_refused(workdir: Path) -> None:
    """The screen `session_root` does apply, held in place.

    Deleting any clause of the `"/" in external_id or "\\\\" in external_id or
    external_id.startswith(".")` guard at local_exec.py:68 fails this.
    """
    for bad in ("box\x00/../..", "../escape", "..", ".hidden", "", "a\\b"):
        with pytest.raises(SandboxError):
            local_exec.session_root(workdir, bad)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: session_root (local_exec.py:68-73) screens for '/', '\\\\' and a "
        "leading dot, then calls Path.resolve(), which raises a bare ValueError "
        "on an embedded NUL. ValueError is not SandboxError, so it escapes every "
        "`except SandboxError` in tools.py and api/sandbox.py and surfaces as a "
        "500 instead of a refusal. external_id is driver-generated today, but it "
        "is a plain unconstrained string column that is read back out of the "
        "database and fed to this function on every later request. Remove the "
        "xfail when the guard rejects control characters."
    ),
)
@pytest.mark.parametrize("bad", ["a\x00b", "\x00"])
def test_a_session_id_holding_a_null_byte_is_refused_as_a_sandbox_error(
    workdir: Path, bad: str
) -> None:
    with pytest.raises(SandboxError):
        local_exec.session_root(workdir, bad)


# --------------------------------------------------------------------------
# Hostile output


def test_a_line_with_no_newline_and_hostile_bytes_survives_capture(
    local: SubprocessProvider, workdir: Path
) -> None:
    """Invalid UTF-8, ANSI escapes, NULs and a long unterminated line.

    The point is that none of it raises and none of it is silently swallowed:
    `run_process` reads with `errors="replace"`, so the bytes come back as
    U+FFFD rather than killing the execution.
    """
    handle, _root = _session(local, workdir)
    source = (
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\xfe invalid ')\n"
        "sys.stdout.buffer.write(b'\\x1b[31mANSI\\x1b[0m ')\n"
        "sys.stdout.buffer.write(b'nul\\x00byte ')\n"
        "sys.stdout.buffer.write(b'A' * 200000)\n"  # no trailing newline
        "sys.stdout.flush()\n"
    )
    result = local.run_code(handle, source, language="python", timeout=30.0)
    assert result.exit_code == 0
    assert "�" in result.stdout, "invalid bytes should be replaced, not dropped"
    assert "\x1b[31m" in result.stdout, "ANSI escapes reach the caller unfiltered"
    assert "A" * 200000 in result.stdout


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT: ExecResult.truncated is hardcoded False at local_exec.py:299 "
        "and is never set anywhere in app/, so `SandboxRunOut.truncated` "
        "(api/sandbox.py:327) always says False and the model-facing 'output was "
        "clipped' branch (tools.py:267) is unreachable — while the stored stdout "
        "*is* clipped by session.clip(). Meanwhile run_process applies no cap at "
        "all during capture, so the full output is buffered in the API process "
        "first. Remove the xfail when the drivers set truncated."
    ),
)
def test_output_over_the_cap_is_reported_as_truncated(
    local: SubprocessProvider, workdir: Path
) -> None:
    handle, _root = _session(local, workdir)
    result = local.run_code(
        handle, "print('B' * 2_000_000)", language="python", timeout=30.0
    )
    assert result.truncated, "a multi-megabyte stdout was not flagged as truncated"


def test_clip_cuts_on_a_byte_budget_without_splitting_a_codepoint() -> None:
    """The storage clip is the only cap that actually applies. Multibyte text
    must not come back as a `UnicodeDecodeError` or a lone surrogate."""
    text = "é" * 100  # 2 bytes each
    clipped, was_clipped = session_service.clip(text, 51)
    assert was_clipped
    assert clipped == "é" * 25
    assert clipped.encode("utf-8")  # round-trips


def test_clip_of_zero_reports_everything_as_clipped() -> None:
    assert session_service.clip("anything", 0) == ("", True)
    assert session_service.clip("", 0) == ("", False)


# --------------------------------------------------------------------------
# Quota


@pytest.fixture
def quota_tenant():
    """A tenant of its own, whose sandbox rows are removed when the test ends.

    `FakeProvider` hands out deterministic external ids (`fake-1`) and
    `(provider, external_id)` is unique table-wide, so a live row left behind by
    one quota test collides with the very first create of the next one — in this
    file and in every later module that runs on the fake provider.
    """
    identity = create_identity()
    yield identity
    db = SessionLocal()
    try:
        db.query(SandboxSession).filter(
            SandboxSession.workspace_id == identity.workspace_id
        ).delete()
        db.commit()
    finally:
        db.close()


def test_overlapping_session_creation_cannot_exceed_the_workspace_quota(
    quota_tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two real threads, overlapping wherever the implementation lets them.

    This one races; the test below it does not, and the pair is deliberate. A
    thread test can only ever sample the interleavings the scheduler happens to
    produce, so it is kept for what it does prove — that the invariant survives
    genuine concurrency, on whatever schedule the machine feels like — and the
    exact interleaving that broke the quota is pinned deterministically next
    door. Neither is a substitute for the other: this one would have caught the
    defect roughly four runs in ten, which is indistinguishable from a flake and
    is precisely how it survived being diagnosed twice.

    The barrier is a *probe*, not a synchronisation primitive. It sits inside
    `provider.create`, and the loser is refused before it ever reaches the
    provider, so on a correct implementation only one party arrives and the
    barrier breaks. A broken barrier is therefore the fix working. What is
    asserted is the invariant itself: two overlapping creates against a limit of
    one leave exactly one live session.
    """
    identity = quota_tenant
    settings = _sandbox_settings(limit=1)
    barrier = threading.Barrier(2, timeout=2)

    class RendezvousProvider(FakeProvider):
        def create(self, spec):  # type: ignore[override]
            handle = super().create(spec)
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                # Only one caller reached the provider — the quota refused the
                # other before it got here, which is the behaviour under test.
                pass
            return handle

    provider = RendezvousProvider()
    monkeypatch.setattr(session_service, "get_provider", lambda _settings: provider)

    def attempt(index: int) -> None:
        db = SessionLocal()
        try:
            session_service.ensure_session(
                db,
                workspace_id=identity.workspace_id,
                user_id=identity.user_id,
                settings=settings,
                project_id=f"project-{index}",
            )
        except Exception:  # noqa: BLE001 - a refusal is one of the outcomes
            pass
        finally:
            db.close()

    threads = [threading.Thread(target=attempt, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads), "the rendezvous hung"

    db = SessionLocal()
    try:
        live = [
            row
            for row in db.query(SandboxSession)
            .filter(SandboxSession.workspace_id == identity.workspace_id)
            .all()
            if row.status in session_service.LIVE_STATUSES
        ]
    finally:
        db.close()
    assert len(live) == 1, (
        f"{len(live)} live sandbox sessions survived a quota of 1 — the slot the "
        "winner holds did not stop the loser from taking one too "
        "(session._claim_a_slot)"
    )


def test_a_claim_that_commits_out_of_order_cannot_take_a_second_slot(
    quota_tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The race, constructed rather than waited for.

    Two overlapping creates both insert a claim row and both commit it. What
    decides the outcome is not who *started* first but the relationship between
    two orders: the order the rows carry (their `created_at`) and the order they
    reach the table. Those two can invert, on either engine, because the ordering
    key is computed in the process — while the parameters of the INSERT are being
    built — and the row becomes visible only at COMMIT, a round trip later:

        A: created_at := t1   B: created_at := t0 (t0 < t1)
        A: INSERT, COMMIT     …
        A: counts rows ahead of t1 → none committed → admitted
                              B: INSERT, COMMIT
                              B: counts rows ahead of t0 → A sorts *after* t0
                                 → none ahead → admitted

    Both are admitted, both reach the provider, and the workspace holds two live
    machines against a quota of one. Under SQLite the two writes are serialised
    and it happens anyway: serialising the writes does not order the timestamps
    they were stamped with before the lock was taken.

    So this test does not race for that interleaving — it states it. The
    `before_insert` hook stamps the winner with the later key and the loser with
    the earlier one, and the calls then run one after another, which is the
    committed sequence the race produces and the only sequence SQLite can
    produce. No barrier, no sleep, no coin flip: the second call is either
    refused before `provider.create` or the quota is broken.
    """
    identity = quota_tenant
    settings = _sandbox_settings(limit=1)
    provider = FakeProvider()
    monkeypatch.setattr(session_service, "get_provider", lambda _settings: provider)

    # Both keys are in the past, so a `starting` claim is inside CLAIM_TTL and the
    # ordering under test is the only thing that is unusual about them.
    now = utcnow()
    keys = [now - timedelta(seconds=1), now - timedelta(seconds=2)]

    def _stamp(_mapper, _connection, target: SandboxSession) -> None:
        if keys:
            target.created_at = keys.pop(0)

    event.listen(SandboxSession, "before_insert", _stamp)
    try:
        db = SessionLocal()
        try:
            first = session_service.ensure_session(
                db,
                workspace_id=identity.workspace_id,
                user_id=identity.user_id,
                settings=settings,
                project_id="winner",
            )
            assert first.status == "running"

            # The loser's claim sorts *ahead* of the winner's and commits second.
            with pytest.raises(SandboxQuotaError):
                session_service.ensure_session(
                    db,
                    workspace_id=identity.workspace_id,
                    user_id=identity.user_id,
                    settings=settings,
                    project_id="loser",
                )
        finally:
            db.close()
    finally:
        event.remove(SandboxSession, "before_insert", _stamp)

    assert [call for call, _args in provider.calls if call == "create"] == ["create"], (
        "the over-limit machine was created at the provider before being refused"
    )

    db = SessionLocal()
    try:
        live = [
            row
            for row in db.query(SandboxSession)
            .filter(SandboxSession.workspace_id == identity.workspace_id)
            .all()
            if row.status in session_service.LIVE_STATUSES
        ]
    finally:
        db.close()
    assert len(live) == 1, (
        f"{len(live)} live sandbox sessions survived a quota of 1 — a claim that "
        "commits out of created_at order is admitted alongside the one already "
        "holding the slot"
    )


def test_a_slot_taken_between_the_read_and_the_insert_refuses_the_second_holder(
    quota_tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read is advisory. The INSERT is the quota — so break the read.

    `ensure_session` looks up which slots are free and then inserts a claim on
    one of them, and those are two statements: a racer can commit the slot in
    between, which makes the answer the first statement gave already false by the
    time the second runs. That gap cannot be closed by looking harder — any check
    before a write is a check about the past — so what has to hold is that the
    write itself is refused.

    The gap is opened here rather than raced for. A `before_flush` listener fires
    once, after `ensure_session` has decided slot 0 is free and before its INSERT
    reaches the database, and commits a rival session into slot 0 from another
    connection. That is precisely the interleaving two processes produce, and it
    is the one no amount of counting survives.

    This is the test that holds the unique index in place. The deterministic race
    test above passes without it — the loser is turned away by the count long
    before the constraint is consulted — which is exactly the kind of gap that
    lets a "load-bearing" mutation check come back green over a broken quota.
    """
    identity = quota_tenant
    settings = _sandbox_settings(limit=1)
    provider = FakeProvider()
    monkeypatch.setattr(session_service, "get_provider", lambda _settings: provider)

    db = SessionLocal()
    fired: list[bool] = []

    def _rival_commits_first(session, _flush_context, _instances) -> None:
        if fired or not any(isinstance(obj, SandboxSession) for obj in session.new):
            return
        fired.append(True)
        rival = SessionLocal()
        try:
            rival.add(
                SandboxSession(
                    workspace_id=identity.workspace_id,
                    created_by=identity.user_id,
                    provider="fake",
                    external_id="rival-box",
                    status="running",
                    slot_index=0,
                )
            )
            rival.commit()
        finally:
            rival.close()

    event.listen(db, "before_flush", _rival_commits_first)
    try:
        with pytest.raises(SandboxQuotaError):
            session_service.ensure_session(
                db,
                workspace_id=identity.workspace_id,
                user_id=identity.user_id,
                settings=settings,
                project_id="loser",
            )
    finally:
        event.remove(db, "before_flush", _rival_commits_first)
        db.close()

    assert fired, "the rival never committed — the test did not open the window"
    assert provider.calls == [], (
        "the loser reached the provider: the machine that breaks the limit was "
        "created before anything refused it"
    )

    db = SessionLocal()
    try:
        live = [
            row
            for row in db.query(SandboxSession)
            .filter(SandboxSession.workspace_id == identity.workspace_id)
            .all()
            if row.status in session_service.LIVE_STATUSES
        ]
    finally:
        db.close()
    assert [row.external_id for row in live] == ["rival-box"], (
        f"{len(live)} live sandbox sessions survived a quota of 1 — the claim was "
        "admitted onto a slot another row had already committed"
    )


def _sandbox_settings(*, limit: int):
    from app.config import get_settings

    return get_settings().model_copy(
        update={
            "sandbox_enabled": True,
            "sandbox_provider": "fake",
            "sandbox_max_concurrent_per_workspace": limit,
        }
    )
