"""The development driver. Runs generated code as this process's own user.

This is not a sandbox and nothing in this codebase calls it one. It has the
authority the API has: it can read `.env`, the SQLite database, and
`~/.aws/credentials`. It exists so a developer without a container runtime can
exercise the whole feature — tools, approval cards, artifacts, the UI — on a
laptop, and `_guard_sandbox` refuses to construct `Settings` with
`SANDBOX_PROVIDER=subprocess` outside development/test so that convenience
cannot reach a deployment by way of a forgotten variable.

What it does apply is worth having anyway, because the common failure here is an
infinite loop rather than an adversary: a scrubbed environment built from
nothing, a per-session directory as cwd, rlimits on CPU, address space, file size
and process count, no shell, and a process-group SIGKILL on timeout.

One caveat that bites in practice: the interpreter is whatever
`SANDBOX_PYTHON_BINARY` points at, defaulting to the API's own venv. That venv's
package set is *not* the container image's, so code that imports scipy here may
fail there or vice versa. `infra/sandbox/Dockerfile` is the real package policy.
"""
from __future__ import annotations

import shlex
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence

from . import local_exec
from .types import (
    ExecResult,
    Language,
    OutputSink,
    SandboxError,
    SandboxHandle,
    SandboxSpec,
)


class SubprocessProvider(local_exec.LocalProvider):
    name = "subprocess"

    def __init__(
        self,
        *,
        workdir: Path,
        env: Mapping[str, str],
        python_binary: str,
        memory_mb: int = 2048,
        pids_limit: int = 256,
    ) -> None:
        super().__init__(workdir=workdir, env=env)
        self._python = python_binary
        self._memory_mb = memory_mb
        self._pids = pids_limit

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        # A session is a directory, so creation cannot fail for provider reasons
        # and there is no machine to leak if the caller crashes before recording
        # the row. The id is random rather than the workspace id: a directory
        # name that encodes a tenant is a directory name someone will guess.
        import uuid

        external_id = f"local-{uuid.uuid4().hex[:16]}"
        local_exec.ensure_session_root(self._workdir, external_id)
        # Persist the session's injected secrets beside its directory: this driver
        # runs a fresh process per execution and holds no machine to carry them.
        local_exec.write_session_env(self._workdir, external_id, spec.env)
        return SandboxHandle(provider=self.name, external_id=external_id)

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
        script = root / ".grain_exec.py"
        script.write_text(code, encoding="utf-8")
        env = self._process_env(handle)
        return self._run([self._python, str(script)], root, timeout, on_output, env)

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
        working = self._resolve(root, cwd) if cwd else root
        try:
            argv: Sequence[str] = shlex.split(command)
        except ValueError as exc:
            raise SandboxError(f"could not parse command: {exc}") from exc
        if not argv:
            raise SandboxError("empty command")
        # shlex.split plus shell=False, so `; rm -rf ~` is an argument to
        # whatever was named rather than a second command. The driver has no
        # isolation to protect, but it should at least not add a shell to it.
        env = self._process_env(handle)
        return self._run(argv, working, timeout, on_output, env)

    def _run(
        self,
        argv: Sequence[str],
        cwd: Path,
        timeout: float,
        on_output: OutputSink,
        env: Mapping[str, str],
    ) -> ExecResult:
        before = local_exec.snapshot(cwd)
        preexec = local_exec.rlimit_preexec(
            # The CPU cap trails the wall clock so a busy loop trips the rlimit
            # first and dies with SIGXCPU, which is legible, instead of being
            # SIGKILLed by the timeout, which looks like the sandbox broke.
            cpu_seconds=max(1, int(timeout)),
            memory_mb=self._memory_mb,
            pids=self._pids,
            fsize_mb=256,
        )
        result = local_exec.run_process(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout,
            on_output=on_output,
            preexec=preexec,
        )
        artifacts, dropped = local_exec.harvest_artifacts(cwd, before)
        note = result.stderr
        if dropped:
            note = f"{note}\n[{dropped} more artifact(s) not returned]".strip()
        return replace(result, artifacts=artifacts, stderr=note)
