"""An in-memory sandbox, so everything above the seam is testable without a key.

Not a mock library and not a stub that returns `None`: a real implementation
with a dict filesystem, real lifecycle states and real refusals. That matters
because the things it is used to test — quota enforcement, tenancy, the approval
path, artifact persistence — are exactly the things a `Mock` would happily agree
with. A test that asserts against a mock's recorded call proves the test, not the
code.

What it deliberately does not do is execute anything. `run_code` recognises a
literal `print("...")` and echoes it, which is enough to keep tool tests readable,
and stops there. Reaching for `exec()` here would put arbitrary generated code on
the machine running the test suite — a genuine sandbox escape, inside the double
whose entire purpose is to avoid one. Tests that need a specific result script it.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Mapping, Sequence, Tuple

from .types import (
    ExecResult,
    FileEntry,
    Language,
    OutputSink,
    SandboxError,
    SandboxHandle,
    SandboxSpec,
)

#: Matches `print("hello")` / `print('hello')` and nothing cleverer. No escapes,
#: no expressions, no f-strings: the moment this grows a feature it starts being
#: an interpreter, and an interpreter in a test double is a liability.
_PRINT = re.compile(r"^\s*print\(\s*(['\"])(.*?)\1\s*\)\s*$")


@dataclass
class _Box:
    """One imaginary machine."""

    id: str
    spec: SandboxSpec
    state: str = "running"  # running | paused | killed
    files: Dict[str, bytes] = field(default_factory=dict)


class FakeProvider:
    """`SandboxProvider` backed by dictionaries. Dev and test only."""

    name = "fake"

    def __init__(self) -> None:
        self._counter = 0
        self._boxes: Dict[str, _Box] = {}
        self._scripted: Deque[ExecResult] = deque()
        #: Every call, in order, for assertions. Public on purpose — this is a
        #: test double and a test reading it is the point.
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    # -- scripting ---------------------------------------------------------

    def script(self, *results: ExecResult) -> None:
        """Queue results for the next executions, in order.

        Consumed by both `run_code` and `run_command`, because a test asserting
        "the tool surfaced the traceback" does not care which one produced it.
        """
        self._scripted.extend(results)

    def clear(self) -> None:
        """Forget sandboxes, scripts and recorded calls."""
        self._counter = 0
        self._boxes.clear()
        self._scripted.clear()
        self.calls.clear()

    def box(self, handle: SandboxHandle) -> _Box:
        """The state behind a handle, for tests asserting on lifecycle."""
        box = self._boxes.get(handle.external_id)
        if box is None:
            raise SandboxError("That sandbox does not exist.")
        return box

    # -- lifecycle ---------------------------------------------------------

    def create(self, spec: SandboxSpec) -> SandboxHandle:
        self._counter += 1
        # Deterministic ids: a test asserting on a stored session row should not
        # have to thread a uuid through three layers to know what to expect.
        box = _Box(id=f"fake-{self._counter}", spec=spec)
        self._boxes[box.id] = box
        self._record("create", workspace_id=spec.workspace_id, template=spec.template)
        return SandboxHandle(provider=self.name, external_id=box.id)

    def pause(self, handle: SandboxHandle) -> None:
        box = self._live(handle)
        box.state = "paused"
        self._record("pause", id=box.id)

    def kill(self, handle: SandboxHandle) -> None:
        # Idempotent, including for an id that was never created: the reaper and
        # an explicit delete race, and the real provider tolerates both.
        self._record("kill", id=handle.external_id)
        box = self._boxes.get(handle.external_id)
        if box is not None:
            box.state = "killed"

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
        self._live(handle)
        self._record("run_code", code=code, language=language, timeout=timeout)
        if self._scripted:
            return self._stream(self._scripted.popleft(), on_output)
        stdout = "".join(
            f"{match.group(2)}\n"
            for match in (_PRINT.match(line) for line in code.splitlines())
            if match
        )
        return self._stream(ExecResult(stdout=stdout, duration_ms=1), on_output)

    def run_command(
        self,
        handle: SandboxHandle,
        command: str,
        *,
        timeout: float = 120.0,
        cwd: str = "",
        on_output: OutputSink = None,
    ) -> ExecResult:
        self._live(handle)
        self._record("run_command", command=command, cwd=cwd, timeout=timeout)
        if self._scripted:
            return self._stream(self._scripted.popleft(), on_output)
        # Nothing ran, so the honest answer is "succeeded silently". Tests that
        # care about a command's output script it.
        return self._stream(ExecResult(exit_code=0, duration_ms=1), on_output)

    # -- files -------------------------------------------------------------

    def write_files(self, handle: SandboxHandle, files: Mapping[str, bytes]) -> None:
        box = self._live(handle)
        self._record("write_files", paths=sorted(files))
        box.files.update(files)

    def read_file(self, handle: SandboxHandle, path: str) -> bytes:
        box = self._live(handle)
        self._record("read_file", path=path)
        if path not in box.files:
            raise SandboxError(f"No such file in the sandbox: {path}")
        return box.files[path]

    def list_files(
        self, handle: SandboxHandle, path: str, *, depth: int = 1
    ) -> Sequence[FileEntry]:
        box = self._live(handle)
        self._record("list_files", path=path, depth=depth)
        prefix = path.rstrip("/") + "/"
        # Directories are implied by the paths of the files under them, exactly
        # as they are in an object store — there is nothing to create.
        entries: Dict[str, FileEntry] = {}
        for stored, blob in sorted(box.files.items()):
            if not stored.startswith(prefix):
                continue
            parts = stored[len(prefix) :].split("/")
            for level in range(1, min(depth, len(parts)) + 1):
                child = prefix + "/".join(parts[:level])
                is_dir = level < len(parts)
                entries[child] = FileEntry(
                    path=child,
                    name=parts[level - 1],
                    is_dir=is_dir,
                    size=0 if is_dir else len(blob),
                )
        return tuple(entries.values())

    # -- internals ---------------------------------------------------------

    def _live(self, handle: SandboxHandle) -> _Box:
        """The box behind a handle, resumed if paused, refused if killed.

        Auto-resume mirrors E2B: reconnecting to a paused sandbox wakes it. A
        killed one is gone for good, and saying so loudly is how a test catches
        a caller that kept using a handle after cleanup.
        """
        box = self._boxes.get(handle.external_id)
        if box is None:
            raise SandboxError("That sandbox does not exist.")
        if box.state == "killed":
            raise SandboxError("That sandbox has been stopped and cannot be reused.")
        box.state = "running"
        return box

    def _record(self, method: str, **args: Any) -> None:
        self.calls.append((method, args))

    def _stream(self, result: ExecResult, on_output: OutputSink) -> ExecResult:
        """Replay the result's output through the sink, so streaming callers are
        exercised by the double rather than only by the real provider."""
        if on_output is not None:
            if result.stdout:
                on_output("stdout", result.stdout)
            if result.stderr:
                on_output("stderr", result.stderr)
        return result
