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

import os
import shlex
import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Mapping, Sequence

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

#: The uid/gid the container runs as. `nobody`, present in every base image.
SANDBOX_USER = "65534:65534"

#: Mode for the session directory on the *host*. A bind mount carries host
#: ownership into the container, and the API process is not uid 65534 (nor
#: necessarily root, so chown is not available), which leaves widening the mode
#: as the only portable way to let the sandbox write its own workspace. The
#: exposure is to other local users on the API host, who are already able to
#: reach the Docker socket this driver drives — root-equivalent — so this grants
#: nothing that was not already there.
SESSION_MODE = 0o777


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
        # Resolved against the parent's PATH once, here, so every later exec
        # names an absolute binary. Falling back to the bare name keeps the
        # error legible ("runtime not found: docker") instead of turning a
        # missing Docker into a confusing None.
        self._docker = shutil.which(docker_binary) or docker_binary
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
        local_exec.ensure_session_root(self._workdir, external_id, mode=SESSION_MODE)
        # Persist the session's injected secrets beside its directory (0600, and
        # outside the bind mount) so each throwaway `docker run` can pass them in
        # via -e without the values ever being readable from inside the box.
        local_exec.write_session_env(self._workdir, external_id, spec.env)
        return SandboxHandle(provider=self.name, external_id=external_id)

    def _cli_env(self) -> Dict[str, str]:
        """Environment for the **docker CLI**, which is not the sandbox's.

        Worth being unambiguous about, because it looks like the thing this
        subsystem spends its time refusing to do: the sandbox's environment is
        built by `policy.sandbox_env` and travels via `-e`, and nothing here
        reaches generated code. This dict configures the client that launches
        the container.

        It has to pass through the caller's Docker configuration, and both
        halves were learned by getting them wrong. A hardcoded
        `/usr/local/bin:/usr/bin:/bin` cannot see Homebrew's `/opt/homebrew/bin`,
        so every command failed with "runtime not found" on an arm64 Mac. And
        forcing `HOME=/tmp` hides `~/.docker/config.json`, which is where the
        active context lives — so Colima, Rancher, or any rootless setup that
        selects a non-default socket silently falls back to
        `/var/run/docker.sock` and fails to connect.
        """
        env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
        for key in (
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_CERT_PATH",
            "DOCKER_TLS_VERIFY",
            "XDG_RUNTIME_DIR",
        ):
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    def _preflight(self) -> None:
        probe = local_exec.run_process(
            [self._docker, "image", "inspect", self._image],
            cwd=self._workdir,
            env=self._cli_env(),
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
        root = local_exec.ensure_session_root(
            self._workdir, handle.external_id, mode=SESSION_MODE
        )
        # Written to the bind mount rather than passed as `python -c`, so the
        # code is not in the process table and a traceback carries real line
        # numbers against a real file.
        (root / ".grain_exec.py").write_text(code, encoding="utf-8")
        env = self._process_env(handle)
        return self._run(
            root, ["python3", f"{MOUNT}/.grain_exec.py"], timeout, on_output, env
        )

    def run_command(
        self,
        handle: SandboxHandle,
        command: str,
        *,
        timeout: float = 120.0,
        cwd: str = "",
        on_output: OutputSink = None,
    ) -> ExecResult:
        root = local_exec.ensure_session_root(
            self._workdir, handle.external_id, mode=SESSION_MODE
        )
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise SandboxError(f"could not parse command: {exc}") from exc
        if not argv:
            raise SandboxError("empty command")
        env = self._process_env(handle)
        return self._run(root, argv, timeout, on_output, env)

    def _docker_argv(
        self, root: Path, inner: Sequence[str], name: str, env: Mapping[str, str]
    ) -> List[str]:
        argv = [
            self._docker,
            "run",
            "--rm",
            # Named so a timeout has something to kill. Killing the `docker` CLI
            # does not stop the container: the daemon owns it, and the CLI is
            # only a client watching it. Without this the module's "a crashed API
            # leaks no compute" promise holds for the API but not for a run that
            # merely overruns, which would sit there burning --cpus forever.
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            # noexec matters: /tmp is the one writable place in the image, so
            # without it a run could stage a binary there and execute it.
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=256m",
            "--user",
            SANDBOX_USER,
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
        for key, value in sorted(env.items()):
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
        env: Mapping[str, str],
    ) -> ExecResult:
        before = local_exec.snapshot(root)
        name = f"grain-{uuid.uuid4().hex[:16]}"
        result = local_exec.run_process(
            self._docker_argv(root, inner, name, env),
            cwd=self._workdir,
            env=self._cli_env(),
            # Give docker a moment past the inner budget to tear down, so a
            # timeout is attributed to the user's code rather than to the runtime.
            timeout=timeout + 15.0,
            on_output=on_output,
        )
        if result.error:
            self._kill_container(name)
        artifacts, dropped = local_exec.harvest_artifacts(root, before)
        note = result.stderr
        if dropped:
            note = f"{note}\n[{dropped} more artifact(s) not returned]".strip()
        return replace(result, artifacts=artifacts, stderr=note)

    def _kill_container(self, name: str) -> None:
        """Stop a container the run did not finish with.

        Best effort by design, and its failures are silent: the container has
        usually exited on its own by the time we get here (`docker kill` then
        reports "no such container"), and the execution already has a result to
        return. Reporting a teardown problem as though it were the user's would
        replace a legible "timed out" with noise about the runtime.

        `_cli_env` for the same reason the run uses it: a kill sent to the
        default socket, when the run went to a Colima or rootless context, would
        report success against a container that is still going.
        """
        try:
            local_exec.run_process(
                [self._docker, "kill", name],
                cwd=self._workdir,
                env=self._cli_env(),
                timeout=30.0,
            )
        except SandboxError:
            return
