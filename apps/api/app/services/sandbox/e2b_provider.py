"""The E2B driver: the only module in this tree that imports the provider SDK.

Two decisions in here are load-bearing and neither is obvious from the code.

**Nothing holds a live `Sandbox` between calls.** Every method reconnects from
the handle's id. The API runs multiple worker processes, so an object cached in
worker A is a socket worker B cannot use, and the session that created a sandbox
is routinely not the one that next runs code in it. Reconnect is one HTTP call
and it is the only thing that is correct across a process boundary.

**User-code failure is a value, infrastructure failure is an exception.** A
traceback, a non-zero exit and a blown time limit all come back as `ExecResult`;
only "we could not reach or start a machine" raises `SandboxError`. Collapsing
the two is how an agent ends up retrying a syntax error against a dead sandbox.

Provider exception messages never escape this module. E2B puts request URLs, and
occasionally echoed request detail, into its exception strings, and this text is
handed verbatim to the model and shown to the user. `_describe` maps the
exception *type* to a sentence we wrote and drops the rest.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

# Exceptions come from `e2b`, which ships py.typed; `Sandbox` only exists in the
# code-interpreter package, which does not, hence the one targeted ignore.
from e2b import (
    AuthenticationException,
    CommandExitException,
    InvalidArgumentException,
    NotEnoughSpaceException,
    NotFoundException,
    RateLimitException,
    SandboxNotFoundException,
    TimeoutException,
)
from e2b_code_interpreter import Sandbox  # type: ignore[import-untyped]

from .policy import egress_rules
from .types import (
    Artifact,
    ExecResult,
    FileEntry,
    Language,
    OutputSink,
    SandboxError,
    SandboxHandle,
    SandboxSpec,
)

#: The sandbox user's home. E2B resolves relative paths against it, so commands
#: run here unless the caller names somewhere else — otherwise `ls` and a
#: subsequent `open("data.csv")` disagree about where "here" is.
WORKDIR = "/home/user"

#: Shell convention for "killed by a timeout" (what `timeout(1)` exits with).
#: Reported instead of raising, because a slow query is the user's problem to
#: fix and the model can only fix it if it gets a result back.
TIMEOUT_EXIT_CODE = 124

#: Exception type -> the sentence we are willing to say about it. Ordered most
#: specific first. Anything unlisted degrades to the type name alone, which is
#: enough to grep our logs by and carries no provider payload.
_SAFE_REASONS: Tuple[Tuple[type, str], ...] = (
    (AuthenticationException, "the sandbox provider rejected our credentials"),
    (SandboxNotFoundException, "the sandbox no longer exists"),
    (NotFoundException, "the sandbox no longer exists"),
    (RateLimitException, "the sandbox provider is rate limiting this account"),
    (NotEnoughSpaceException, "the sandbox ran out of disk space"),
    (InvalidArgumentException, "the sandbox provider rejected the request"),
    (TimeoutException, "the sandbox provider timed out"),
)


def _sdk() -> Any:
    """The SDK entrypoint, read at call time.

    Indirection for two reasons: tests monkeypatch this module's `Sandbox` and
    a module-level alias would freeze the original, and E2B's dual-use methods
    (`Sandbox.kill(id)` as well as `sandbox.kill()`) are implemented with a
    descriptor that mypy reads as an ordinary bound method, so every class-level
    call would be a false positive.
    """
    return Sandbox


def _describe(exc: BaseException, action: str) -> str:
    """A failure sentence safe to show a user and hand to the model."""
    for kind, reason in _SAFE_REASONS:
        if isinstance(exc, kind):
            return f"Could not {action}: {reason}."
    return f"Could not {action}: the sandbox provider failed ({type(exc).__name__})."


def _sink_adapter(sink: OutputSink, stream: str) -> Optional[Callable[[Any], None]]:
    """Adapt an `OutputSink` to the SDK's per-stream callbacks.

    `run_code` hands back an `OutputMessage`, `commands.run` a bare `str`; one
    adapter covers both. Returns None when there is no sink so the SDK skips
    streaming entirely rather than paying for a no-op callback per line.
    """
    if sink is None:
        return None

    def forward(message: Any) -> None:
        try:
            sink(stream, str(getattr(message, "line", message)))
        except Exception:  # noqa: BLE001 - see below
            # A UI sink that throws must not abort a running execution: the user
            # would lose the work as well as the output. The result still
            # carries the full logs, so nothing is actually lost here.
            pass

    return forward


def _chart_json(chart: Any) -> str:
    """Serialise the SDK's structured chart, or "" if it will not serialise."""
    to_dict = getattr(chart, "to_dict", None)
    raw = to_dict() if callable(to_dict) else getattr(chart, "__dict__", None)
    if not raw:
        return ""
    try:
        # default=str because chart type is an enum and axis values can be dates.
        return json.dumps(raw, default=str)
    except (TypeError, ValueError):
        return ""


