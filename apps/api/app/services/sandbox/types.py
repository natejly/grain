"""The seam between "we want to run this" and "a provider ran it".

Nothing above this module imports a provider SDK. That is not vendor-neutrality
theatre — it is what lets the tool layer, the quota checks, the approval path and
the artifact handling all be exercised with `FakeProvider`, with no key and no
network. A test that needs a real microVM to assert a quota is a test nobody runs.

The types are deliberately dumb: frozen dataclasses of plain data, JSON-round-
trippable, with no provider objects hiding inside them. `SandboxHandle` in
particular carries only the two strings needed to address a machine, so it can be
reconstructed from a database row rather than held in memory between requests —
which is the whole reason a paused sandbox can be resumed by a different process
than the one that created it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Mapping, Optional, Protocol, Sequence, Tuple

NetworkPolicy = Literal["open", "allowlist", "none"]
Language = Literal["python", "javascript", "bash", "r", "java"]
ArtifactKind = Literal["png", "jpeg", "svg", "pdf", "html", "json", "chart", "text"]

#: Called with each chunk of output as it arrives, so a long build streams to the
#: user instead of arriving as a wall of text after ninety seconds. Providers must
#: tolerate this being None.
OutputSink = Optional[Callable[[str, str], None]]  # (stream, text) -> None


class SandboxError(RuntimeError):
    """A sandbox could not be created, reached, or run.

    Carries a message already safe to show a user and to hand back to the model:
    provider stack traces and connection URLs never reach here. Mirrors
    `McpError`, and for the same reason — the model reads tool failures, so an
    unhelpful one costs a wasted turn.
    """


class SandboxQuotaError(SandboxError):
    """A limit was hit before anything was created. Distinct because it is not a
    fault: the caller should tell the user which dial to turn, not retry."""


@dataclass(frozen=True)
class SandboxSpec:
    """Everything needed to create a machine. Built by `session.py`, never by a tool."""

    workspace_id: str
    template: str = ""
    timeout_seconds: int = 900
    network: NetworkPolicy = "open"
    allow_hosts: Tuple[str, ...] = ()
    #: Environment variables for the sandbox. An allowlist, never a copy of ours:
    #: everything here is readable by generated code the moment it runs.
    env: Mapping[str, str] = field(default_factory=dict)
    #: Provider-side labels. Carries workspace/user ids so a sandbox is
    #: attributable at the provider's console during an incident.
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SandboxHandle:
    """Addresses one machine. Reconstructed from a `SandboxSession` row."""

    provider: str
    external_id: str


@dataclass(frozen=True)
class Artifact:
    """A non-text result: a chart, an image, a rendered table.

    `data` is base64 for binary kinds and raw text otherwise. `chart` carries the
    provider's structured chart description when it has one — that is strictly
    better than a PNG for the UI, because it can be re-rendered in the app's own
    theme instead of pasting a matplotlib default into a cream-coloured page.
    """

    kind: ArtifactKind
    mime: str
    data: str
    is_main: bool = False
    chart_json: str = ""


@dataclass(frozen=True)
class ExecResult:
    """What happened. Never raises for user-code failure — a traceback is a
    result, not an exception. Only infrastructure failure raises `SandboxError`,
    because those two need different handling and conflating them means the model
    retries a syntax error against a dead machine."""

    stdout: str = ""
    stderr: str = ""
    exit_code: Optional[int] = None
    #: Empty when the code ran cleanly. Set to the exception name/message when
    #: user code raised, with `traceback` holding the detail.
    error: str = ""
    traceback: str = ""
    artifacts: Tuple[Artifact, ...] = ()
    duration_ms: int = 0
    #: True when output was clipped to `sandbox_max_output_bytes`, so the caller
    #: can say so rather than letting the model reason about a truncated tail.
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and (self.exit_code in (None, 0))


@dataclass(frozen=True)
class FileEntry:
    path: str
    name: str
    is_dir: bool
    size: int = 0


class SandboxProvider(Protocol):
    """What a driver must do. Implemented by `E2BProvider` and `FakeProvider`."""

    name: str

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        """Start a machine. Raises `SandboxError` if the provider refuses."""

    def run_code(
        self,
        handle: SandboxHandle,
        code: str,
        *,
        language: Language = "python",
        timeout: float = 120.0,
        on_output: OutputSink = None,
    ) -> ExecResult:
        """Run code in the session's persistent interpreter.

        Persistent is the point: variables, imports and loaded dataframes survive
        between calls, so an agent can explore a dataset over several turns
        without re-reading a 200 MB file each time.
        """

    def run_command(
        self,
        handle: SandboxHandle,
        command: str,
        *,
        timeout: float = 120.0,
        cwd: str = "",
        on_output: OutputSink = None,
    ) -> ExecResult:
        """Run a shell command. This is the `pip install` path."""

    def write_files(self, handle: SandboxHandle, files: Mapping[str, bytes]) -> None:
        """Materialise files into the sandbox. Paths are sandbox-absolute."""

    def read_file(self, handle: SandboxHandle, path: str) -> bytes:
        """Read one file out. Raises `SandboxError` if it is missing."""

    def list_files(
        self, handle: SandboxHandle, path: str, *, depth: int = 1
    ) -> Sequence[FileEntry]:
        ...

    def pause(self, handle: SandboxHandle) -> None:
        """Snapshot and stop. The machine resumes with its filesystem intact."""

    def kill(self, handle: SandboxHandle) -> None:
        """Destroy. Must be idempotent — the reaper and an explicit delete race."""
