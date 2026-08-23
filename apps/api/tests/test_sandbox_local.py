"""The subprocess and container drivers.

The container tests deliberately never run Docker. What matters about that driver
is the argv it builds — `--network none` is the entire security argument, and a
test that needs a daemon to check it is a test that does not run in CI and
therefore does not stop the flag being dropped. So the argv is asserted directly,
flag by flag, and each assertion names what the flag is for.

The subprocess tests do execute, because that driver's whole job is to execute
and because the properties worth checking (a scrubbed environment, a timeout that
actually kills, no shell) are only observable by running something.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.services.sandbox import local_exec
from app.services.sandbox.container_provider import ContainerProvider
from app.services.sandbox.subprocess_provider import SubprocessProvider
from app.services.sandbox.types import SandboxError, SandboxHandle, SandboxSpec

SPEC = SandboxSpec(workspace_id="w1")


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "sandboxes"
    root.mkdir()
    return root


@pytest.fixture
def local(workdir: Path) -> SubprocessProvider:
    import sys

    return SubprocessProvider(
        workdir=workdir,
        env={"GRAIN_SANDBOX": "1", "PYTHONUNBUFFERED": "1"},
        python_binary=sys.executable,
    )


# --- session paths ------------------------------------------------------


@pytest.mark.parametrize("bad", ["../escape", "a/b", "..", ".hidden", "", "a\\b"])
def test_session_root_refuses_anything_that_could_leave_the_workdir(
    workdir: Path, bad: str
) -> None:
    with pytest.raises(SandboxError):
        local_exec.session_root(workdir, bad)


def test_sandbox_relative_paths_cannot_escape_the_session(local: SubprocessProvider) -> None:
    handle = local.create(SPEC)
    local.write_files(handle, {"data.txt": b"inside"})
    assert local.read_file(handle, "data.txt") == b"inside"
    # Generated code names these paths, so they are attacker-controlled input.
    for bad in ("../../../etc/passwd", "/etc/passwd", "a/../../../../etc/passwd"):
        with pytest.raises(SandboxError):
            local.read_file(handle, bad)


def test_workspace_and_home_prefixes_map_onto_the_session(local: SubprocessProvider) -> None:
    """The two paths generated code actually writes. E2B's cwd is /home/user and
    the container's is /workspace, so both have to land in the same place or code
    written against one driver silently writes nothing on the other."""
    handle = local.create(SPEC)
    local.write_files(handle, {"/workspace/a.txt": b"a", "/home/user/b.txt": b"b"})
    assert local.read_file(handle, "a.txt") == b"a"
    assert local.read_file(handle, "b.txt") == b"b"


# --- artifact harvesting ------------------------------------------------


def test_harvest_returns_only_what_the_execution_touched(tmp_path: Path) -> None:
    (tmp_path / "old.png").write_bytes(b"\x89PNG" + b"0" * 16)
    before = local_exec.snapshot(tmp_path)
    time.sleep(0.01)
    (tmp_path / "new.png").write_bytes(b"\x89PNG" + b"1" * 16)
    (tmp_path / "notes.txt").write_text("not an artifact")

    artifacts, dropped = local_exec.harvest_artifacts(tmp_path, before)

    assert dropped == 0
    assert len(artifacts) == 1, "a pre-existing file is not a result of this run"
    assert artifacts[0].kind == "png"
    assert artifacts[0].is_main


def test_rewriting_the_same_filename_is_harvested_again(tmp_path: Path) -> None:
    """Overwriting chart.png on a second run is the common case, and an
    existence check would report it once and then never again."""
    chart = tmp_path / "chart.png"
    chart.write_bytes(b"\x89PNG" + b"0" * 16)
    before = local_exec.snapshot(tmp_path)
    time.sleep(0.01)
    chart.write_bytes(b"\x89PNG" + b"2" * 16)

    artifacts, _ = local_exec.harvest_artifacts(tmp_path, before)
    assert len(artifacts) == 1


def test_too_many_figures_are_capped_and_counted(tmp_path: Path) -> None:
    before = local_exec.snapshot(tmp_path)
    for index in range(local_exec.MAX_ARTIFACTS + 5):
        (tmp_path / f"f{index}.png").write_bytes(b"\x89PNG" + bytes([index]) * 8)

    artifacts, dropped = local_exec.harvest_artifacts(tmp_path, before)

    assert len(artifacts) == local_exec.MAX_ARTIFACTS
    assert dropped == 5, "the caller must be able to say what it did not return"


def test_svg_travels_as_text_and_png_as_base64(tmp_path: Path) -> None:
    before = local_exec.snapshot(tmp_path)
    (tmp_path / "a.svg").write_text("<svg/>")
    (tmp_path / "b.png").write_bytes(b"\x89PNG\x00\x01")

    artifacts, _ = local_exec.harvest_artifacts(tmp_path, before)
    by_kind = {a.kind: a for a in artifacts}

    assert by_kind["svg"].data == "<svg/>"
    import base64

    assert base64.b64decode(by_kind["png"].data) == b"\x89PNG\x00\x01"


# --- subprocess driver --------------------------------------------------


def test_code_runs_and_stdout_comes_back(local: SubprocessProvider) -> None:
    result = local.run_code(local.create(SPEC), "print('hello')")
    assert result.stdout.strip() == "hello"
    assert result.ok


def test_a_traceback_is_a_result_not_an_exception(local: SubprocessProvider) -> None:
    """The model reads tool failures and can fix a NameError by writing better
    code. Raising here would make it retry against a machine it thinks is broken."""
    result = local.run_code(local.create(SPEC), "raise ValueError('boom')")
    assert not result.ok
    assert result.exit_code != 0
    assert "ValueError: boom" in result.stderr


def test_the_environment_is_built_not_inherited(local: SubprocessProvider) -> None:
    """The parent holds an OpenAI key, a database URL and the session secret.
    This is the assertion that they do not travel."""
    result = local.run_code(
        local.create(SPEC),
        "import os\n"
        "leaked = [k for k in os.environ if 'KEY' in k or 'DATABASE' in k or 'SECRET' in k]\n"
        "print(sorted(leaked))",
    )
    assert result.stdout.strip() == "[]"


def test_a_sessions_own_secrets_reach_its_runs(local: SubprocessProvider) -> None:
    """The other half of the previous test: the base env is scrubbed, but a
    secret the session was *created with* is exactly what "connect stuff" means,
    so it has to arrive in os.environ. The value is carried on the per-session
    sidecar, never on the frozen base every session shares."""
    handle = local.create(SandboxSpec(workspace_id="w1", env={"STRIPE_API_KEY": "sk_live_9"}))
    result = local.run_code(handle, "import os; print(os.environ.get('STRIPE_API_KEY'))")
    assert result.stdout.strip() == "sk_live_9"

    # And a session created without it does not see another session's secret.
    other = local.run_code(
        local.create(SPEC), "import os; print(os.environ.get('STRIPE_API_KEY'))"
    )
    assert other.stdout.strip() == "None"


def test_a_runaway_loop_is_killed_and_reported(local: SubprocessProvider) -> None:
    started = time.monotonic()
    result = local.run_code(local.create(SPEC), "while True: pass", timeout=2.0)
    elapsed = time.monotonic() - started

    assert "timed out" in result.error
    assert not result.ok
    assert elapsed < 20, "the caller must not block past the timeout"


def test_commands_get_no_shell(local: SubprocessProvider) -> None:
    """`;` is an argument, not a second command."""
    result = local.run_code(local.create(SPEC), "print('x')")
    assert result.ok
    out = local.run_command(local.create(SPEC), "echo hi; echo pwned")
    assert out.stdout.strip() == "hi; echo pwned"


def test_a_file_written_by_code_becomes_an_artifact(local: SubprocessProvider) -> None:
    handle = local.create(SPEC)
    result = local.run_code(
        handle, "open('chart.png','wb').write(b'\\x89PNG' + b'z'*32)"
    )
    assert [a.kind for a in result.artifacts] == ["png"]


def test_files_persist_between_executions_but_state_does_not(
    local: SubprocessProvider,
) -> None:
    """The documented behavioural difference from a notebook kernel: the session
    is a directory, so files survive and variables do not."""
    handle = local.create(SPEC)
    local.run_code(handle, "open('kept.txt','w').write('yes'); x = 41")
    survived = local.run_code(handle, "print(open('kept.txt').read())")
    assert survived.stdout.strip() == "yes"

    lost = local.run_code(handle, "print(x)")
    assert not lost.ok
    assert "NameError" in lost.stderr


def test_kill_removes_the_session_and_is_idempotent(
    local: SubprocessProvider, workdir: Path
) -> None:
    handle = local.create(SPEC)
    assert (workdir / handle.external_id).exists()
    local.kill(handle)
    assert not (workdir / handle.external_id).exists()
    local.kill(handle)  # the reaper races an explicit delete


# --- container driver ---------------------------------------------------


@pytest.fixture
def container(workdir: Path) -> ContainerProvider:
    return ContainerProvider(
        workdir=workdir, env={"GRAIN_SANDBOX": "1"}, image="grain-sandbox:test"
    )


def test_the_container_argv_carries_every_flag_the_isolation_depends_on(
    container: ContainerProvider, workdir: Path
) -> None:
    argv = container._docker_argv(
        workdir / "box-1", ["python3", "x.py"], "box-1-run", container._env
    )
    joined = " ".join(argv)

    # Named, so a timeout can kill the container rather than only the CLI that
    # is watching it. Without this an overrunning run outlives its own result.
    assert "--name" in argv
    assert argv[argv.index("--name") + 1] == "box-1-run"

    # No route out. This is the whole exfiltration story.
    assert "--network none" in joined
    # The image filesystem is immutable; only the bind mount and tmpfs are not.
    assert "--read-only" in argv
    # noexec on the one writable in-image path, so nothing can be staged there.
    assert any(a.startswith("/tmp:rw,noexec") for a in argv)
    # Not root, and no way back to it.
    assert "65534:65534" in argv
    assert "ALL" in argv and "--cap-drop" in argv
    assert "no-new-privileges" in argv
    # Runaway resource use is the container's problem, not the host's.
    assert "--pids-limit" in argv
    assert "--memory" in argv and "--cpus" in argv
    # Equal to --memory, or the container swaps past the limit it was given.
    memory = argv[argv.index("--memory") + 1]
    swap = argv[argv.index("--memory-swap") + 1]
    assert memory == swap
    # Throwaway: nothing to reap if the API dies mid-run.
    assert "--rm" in argv


def test_the_sandbox_environment_reaches_the_container_and_the_host_env_does_not(
    workdir: Path,
) -> None:
    provider = ContainerProvider(
        workdir=workdir, env={"GRAIN_SANDBOX": "1"}, image="img"
    )
    # The env is what the run actually passes: the frozen base plus this session's
    # injected secrets. A secret rides in on the same `-e` the policy env does.
    env = {**provider._env, "STRIPE_API_KEY": "sk_test_123"}
    argv = provider._docker_argv(workdir / "box-1", ["python3"], "box-1-run", env)
    assert "GRAIN_SANDBOX=1" in argv
    assert "STRIPE_API_KEY=sk_test_123" in argv


def test_an_allowlist_policy_is_refused_rather_than_quietly_widened(
    container: ContainerProvider,
) -> None:
    """Docker has no native per-host egress filter. Downgrading to `open` would
    be the bug that matters, so this raises instead."""
    with pytest.raises(SandboxError, match="allowlist|e2b"):
        container.create(SandboxSpec(workspace_id="w1", network="allowlist"))


def test_a_missing_image_fails_at_session_creation(
    container: ContainerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session that is created fine and then fails every run gets reported as
    'the sandbox is broken' with no further detail."""
    from app.services.sandbox.types import ExecResult

    monkeypatch.setattr(
        local_exec, "run_process", lambda *a, **k: ExecResult(exit_code=1)
    )
    with pytest.raises(SandboxError, match="not available|sandbox-image"):
        container.create(SPEC)


