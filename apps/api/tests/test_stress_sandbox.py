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
from pathlib import Path

import pytest
from conftest import create_identity

from app.database import SessionLocal
from app.models import SandboxSession
from app.services.sandbox import local_exec
from app.services.sandbox import session as session_service
from app.services.sandbox.fake import FakeProvider
from app.services.sandbox.subprocess_provider import SubprocessProvider
from app.services.sandbox.types import SandboxError, SandboxSpec

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
        workdir=workdir, env={"JASMINE_SANDBOX": "1"}, python_binary=sys.executable
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


def test_overlapping_session_creation_cannot_exceed_the_workspace_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two creates that overlap in the window `ensure_session` leaves open.

    The barrier sits inside the provider's `create`, which *used* to be the
    window: `ensure_session` counted, called the provider, then inserted, so
    both callers could pass the quota check before either inserted.

    Claim-then-create closed that window, and closing it is what makes the
    rendezvous impossible — the loser is now refused before the provider is ever
    reached, so only one party arrives at the barrier. So the barrier is a
    *probe*, not a synchronisation primitive: it opens the widest overlap the
    implementation still allows, and a broken barrier is the fix working rather
    than a failure. Treating it as fatal made this test fail roughly one run in
    three and cost the suite 30 seconds of dead waiting each time, which is how
    a real assertion gets re-run until it is green and eventually deleted.

    The invariant is asserted below, and it is the same one either way: two
    overlapping creates against a limit of one leave exactly one live session.
    """
    identity = create_identity()
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
    assert len(live) <= 1, (
        f"{len(live)} live sandbox sessions survived a quota of 1 — "
        "ensure_session counts and then creates with nothing serialising the two "
        "(session.py:166 vs session.py:227)"
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