def _artifacts(result: Any) -> List[Artifact]:
    """Convert one SDK `Result` into zero or more artifacts.

    A single result carries several representations of the same thing — a
    matplotlib figure arrives as a chart, a PNG *and* the text
    "<Figure size 640x480>". All the rich ones are worth keeping; the text is
    only worth keeping when it is the only thing there, which is why it is
    checked last against an empty list.
    """
    main = bool(getattr(result, "is_main_result", False))
    out: List[Artifact] = []

    chart = getattr(result, "chart", None)
    if chart is not None:
        payload = _chart_json(chart)
        if payload:
            out.append(
                Artifact(
                    kind="chart",
                    mime="application/json",
                    data=payload,
                    is_main=main,
                    chart_json=payload,
                )
            )
    # png/jpeg/pdf arrive base64-encoded from the SDK and `Artifact.data` is
    # defined as base64 for binary kinds, so these pass through verbatim.
    if getattr(result, "png", None):
        out.append(Artifact(kind="png", mime="image/png", data=result.png, is_main=main))
    if getattr(result, "jpeg", None):
        out.append(
            Artifact(kind="jpeg", mime="image/jpeg", data=result.jpeg, is_main=main)
        )
    if getattr(result, "pdf", None):
        out.append(
            Artifact(kind="pdf", mime="application/pdf", data=result.pdf, is_main=main)
        )
    if getattr(result, "svg", None):
        out.append(
            Artifact(kind="svg", mime="image/svg+xml", data=result.svg, is_main=main)
        )
    if getattr(result, "html", None):
        out.append(
            Artifact(kind="html", mime="text/html", data=result.html, is_main=main)
        )
    if getattr(result, "json", None):
        try:
            payload = json.dumps(result.json, default=str)
        except (TypeError, ValueError):
            payload = ""
        if payload:
            out.append(
                Artifact(
                    kind="json", mime="application/json", data=payload, is_main=main
                )
            )
    if not out and getattr(result, "text", None):
        out.append(
            Artifact(kind="text", mime="text/plain", data=result.text, is_main=main)
        )
    return out