def test_a_timed_out_run_kills_the_container_not_just_the_cli(
    container: ContainerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGKILLing the `docker` CLI leaves the container running: the daemon owns
    it. Verified against a real runtime — before this, a timed-out `while True`
    kept burning --cpus after the run had already returned its result."""
    from app.services.sandbox.types import ExecResult

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        # What run_process returns when the budget expired: a result, not a raise.
        return ExecResult(exit_code=-9, error="timed out after 20s")

    monkeypatch.setattr(local_exec, "run_process", fake_run)
    container._run(Path("/tmp/box-1"), ["python3", "x.py"], 5.0, None, {})

    run_argv, kill_argv = calls[0], calls[-1]
    name = run_argv[run_argv.index("--name") + 1]
    assert kill_argv[1:] == ["kill", name], (
        "a timed-out run must kill the container it named, not only the CLI"
    )


def test_a_run_that_finished_leaves_no_kill_behind(
    container: ContainerProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kill is for timeouts only. `docker run --rm` already reaped a run that
    ended on its own, so a kill there would be a pointless second exec."""
    from app.services.sandbox.types import ExecResult

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return ExecResult(exit_code=0, stdout="done")

    monkeypatch.setattr(local_exec, "run_process", fake_run)
    container._run(Path("/tmp/box-1"), ["python3", "x.py"], 5.0, None, {})
    assert len(calls) == 1 and "kill" not in calls[0]


def test_a_missing_docker_binary_is_a_legible_error(workdir: Path) -> None:
    provider = ContainerProvider(
        workdir=workdir, env={}, image="img", docker_binary="definitely-not-docker"
    )
    with pytest.raises(SandboxError, match="not found|not available"):
        provider.create(SPEC)


def test_both_local_drivers_agree_on_the_session_layout(
    local: SubprocessProvider, container: ContainerProvider, workdir: Path
) -> None:
    """They share LocalProvider precisely so a session written by one is readable
    by the other — which is what makes switching drivers a config change."""
    handle = SandboxHandle(provider="local", external_id="box-shared")
    local_exec.ensure_session_root(workdir, handle.external_id)
    local.write_files(handle, {"shared.txt": b"same"})
    assert container.read_file(handle, "shared.txt") == b"same"
