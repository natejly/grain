"""The deployment driver. One throwaway container per execution, no network.

This is the one that is actually a boundary, and the flags below are the whole
argument, so they are worth reading rather than skimming:

    --network none          no route, no DNS, no loopback to the host. This is
                            what makes prompt-injected exfiltration a non-issue
                            rather than a mitigation: there is nowhere to send.
    --read-only             the image filesystem is immutable; only the bind
                            mount and an explicit tmpfs are writable.
    --cap-drop ALL          no CAP_NET_RAW, no CAP_SYS_ADMIN, nothing.
    --security-opt no-new-privileges   setuid binaries cannot regain what
                            --cap-drop removed.
    --user 65534:65534      nobody. Combined with --read-only there is no path
                            to writing anything the next container will execute.
    --pids-limit            fork bombs terminate instead of the host doing so.
    --memory/--cpus         a runaway allocation is the container's problem.

There is no long-lived container. Each execution is `docker run --rm`, and the
session is the bind-mounted directory — so a crashed API leaks no compute, there
is nothing to reap, and the "persistent workspace" promise is kept by the
filesystem rather than by a machine somebody has to keep alive. The cost is that
interpreter state does not survive between executions; ADR 0005 records that.

Egress policy `allowlist` is deliberately unimplemented here. Docker has no
native per-host egress filter, and the honest options are an HTTP proxy or
iptables in a sidecar — both of which would be a second security surface built
to serve a mode this product does not need now that packages are pre-baked.
Asking for it raises rather than silently downgrading to `open`, because
silently widening egress is precisely the bug that would matter.
"""
from __future__ import annotations

import shlex
import uuid
from dataclasses import replace
from pathlib import Path
from typing import List, Mapping, Sequence

from . import local_exec
from .types import (
    ExecResult,
    Language,
    OutputSink,
    SandboxError,
    SandboxHandle,
    SandboxSpec,
)

#: Where the session directory appears inside the container.
MOUNT = "/workspace"


class ContainerProvider(local_exec.LocalProvider):
    name = "container"

    def __init__(
        self,
        *,
        workdir: Path,
        env: Mapping[str, str],
        image: str,
        docker_binary: str = "docker",
        memory_mb: int = 2048,
        cpus: float = 2.0,
        pids_limit: int = 256,
    ) -> None:
        super().__init__(workdir=workdir, env=env)
        self._image = image
        self._docker = docker_binary
        self._memory_mb = memory_mb
        self._cpus = cpus
        self._pids = pids_limit

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        if spec.network == "allowlist":
            raise SandboxError(
                "the container driver supports network policy 'none' or 'open'; "
                "use the e2b driver for a host allowlist"
            )
        # Check the runtime here rather than on first execution. A session that
        # is created successfully and then fails every run is the shape of bug
        # that gets reported as "the sandbox is broken" with no further detail.
        self._preflight()
        external_id = f"box-{uuid.uuid4().hex[:16]}"
        local_exec.ensure_session_root(self._workdir, external_id)
        return SandboxHandle(provider=self.name, external_id=external_id)

    def _preflight(self) -> None:
        probe = local_exec.run_process(
            [self._docker, "image", "inspect", self._image],
            cwd=self._workdir,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
            timeout=30.0,
        )
        if probe.exit_code != 0:
            raise SandboxError(
                f"sandbox image '{self._image}' is not available to {self._docker}. "
                "Build it with `make sandbox-image`."
            )

    def run_code(
        self,
        handle: SandboxHandle,
        code: str,
        *,
        language: Language = "python",
        timeout: float = 120.0,
        on_output: OutputSink = None,
    ) -> ExecResult:
        if language == "bash":
            return self.run_command(handle, code, timeout=timeout, on_output=on_output)
        if language != "python":
            raise SandboxError(f"the {self.name} driver runs python, not {language}")
        root = local_exec.ensure_session_root(self._workdir, handle.external_id)
        # Written to the bind mount rather than passed as `python -c`, so the
        # code is not in the process table and a traceback carries real line
        # numbers against a real file.
        (root / ".jasmine_exec.py").write_text(code, encoding="utf-8")
        return self._run(root, ["python3", f"{MOUNT}/.jasmine_exec.py"], timeout, on_output)

    def run_command(
        self,
        handle: SandboxHandle,
        command: str,
        *,
        timeout: float = 120.0,
        cwd: str = "",
        on_output: OutputSink = None,
    ) -> ExecResult:
        root = local_exec.ensure_session_root(self._workdir, handle.external_id)
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise SandboxError(f"could not parse command: {exc}") from exc
        if not argv:
            raise SandboxError("empty command")
        return self._run(root, argv, timeout, on_output)

    def _docker_argv(self, root: Path, inner: Sequence[str]) -> List[str]:
        argv = [
            self._docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            # noexec matters: /tmp is the one writable place in the image, so
            # without it a run could stage a binary there and execute it.
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(self._pids),
            "--memory",
            f"{self._memory_mb}m",
            # Equal to --memory so the container cannot swap its way past the
            # limit it was given.
            "--memory-swap",
            f"{self._memory_mb}m",
            "--cpus",
            str(self._cpus),
            "-v",
            f"{root}:{MOUNT}:rw",
            "-w",
            MOUNT,
        ]
        for key, value in sorted(self._env.items()):
            argv += ["-e", f"{key}={value}"]
        argv.append(self._image)
        argv.extend(inner)
        return argv

    def _run(
        self,
        root: Path,
        inner: Sequence[str],
        timeout: float,
        on_output: OutputSink,
    ) -> ExecResult:
        before = local_exec.snapshot(root)
        result = local_exec.run_process(
            self._docker_argv(root, inner),
            cwd=self._workdir,
            # The docker CLI itself needs a PATH and nothing else. The sandbox's
            # own environment travels via -e, so this dict is not the one
            # generated code sees.
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp"},
            # Give docker a moment past the inner budget to tear down, so a
            # timeout is attributed to the user's code rather than to the runtime.
            timeout=timeout + 15.0,
            on_output=on_output,
        )
        artifacts, dropped = local_exec.harvest_artifacts(root, before)
        note = result.stderr
        if dropped:
            note = f"{note}\n[{dropped} more artifact(s) not returned]".strip()
        return replace(result, artifacts=artifacts, stderr=note)