class E2BProvider:
    """`SandboxProvider` over E2B's Firecracker microVMs."""

    name = "e2b"

    def __init__(self, *, api_key: str, template: str = "") -> None:
        self._api_key = api_key
        self._template = template

    # -- lifecycle ---------------------------------------------------------

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        internet, allow_out, deny_out = egress_rules(spec.network, list(spec.allow_hosts))
        network: Dict[str, Any] = {"allow_out": allow_out, "deny_out": deny_out}
        try:
            sandbox = _sdk().create(
                template=spec.template or self._template or None,
                timeout=spec.timeout_seconds,
                metadata=dict(spec.metadata),
                envs=dict(spec.env),
                allow_internet_access=internet,
                network=network,
                # Pause rather than kill when the idle timer expires. This is the
                # whole persistent-workspace promise: a user who comes back after
                # lunch finds their dataframes and their /home/user files intact
                # instead of a fresh machine. `keep_memory` is what preserves the
                # interpreter's variables and not merely the filesystem.
                lifecycle={"on_timeout": {"action": "pause", "keep_memory": True}},
                api_key=self._api_key,
            )
        except Exception as exc:
            raise SandboxError(_describe(exc, "start a sandbox")) from exc
        return SandboxHandle(provider=self.name, external_id=sandbox.sandbox_id)

    def pause(self, handle: SandboxHandle) -> None:
        try:
            # Class-level call: pausing does not need a connection, and
            # connecting would auto-resume an already-paused sandbox just to put
            # it back to sleep — a round trip and a billed resume for nothing.
            _sdk().pause(handle.external_id, api_key=self._api_key)
        except Exception as exc:
            raise SandboxError(_describe(exc, "pause the sandbox")) from exc

    def kill(self, handle: SandboxHandle) -> None:
        try:
            # Idempotent by contract: the idle reaper and an explicit delete race
            # each other, and the loser must not turn a completed job into an
            # error. The SDK returns False for an unknown id; older paths raise
            # not-found instead, so both are treated as "already gone".
            _sdk().kill(handle.external_id, api_key=self._api_key)
        except (NotFoundException, SandboxNotFoundException):
            return
        except Exception as exc:
            raise SandboxError(_describe(exc, "stop the sandbox")) from exc

    # -- execution ---------------------------------------------------------

    def run_code(
        self,
        handle: SandboxHandle,
        code: str,
        *,
        language: Language = "python",
        timeout: float = 120.0,
        on_output: OutputSink = None,
    ) -> ExecResult:
        sandbox = self._connect(handle)
        started = time.perf_counter()
        try:
            execution = sandbox.run_code(
                code,
                language=language,
                on_stdout=_sink_adapter(on_output, "stdout"),
                on_stderr=_sink_adapter(on_output, "stderr"),
                timeout=timeout,
            )
        except TimeoutException:
            return ExecResult(
                error=f"Timed out after {timeout:.0f}s",
                duration_ms=_elapsed_ms(started),
            )
        except Exception as exc:
            raise SandboxError(_describe(exc, "run code in the sandbox")) from exc

        logs = getattr(execution, "logs", None)
        error = getattr(execution, "error", None)
        artifacts: List[Artifact] = []
        for result in getattr(execution, "results", None) or []:
            artifacts.extend(_artifacts(result))
        return ExecResult(
            # `Logs.stdout`/`.stderr` are lists of chunks, not strings; the SDK
            # splits on flush boundaries, so joining without a separator would
            # weld two print statements together.
            stdout="".join(getattr(logs, "stdout", None) or []),
            stderr="".join(getattr(logs, "stderr", None) or []),
            # No exit code exists for an interpreter cell — `ok` reads `error`.
            exit_code=None,
            error=f"{error.name}: {error.value}" if error else "",
            traceback=getattr(error, "traceback", "") or "" if error else "",
            artifacts=tuple(artifacts),
            duration_ms=_elapsed_ms(started),
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
        sandbox = self._connect(handle)
        started = time.perf_counter()
        try:
            result = sandbox.commands.run(
                command,
                cwd=cwd or WORKDIR,
                timeout=timeout,
                on_stdout=_sink_adapter(on_output, "stdout"),
                on_stderr=_sink_adapter(on_output, "stderr"),
            )
        except CommandExitException as exc:
            # A failed `pip install` is a result the model should read and act
            # on, not an exception. Caught before the generic handler because it
            # subclasses the SDK's base exception.
            return _command_result(exc, started)
        except TimeoutException:
            return ExecResult(
                exit_code=TIMEOUT_EXIT_CODE,
                error=f"Timed out after {timeout:.0f}s",
                duration_ms=_elapsed_ms(started),
            )
        except Exception as exc:
            raise SandboxError(_describe(exc, "run a command in the sandbox")) from exc
        return _command_result(result, started)

    # -- files -------------------------------------------------------------

    def write_files(self, handle: SandboxHandle, files: Mapping[str, bytes]) -> None:
        sandbox = self._connect(handle)
        for path, data in files.items():
            try:
                sandbox.files.write(path, data)
            except Exception as exc:
                raise SandboxError(
                    _describe(exc, f"write {path} into the sandbox")
                ) from exc

    def read_file(self, handle: SandboxHandle, path: str) -> bytes:
        sandbox = self._connect(handle)
        try:
            data = sandbox.files.read(path, format="bytes")
        except (NotFoundException, FileNotFoundError) as exc:
            raise SandboxError(f"No such file in the sandbox: {path}") from exc
        except Exception as exc:
            raise SandboxError(_describe(exc, f"read {path} from the sandbox")) from exc
        return bytes(data)

    def list_files(
        self, handle: SandboxHandle, path: str, *, depth: int = 1
    ) -> Sequence[FileEntry]:
        sandbox = self._connect(handle)
        try:
            entries = sandbox.files.list(path, depth=depth)
        except (NotFoundException, FileNotFoundError) as exc:
            raise SandboxError(f"No such directory in the sandbox: {path}") from exc
        except Exception as exc:
            raise SandboxError(_describe(exc, f"list {path} in the sandbox")) from exc
        return tuple(_file_entry(entry) for entry in entries)

    # -- internals ---------------------------------------------------------

    def _connect(self, handle: SandboxHandle) -> Any:
        """Reconnect to the machine, resuming it if it was paused.

        Deliberately not cached. See the module docstring: a live connection is
        process-local and this process is not the only one.
        """
        try:
            return _sdk().connect(handle.external_id, api_key=self._api_key)
        except Exception as exc:
            raise SandboxError(_describe(exc, "reach the sandbox")) from exc


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _command_result(result: Any, started: float) -> ExecResult:
    """Shared shape for both the success and the non-zero-exit paths, which the
    SDK reports with the same fields on two different objects."""
    exit_code = int(getattr(result, "exit_code", 0) or 0)
    error = getattr(result, "error", None) or ""
    return ExecResult(
        stdout=getattr(result, "stdout", "") or "",
        stderr=getattr(result, "stderr", "") or "",
        exit_code=exit_code,
        # `ok` already keys off exit_code; a message is only worth carrying when
        # the SDK gave us one beyond "it exited non-zero".
        error=str(error) if error else ("" if exit_code == 0 else f"exit {exit_code}"),
        duration_ms=_elapsed_ms(started),
    )


def _file_entry(entry: Any) -> FileEntry:
    # `type` is an enum on current SDKs and was a plain string on older ones;
    # `size` only exists on recent ones. Both are guarded rather than pinned,
    # because a listing is not worth a hard version floor.
    kind = getattr(entry, "type", None)
    kind_name = getattr(kind, "value", kind) or ""
    return FileEntry(
        path=getattr(entry, "path", "") or "",
        name=getattr(entry, "name", "") or "",
        is_dir=str(kind_name).lower() == "dir",
        size=int(getattr(entry, "size", 0) or 0),
    )
