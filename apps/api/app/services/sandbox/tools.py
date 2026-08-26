"""Agent tools for server-side execution (ADR 0005).

What the model actually calls. Everything here is a thin shell over
`session.py` and the provider seam: this module's real job is the two things a
provider cannot do for us — deciding *which* sandbox a call means, and turning
what came back into something a model can act on.

Three rules run through it.

*Lifecycle is not the model's problem.* Every tool takes an optional `session`
and defaults to `ensure_session`, so "what is 2**100" costs one tool call rather
than create-run-kill. A named session goes through `resolve_session` and nowhere
else — that function filters by workspace, and it is the only reason a sandbox
id in a model's context window is not a way into another tenant's machine.

*A failed execution is a result, not an exception.* A traceback comes back as
text the model can read and fix. Only a dead provider raises, and it is rendered
as a sentence rather than a stack trace, because the model reads tool failures
and an unhelpful one costs a wasted turn (the same argument as mcp/client.py).

*The write tools are approval-gated by inheritance.* `read_only=False` lands
them on the "ask" policy from the approval loop, and the preview shows the code
*and* the network policy. Those two are one fact: `requests.post(url, data=df)`
is a bug on `none` and an exfiltration on `open`, and an approver who is shown
only the code cannot tell which one they are approving.
"""
from __future__ import annotations

import difflib
import json
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...config import Settings, get_settings
from ...models import AgentToolCall, Run, SandboxExecution, SandboxSession, Source, new_id
from ..ingestion import object_path, sanitize_filename
from ..llm_tools import MAX_RESULT_CHARS, ToolContext, ToolResult, ToolSpec
from ..projects import store
from .outputs import persist_artifacts
from .provider import get_provider
from .secrets import secret_names
from .session import (
    allow_hosts_for,
    clip,
    ensure_session,
    handle_for,
    list_sessions,
    network_policy_of,
    record_execution,
    resolve_session,
    touch,
)
from .types import (
    ExecResult,
    SandboxError,
    SandboxHandle,
    SandboxProvider,
    SandboxQuotaError,
)

#: The sandbox's working directory, as the model is told to think of it. E2B
#: really does drop the user in /home/user; the local drivers map that prefix
#: onto their per-session root. Naming it once here is what keeps an upload, a
#: download and a `pd.read_csv` agreeing about where a file went.
SANDBOX_HOME = "/home/user"

#: Rendering budgets. stdout is the elastic one: stderr, the traceback and the
#: artifact list are all bounded first, and stdout gets whatever is left of the
#: result budget. That ordering is deliberate — the artifact ids come last in the
#: rendered text, so a 40 MB print loop must not be what pushes them out of it.
MAX_STDERR_CHARS = 1_200
MAX_TRACEBACK_CHARS = 1_500
MIN_STDOUT_CHARS = 400
#: Slack for the section headers the budget arithmetic does not count.
RENDER_MARGIN = 200
#: Approval cards are read by a human under time pressure; a 400-line paste is
#: not reviewed, it is waved through.
PREVIEW_CODE_CHARS = 4_000

#: One `write_files` call buffers everything in memory on both sides, so an
#: upload is bounded by count and by total size rather than per file.
MAX_UPLOAD_FILES = 50
MAX_UPLOAD_TOTAL_BYTES = 32 * 1024 * 1024

#: A file read into the model's context is bounded by the result budget, not by
#: what a file can be — a 40 MB log read whole is both useless to the model and a
#: way to blow the turn's tokens. The model is told to read a slice instead.
MAX_FILE_READ_BYTES = 128 * 1024
#: A single model-authored file. Generous — a written module or a JSON config is
#: kilobytes — but bounded, because `content` is unbounded model output.
MAX_FILE_WRITE_BYTES = 4 * 1024 * 1024
#: Diff shown on the approval card. The card is read by a human under time
#: pressure; a 2,000-line diff is waved through rather than reviewed.
MAX_DIFF_LINES = 200


def _text(args: Dict[str, Any], key: str, default: str = "") -> str:
    value = args.get(key, default)
    return value if isinstance(value, str) else default


def _names(args: Dict[str, Any], key: str) -> List[str]:
    """Read a list-of-strings argument, tolerating the string a model sends when
    it forgets the brackets."""
    raw = args.get(key)
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, str) and item.strip()]


# --------------------------------------------------------------------------
# Which run, which sandbox


def _current_call(db: Session, context: ToolContext) -> Tuple[str, str]:
    """(run_id, tool_call_id) for the call being executed right now.

    `ToolContext` carries no run id — it predates this family — and both the
    execution record and the per-run cap need one. The agent loop runs tools
    inside the newest run of the conversation and writes the `AgentToolCall` row
    (and commits it) before invoking the executor, so the newest row of each is
    this call. Missing rows are not an error: these tools are also reachable
    from a script and from tests, and there the cap simply has no run to count.
    """
    run = db.scalar(
        select(Run)
        .where(
            Run.workspace_id == context.workspace_id,
            Run.conversation_id == context.conversation_id,
        )
        .order_by(Run.created_at.desc())
    )
    if run is None:
        return "", ""
    call = db.scalar(
        select(AgentToolCall)
        .where(AgentToolCall.run_id == run.id)
        .order_by(AgentToolCall.id.desc())
    )
    return run.id, (call.id if call is not None else "")


def _over_exec_cap(
    db: Session, *, workspace_id: str, run_id: str, settings: Settings
) -> Optional[str]:
    """The runaway-loop guard: a message when this turn has executed enough.

    A model that misreads an error can retry the same cell forever, and each
    retry is a billed microVM second. The cap is per run rather than per session
    because the session outlives the turn — the thing worth bounding is one
    turn's appetite, not the machine's lifetime.
    """
    if not run_id:
        return None
    used = (
        db.scalar(
            select(func.count())
            .select_from(SandboxExecution)
            .where(
                SandboxExecution.workspace_id == workspace_id,
                SandboxExecution.run_id == run_id,
            )
        )
        or 0
    )
    if used < settings.sandbox_max_execs_per_run:
        return None
    return (
        f"Sandbox execution limit reached: {settings.sandbox_max_execs_per_run} "
        "executions per turn, and this turn has used them all. Work with the "
        "results you already have and tell the user what is left to run."
    )


def _session_for(
    db: Session,
    context: ToolContext,
    args: Dict[str, Any],
    settings: Settings,
    *,
    project_id: str = "",
) -> SandboxSession:
    """The sandbox a call means.

    A named session is resolved through `resolve_session` and nothing else; an
    unnamed one reuses or creates the workspace's current sandbox, so the model
    never has to manage a lifecycle to do one calculation.
    """
    named = _text(args, "session") or _text(args, "session_id")
    if named:
        return resolve_session(db, workspace_id=context.workspace_id, session_id=named)
    return ensure_session(
        db,
        workspace_id=context.workspace_id,
        user_id=context.user_id,
        settings=settings,
        project_id=project_id,
    )


def _policy_of(
    db: Session, context: ToolContext, args: Dict[str, Any], settings: Settings
) -> Tuple[str, List[str]]:
    """The egress policy a call will run under, for the approval card.

    Read off the session row when one is named, because a session records the
    policy it was created with: changing the workspace default must not make an
    approval card describe a live sandbox as more restricted than it is.
    """
    named = _text(args, "session") or _text(args, "session_id")
    if named:
        try:
            session = resolve_session(db, workspace_id=context.workspace_id, session_id=named)
        except SandboxError:
            # An unknown session is the executor's problem to report; a preview
            # still has to render, so it describes what a new sandbox would get.
            return settings.sandbox_network_policy, settings.sandbox_allowed_hosts
        return network_policy_of(session), allow_hosts_for(session)
    return settings.sandbox_network_policy, settings.sandbox_allowed_hosts


def _policy_line(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    settings = get_settings()
    policy, hosts = _policy_of(db, context, args, settings)
    if policy == "none":
        return "network: none — this sandbox has no outbound access"
    if policy == "allowlist":
        allowed = ", ".join(hosts) if hosts else "nothing"
        return f"network: allowlist — it can reach {allowed}, and nothing else"
    return (
        "network: open — it can reach the whole internet, including anywhere it "
        "could send this data"
    )


def _secrets_line(db: Session, workspace_id: str) -> str:
    """The credentials sentence for the approval card, shown next to the egress
    one. Code the approver is about to run reads these from its environment, so
    what it can *do* with the network depends on what it can *read* here — both
    belong on the same card. Names only; a value never leaves the box."""
    names = secret_names(db, workspace_id=workspace_id)
    if not names:
        return "secrets: none"
    return f"secrets: it can read {', '.join(names)} from its environment"


# --------------------------------------------------------------------------
# Rendering a result for the model


def _describe_artifact(descriptor: Dict[str, Any]) -> str:
    """One line per artifact, which is what a model needs to decide whether to
    look again. `chart: line, 2 series` says the plot worked; `image: png
    480x320` says the figure is not a blank canvas."""
    kind = str(descriptor.get("kind", "file"))
    chart = descriptor.get("chart")
    if isinstance(chart, dict):
        chart_type = str(chart.get("type") or "chart")
        elements = chart.get("elements")
        count = len(elements) if isinstance(elements, list) else 0
        series = "1 series" if count == 1 else f"{count} series"
        return f"chart: {chart_type}, {series}"
    if "width" in descriptor and "height" in descriptor:
        return f"image: {kind} {descriptor['width']}x{descriptor['height']}"
    return f"{kind}: {int(descriptor.get('bytes', 0)):,} bytes"


def _tail(
    result: ExecResult, descriptors: List[Dict[str, Any]], dropped: int
) -> List[str]:
    """The sections that must survive clipping: what was produced, and whether
    what is shown is the whole of it."""
    sections: List[str] = []
    if descriptors:
        lines = [
            f"- {_describe_artifact(item)} — saved as source {item['id']}"
            for item in descriptors
        ]
        sections.append("Artifacts:\n" + "\n".join(lines))
    if dropped > 0:
        sections.append(
            f"{dropped} further artifact(s) were not saved: over the size limit "
            "or in a format this workspace does not store."
        )
    if result.truncated:
        sections.append(
            "The sandbox clipped this execution's output at its byte limit, so "
            "what is above is not the whole log."
        )
    return sections


def _render(
    result: ExecResult, descriptors: List[Dict[str, Any]], *, dropped: int
) -> str:
    """stdout, stderr, the error, what it produced — in the order a reader needs
    them, with the bounded sections measured first so the elastic one (stdout)
    cannot push the artifact ids out of the result budget."""
    tail = _tail(result, descriptors, dropped)

    bounded: List[str] = []
    stderr_text, stderr_cut = clip(result.stderr.strip(), MAX_STDERR_CHARS)
    if stderr_text:
        bounded.append("stderr:\n" + stderr_text + ("\n(stderr truncated)" if stderr_cut else ""))
    if result.error:
        trace_text, trace_cut = clip(result.traceback.strip(), MAX_TRACEBACK_CHARS)
        error_block = f"Error: {result.error}"
        if trace_text:
            error_block += "\n" + trace_text + ("\n(traceback truncated)" if trace_cut else "")
        bounded.append(error_block)
    if result.exit_code not in (None, 0):
        bounded.append(f"Exit code: {result.exit_code}")

    spent = sum(len(section) for section in bounded + tail)
    budget = max(MIN_STDOUT_CHARS, MAX_RESULT_CHARS - spent - RENDER_MARGIN)
    stdout_text, stdout_cut = clip(result.stdout.strip(), budget)

    sections: List[str] = []
    if stdout_text:
        sections.append("stdout:\n" + stdout_text + ("\n(stdout truncated)" if stdout_cut else ""))
    sections.extend(bounded)
    sections.extend(tail)
    if not sections:
        # Silence is a real outcome — an assignment, a successful install — and
        # saying so is better than handing the model an empty string to reason about.
        return "Ran successfully with no output."
    return "\n\n".join(sections)


# --------------------------------------------------------------------------
# run_python / run_command


def _execute(db: Session, context: ToolContext, args: Dict[str, Any], *, kind: str) -> ToolResult:
    settings = get_settings()
    argument = "code" if kind == "code" else "command"
    source = _text(args, argument)
    if not source.strip():
        return ToolResult(content=f"Error: `{argument}` is required.")

    run_id, tool_call_id = _current_call(db, context)
    refusal = _over_exec_cap(
        db, workspace_id=context.workspace_id, run_id=run_id, settings=settings
    )
    if refusal:
        return ToolResult(content=refusal)

    try:
        session = _session_for(db, context, args, settings)
    except SandboxQuotaError as exc:
        return ToolResult(content=str(exc))
    except SandboxError as exc:
        return ToolResult(content=f"Error: {exc}")

    try:
        provider = get_provider(settings)
        handle = handle_for(session)
        timeout = settings.sandbox_exec_timeout_seconds
        if kind == "code":
            result = provider.run_code(handle, source, language="python", timeout=timeout)
        else:
            # No cwd: every driver defaults to its own session root, and naming
            # one here would hard-code this file to the E2B layout.
            result = provider.run_command(handle, source, timeout=timeout)
    except SandboxQuotaError as exc:
        return ToolResult(content=str(exc))
    except SandboxError as exc:
        # Infrastructure, not user code. Named as such so the model retries the
        # turn rather than "fixing" a syntax error that was never there.
        return ToolResult(content=f"The sandbox could not run this: {exc}")

    descriptors = persist_artifacts(
        db,
        workspace_id=context.workspace_id,
        session_id=session.id,
        result=result,
        settings=settings,
    )
    record_execution(
        db,
        session=session,
        run_id=run_id,
        tool_call_id=tool_call_id,
        kind=kind,
        source=source,
        result=result,
        settings=settings,
        artifacts=descriptors,
    )
    dropped = len(result.artifacts) - len(descriptors)
    return ToolResult(
        content=_render(result, descriptors, dropped=dropped),
        artifacts=descriptors,
    )


def _run_python(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return _execute(db, context, args, kind="code")


def _run_command(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return _execute(db, context, args, kind="command")


def _preview_execution(
    db: Session, context: ToolContext, args: Dict[str, Any], *, kind: str
) -> str:
    argument = "code" if kind == "code" else "command"
    body = _text(args, argument).strip()
    fence = "python" if kind == "code" else "bash"
    verb = (
        "Run this Python in a sandbox"
        if kind == "code"
        else "Run this shell command in a sandbox"
    )
    if not body:
        # Previews are a courtesy and must never be fatal, so a missing argument
        # is described rather than raised — the approver still sees a card.
        return f"{verb} — but no `{argument}` was given, so this call will fail."
    clipped = body[:PREVIEW_CODE_CHARS]
    if len(body) > PREVIEW_CODE_CHARS:
        clipped += "\n…(truncated)"
    return (
        f"{verb} ({_policy_line(db, context, args)}; "
        f"{_secrets_line(db, context.workspace_id)}):"
        f"\n\n```{fence}\n{clipped}\n```"
    )


def _preview_run_python(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    return _preview_execution(db, context, args, kind="code")


def _preview_run_command(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    return _preview_execution(db, context, args, kind="command")


# --------------------------------------------------------------------------
# Moving files in and out


def _workspace_sources(db: Session, workspace_id: str) -> List[Source]:
    return list(
        db.scalars(
            select(Source).where(
                Source.workspace_id == workspace_id, Source.deleted_at.is_(None)
            )
        )
    )


def _files_from_sources(db: Session, context: ToolContext, names: List[str]) -> Dict[str, bytes]:
    """Read named uploaded sources into a path -> bytes map.

    Every name goes through `store.normalize_path`, which is the codebase's one
    answer to "is this path allowed to exist" — absolute paths, `..`, backslashes
    and control characters are refused there rather than sanitized here.
    """
    available = _workspace_sources(db, context.workspace_id)
    by_name = {source.filename.lower(): source for source in available}
    files: Dict[str, bytes] = {}
    for name in names:
        clean = store.normalize_path(name)
        source = by_name.get(clean.lower()) or by_name.get(Path(clean).name.lower())
        if source is None:
            known = ", ".join(sorted(source.filename for source in available)) or "none"
            raise store.ProjectError(f"No uploaded source named “{name}”. Available: {known}")
        try:
            files[f"{SANDBOX_HOME}/{Path(clean).name}"] = Path(source.object_key).read_bytes()
        except OSError as exc:
            raise store.ProjectError(
                f"“{source.filename}” could not be read from storage"
            ) from exc
    return files


def _files_from_project(
    db: Session, context: ToolContext, args: Dict[str, Any]
) -> Dict[str, bytes]:
    project = store.resolve(
        db,
        workspace_id=context.workspace_id,
        project_id=_text(args, "project_id"),
        name=_text(args, "project"),
    )
    snapshot = store.snapshot(db, project)
    entries = snapshot.get("files")
    files: Dict[str, bytes] = {}
    for entry in entries if isinstance(entries, list) else []:
        path = store.normalize_path(str(entry.get("path", "")))
        files[f"{SANDBOX_HOME}/{path}"] = str(entry.get("content", "")).encode("utf-8")
    return files


def _check_upload_size(files: Dict[str, bytes]) -> None:
    if len(files) > MAX_UPLOAD_FILES:
        raise store.ProjectError(
            f"That is {len(files)} files; at most {MAX_UPLOAD_FILES} can be uploaded at once"
        )
    total = sum(len(data) for data in files.values())
    if total > MAX_UPLOAD_TOTAL_BYTES:
        raise store.ProjectError(
            f"That is {total:,} bytes; one upload carries at most "
            f"{MAX_UPLOAD_TOTAL_BYTES:,}. Upload fewer files."
        )


def _sandbox_upload(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    settings = get_settings()
    names = _names(args, "paths")
    wants_project = bool(_text(args, "project") or _text(args, "project_id"))
    if not names and not wants_project:
        return ToolResult(
            content="Error: give `paths` (names of uploaded sources) or `project`."
        )
    try:
        files = _files_from_sources(db, context, names) if names else {}
        if wants_project:
            files.update(_files_from_project(db, context, args))
        _check_upload_size(files)
    except store.ProjectError as exc:
        return ToolResult(content=f"Error: {exc}")
    if not files:
        return ToolResult(content="Error: nothing to upload — that project has no files.")

    try:
        session = _session_for(db, context, args, settings)
        provider = get_provider(settings)
        provider.write_files(handle_for(session), files)
    except SandboxQuotaError as exc:
        return ToolResult(content=str(exc))
    except SandboxError as exc:
        return ToolResult(content=f"The sandbox could not take these files: {exc}")
    touch(db, session)

    listed = ", ".join(
        f"{path} ({len(data):,} bytes)" for path, data in sorted(files.items())
    )
    return ToolResult(
        content=f"Uploaded {len(files)} file(s) into {SANDBOX_HOME}: {listed}."
    )


def _preview_upload(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    names = _names(args, "paths")
    project = _text(args, "project") or _text(args, "project_id")
    if names:
        what = ", ".join(names)
    elif project:
        what = f"every file in project “{project}”"
    else:
        return "Copy workspace files into a sandbox — but neither `paths` nor `project` was given."
    return (
        f"Copy {what} into {SANDBOX_HOME} in a sandbox, where generated code can "
        f"read them ({_policy_line(db, context, args)})."
    )


def _remote_path(raw: str) -> str:
    """Where in the sandbox to read from.

    Relative paths are resolved against /home/user because that is where the
    code that wrote the file was standing. `..` is refused outright: it never
    means anything useful here, and refusing is cheaper than reasoning about
    what a hostile sandbox could point it at.
    """
    path = raw.strip()
    if not path:
        raise store.ProjectError("A `path` inside the sandbox is required")
    if ".." in path.split("/"):
        raise store.ProjectError("Sandbox paths may not contain “..”")
    return path if path.startswith("/") else f"{SANDBOX_HOME}/{path}"


def _media_type(filename: str) -> str:
    guessed, _encoding = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def _reported_size(
    provider: SandboxProvider, handle: SandboxHandle, remote: str
) -> Optional[int]:
    """The provider's idea of the file's size, before we buffer any of it.

    A sandbox is untrusted and can hand back a 2 GB file, and by the time
    `read_file` returns it is already in this process's memory. Listing the
    parent directory first is the only pre-read signal the driver seam offers.
    Best effort: a provider that reports no size is not an error, the post-read
    check below still holds.
    """
    parent = remote.rsplit("/", 1)[0] or "/"
    try:
        entries = provider.list_files(handle, parent, depth=1)
    except SandboxError:
        return None
    for entry in entries:
        if entry.path == remote or entry.name == remote.rsplit("/", 1)[-1]:
            return int(entry.size) if entry.size else None
    return None


def _sandbox_download(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    settings = get_settings()
    try:
        remote = _remote_path(_text(args, "path"))
    except store.ProjectError as exc:
        return ToolResult(content=f"Error: {exc}")

    try:
        session = _session_for(db, context, args, settings)
        provider = get_provider(settings)
        handle = handle_for(session)
        reported = _reported_size(provider, handle, remote)
        if reported is not None and reported > settings.max_upload_bytes:
            return ToolResult(
                content=(
                    f"{remote} is {reported:,} bytes, over this workspace's "
                    f"{settings.max_upload_bytes:,}-byte file limit. Write a smaller "
                    "file — a summary, a sample, or a compressed export."
                )
            )
        data = provider.read_file(handle, remote)
    except SandboxQuotaError as exc:
        return ToolResult(content=str(exc))
    except SandboxError as exc:
        return ToolResult(content=f"The sandbox could not return that file: {exc}")
    # Checked again after the read: the listing can be stale, absent, or simply
    # wrong, and the size that matters is the one we are about to store.
    if len(data) > settings.max_upload_bytes:
        return ToolResult(
            content=(
                f"{remote} is {len(data):,} bytes, over this workspace's "
                f"{settings.max_upload_bytes:,}-byte file limit, so it was not saved."
            )
        )
    touch(db, session)

    filename = sanitize_filename(remote.rsplit("/", 1)[-1])
    source = Source(
        id=new_id(),
        workspace_id=context.workspace_id,
        created_by=context.user_id,
        filename=filename,
        media_type=_media_type(filename),
        object_key="",
        byte_size=len(data),
        # Stored, not ingested — nothing indexed this and retrieval must not
        # believe it can quote it. Same rule as sandbox artifacts.
        status="stored",
    )
    path = object_path(context.workspace_id, source.id, filename)
    path.write_bytes(data)
    source.object_key = str(path)
    db.add(source)
    db.commit()
    return ToolResult(
        content=(
            f"Saved {remote} to the workspace as “{filename}” "
            f"({len(data):,} bytes), source id {source.id}."
        ),
        created_ids=[source.id],
    )


def _preview_download(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    raw = _text(args, "path").strip()
    if not raw:
        return "Copy a file out of a sandbox — but no `path` was given, so this call will fail."
    return (
        f"Copy {raw} out of the sandbox and save it to this workspace's files, "
        "where you can open it."
    )


# --------------------------------------------------------------------------
# Reading, writing and editing files in the sandbox
#
# `sandbox_upload`/`sandbox_download` move whole files between the workspace's
# object store and a sandbox. These four are the Claude-Code loop *inside* the
# sandbox: read a file the code just wrote, compose a new one from text, or edit
# one in place — none of which needs a workspace `Source` on either end. They
# lean entirely on the provider file methods every driver already implements, so
# there is no new provider surface and they work identically on fake, local and
# e2b. Reads and lists are `read_only`; write and edit inherit the approval gate
# and render a unified diff, so the approver sees exactly what changes.


def _read_text(data: bytes) -> Tuple[str, bool]:
    """Decode a sandbox file for the model, flagging binary.

    A NUL byte is the cheap, reliable "this is not text" signal: inlining a
    decoded PNG wastes the turn's budget on mojibake and tells the model nothing
    it can act on. The caller turns the flag into a "use sandbox_download" nudge.
    """
    if b"\x00" in data:
        return "", True
    return data.decode("utf-8", errors="replace"), False


def _diff(before: str, after: str, path: str) -> str:
    """A unified diff the approval card renders as red/green.

    `ProposalDiff` on the web splits the preview at the first `@@` hunk header,
    so the prose above stays a note and everything from the `---/+++/@@` lines
    down is coloured. Bounded in lines because a card nobody reads is not a
    review — the tail is dropped with a marker rather than silently.
    """
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    if not lines:
        return ""
    clipped = lines[:MAX_DIFF_LINES]
    if len(lines) > MAX_DIFF_LINES:
        clipped.append(f"… ({len(lines) - MAX_DIFF_LINES} more diff lines)")
    return "\n".join(clipped)


def _read_current(
    db: Session, context: ToolContext, args: Dict[str, Any], settings: Settings, remote: str
) -> Optional[str]:
    """The file's current text, or None if it is absent or unreadable.

    Used by the write/edit previews and by the edit executor. Kept best-effort on
    purpose: a preview that raised because the file does not exist yet would blank
    the approval card for a perfectly valid "create this file" call.
    """
    try:
        session = _session_for(db, context, args, settings)
        provider = get_provider(settings)
        data = provider.read_file(handle_for(session), remote)
    except SandboxError:
        return None
    text, is_binary = _read_text(data)
    return None if is_binary else text


def _sandbox_read(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    settings = get_settings()
    try:
        remote = _remote_path(_text(args, "path"))
    except store.ProjectError as exc:
        return ToolResult(content=f"Error: {exc}")
    try:
        session = _session_for(db, context, args, settings)
        provider = get_provider(settings)
        data = provider.read_file(handle_for(session), remote)
    except SandboxQuotaError as exc:
        return ToolResult(content=str(exc))
    except SandboxError as exc:
        return ToolResult(content=f"Error: {exc}")
    touch(db, session)

    text, is_binary = _read_text(data)
    if is_binary:
        return ToolResult(
            content=(
                f"{remote} is {len(data):,} bytes of binary data, not text. Use "
                "sandbox_download to save it to the workspace, or run_python to "
                "inspect it."
            )
        )
    body, truncated = clip(text, MAX_FILE_READ_BYTES)
    note = (
        f"\n\n…(file is {len(data):,} bytes; showing the first "
        f"{MAX_FILE_READ_BYTES:,}. Read a slice in run_python for more.)"
        if truncated
        else ""
    )
    return ToolResult(content=f"{remote}:\n\n{body}{note}")


def _sandbox_write(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    settings = get_settings()
    try:
        remote = _remote_path(_text(args, "path"))
    except store.ProjectError as exc:
        return ToolResult(content=f"Error: {exc}")
    content = _text(args, "content")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_FILE_WRITE_BYTES:
        return ToolResult(
            content=(
                f"That is {len(encoded):,} bytes; a single write carries at most "
                f"{MAX_FILE_WRITE_BYTES:,}. Write it in parts, or generate it with "
                "run_python."
            )
        )
    try:
        session = _session_for(db, context, args, settings)
        provider = get_provider(settings)
        provider.write_files(handle_for(session), {remote: encoded})
    except SandboxQuotaError as exc:
        return ToolResult(content=str(exc))
    except SandboxError as exc:
        return ToolResult(content=f"The sandbox could not write that file: {exc}")
    touch(db, session)
    return ToolResult(content=f"Wrote {len(encoded):,} bytes to {remote}.")


def _sandbox_edit(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    settings = get_settings()
    try:
        remote = _remote_path(_text(args, "path"))
    except store.ProjectError as exc:
        return ToolResult(content=f"Error: {exc}")
    find = _text(args, "find")
    if not find:
        return ToolResult(content="Error: `find` is required — the exact text to replace.")
    replace = _text(args, "replace")
    replace_all = bool(args.get("all"))

    try:
        session = _session_for(db, context, args, settings)
        provider = get_provider(settings)
        handle = handle_for(session)
        data = provider.read_file(handle, remote)
    except SandboxQuotaError as exc:
        return ToolResult(content=str(exc))
    except SandboxError as exc:
        return ToolResult(content=f"Error: {exc}")

    before, is_binary = _read_text(data)
    if is_binary:
        return ToolResult(content=f"Error: {remote} is binary and cannot be edited as text.")
    occurrences = before.count(find)
    if occurrences == 0:
        return ToolResult(
            content=f"Error: `find` text is not in {remote}. Read it first to copy the exact text."
        )
    if occurrences > 1 and not replace_all:
        return ToolResult(
            content=(
                f"Error: `find` matches {occurrences} places in {remote}. Quote more "
                "surrounding text to make it unique, or pass all=true to replace every match."
            )
        )
    after = before.replace(find, replace)
    try:
        provider.write_files(handle, {remote: after.encode("utf-8")})
    except SandboxError as exc:
        return ToolResult(content=f"The sandbox could not write that file: {exc}")
    touch(db, session)
    changed = occurrences if replace_all else 1
    return ToolResult(content=f"Edited {remote}: {changed} replacement(s).")


def _preview_write(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    raw = _text(args, "path").strip()
    if not raw:
        return "Write a file in the sandbox — but no `path` was given, so this call will fail."
    settings = get_settings()
    try:
        remote = _remote_path(raw)
    except store.ProjectError:
        remote = raw
    after = _text(args, "content")
    before = _read_current(db, context, args, settings, remote)
    verb = "Overwrite" if before is not None else "Create"
    diff = _diff(before or "", after, remote)
    head = f"{verb} {remote} in the sandbox ({_policy_line(db, context, args)})."
    return f"{head}\n\n{diff}" if diff else head


def _preview_edit(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    raw = _text(args, "path").strip()
    if not raw:
        return "Edit a file in the sandbox — but no `path` was given, so this call will fail."
    settings = get_settings()
    try:
        remote = _remote_path(raw)
    except store.ProjectError:
        remote = raw
    find = _text(args, "find")
    replace = _text(args, "replace")
    head = f"Edit {remote} in the sandbox ({_policy_line(db, context, args)})."
    before = _read_current(db, context, args, settings, remote)
    if before is not None and find and find in before:
        count = -1 if args.get("all") else 1
        after = before.replace(find, replace, count)
        diff = _diff(before, after, remote)
        if diff:
            return f"{head}\n\n{diff}"
    # No before-text to diff against (new session, unreadable, or find not
    # present yet): show the substitution itself rather than a blank card.
    clipped_find = find[:PREVIEW_CODE_CHARS]
    clipped_replace = replace[:PREVIEW_CODE_CHARS]
    return f"{head}\n\nReplace:\n{clipped_find}\n\nWith:\n{clipped_replace}"


def _sandbox_list(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    settings = get_settings()
    raw = _text(args, "path").strip() or SANDBOX_HOME
    try:
        remote = _remote_path(raw)
    except store.ProjectError as exc:
        return ToolResult(content=f"Error: {exc}")
    depth = 1
    if isinstance(args.get("recursive"), bool) and args["recursive"]:
        depth = 3
    try:
        session = _session_for(db, context, args, settings)
        provider = get_provider(settings)
        entries = provider.list_files(handle_for(session), remote, depth=depth)
    except SandboxQuotaError as exc:
        return ToolResult(content=str(exc))
    except SandboxError as exc:
        return ToolResult(content=f"Error: {exc}")
    touch(db, session)
    if not entries:
        return ToolResult(content=f"{remote} is empty or does not exist.")
    lines = [
        f"{'dir ' if entry.is_dir else 'file'}  {entry.path}"
        + ("" if entry.is_dir else f"  ({entry.size:,} bytes)")
        for entry in entries
    ]
    return ToolResult(content=f"{remote}:\n" + "\n".join(lines))


def _preview_list(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    raw = _text(args, "path").strip() or SANDBOX_HOME
    return f"List files under {raw} in the sandbox."


# --------------------------------------------------------------------------
# Listing


def _list_sandboxes(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    rows = list_sessions(db, workspace_id=context.workspace_id)
    if not rows:
        return ToolResult(
            content="No sandboxes yet. run_python creates one automatically."
        )
    return ToolResult(
        content=json.dumps(
            {
                "sandboxes": [
                    {
                        "id": row.id,
                        "label": row.label,
                        "status": row.status,
                        "network_policy": row.network_policy,
                        "project_id": row.project_id,
                        "executions": row.exec_count,
                        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else "",
                    }
                    for row in rows
                ]
            }
        )
    )


_SESSION_ARGUMENT = {
    "session": {
        "type": "string",
        "description": (
            "Existing sandbox id from list_sandboxes. Omit it to use this "
            "workspace's current sandbox, or start one if there is none — "
            "variables, files and installed packages persist across calls to it."
        ),
    }
}


def registry_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """Execution tools, or nothing at all.

    Empty when SANDBOX_ENABLED=0 so a deployment without an execution provider
    simply has no run tools, rather than tools that fail on first use and burn a
    turn teaching the model that they do.
    """
    settings = get_settings()
    if not settings.sandbox_enabled:
        return {}
    return {
        "run_python": ToolSpec(
            name="run_python",
            description=(
                "Run Python in a sandboxed Linux VM and return its output. The "
                "interpreter is persistent: variables, imports and loaded "
                "dataframes survive between calls in the same sandbox. pandas, "
                "numpy and matplotlib are available; plots and figures come back "
                "as artifacts the user can open. The working directory is "
                f"{SANDBOX_HOME}. Use this for real computation — arithmetic on "
                "many numbers, parsing a file, statistics — instead of doing it "
                "in your head."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source to execute."},
                    **_SESSION_ARGUMENT,
                },
                "required": ["code"],
            },
            executor=_run_python,
            read_only=False,
            preview=_preview_run_python,
        ),
        "run_command": ToolSpec(
            name="run_command",
            description=(
                "Run a shell command in the sandbox and return its output. This "
                "is how you install packages: `pip install polars`. Also useful "
                "for ls, unzip, and command-line tools. The working directory is "
                f"{SANDBOX_HOME}, and the command shares the sandbox's filesystem "
                "with run_python."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run."},
                    **_SESSION_ARGUMENT,
                },
                "required": ["command"],
            },
            executor=_run_command,
            read_only=False,
            preview=_preview_run_command,
        ),
        "sandbox_upload": ToolSpec(
            name="sandbox_upload",
            description=(
                "Copy workspace files into the sandbox so code can read them. "
                "Give `paths` — the filenames of uploaded sources — or `project` "
                f"to copy a code project's files. Everything lands in {SANDBOX_HOME}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filenames of uploaded sources, e.g. sales.csv.",
                    },
                    "project": {"type": "string", "description": "Project name to copy in full."},
                    "project_id": {"type": "string", "description": "Project id, if no name."},
                    **_SESSION_ARGUMENT,
                },
            },
            executor=_sandbox_upload,
            read_only=False,
            preview=_preview_upload,
        ),
        "sandbox_download": ToolSpec(
            name="sandbox_download",
            description=(
                "Copy a file the sandbox produced back into the workspace, where "
                "the user can open it. Give a path inside the sandbox, absolute "
                f"or relative to {SANDBOX_HOME}. Returns the saved file's id."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            f"Path in the sandbox, e.g. report.pdf or {SANDBOX_HOME}/out.csv."
                        ),
                    },
                    **_SESSION_ARGUMENT,
                },
                "required": ["path"],
            },
            executor=_sandbox_download,
            read_only=False,
            preview=_preview_download,
        ),
        "sandbox_read": ToolSpec(
            name="sandbox_read",
            description=(
                "Read a text file inside the sandbox and return its contents. "
                "Give a path absolute or relative to "
                f"{SANDBOX_HOME} (e.g. main.py, out/report.md). Use this to see a "
                "file the code wrote before editing it. Binary files are not "
                "returned — use sandbox_download for those."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": f"Path in the sandbox, e.g. main.py or {SANDBOX_HOME}/x.",
                    },
                    **_SESSION_ARGUMENT,
                },
                "required": ["path"],
            },
            executor=_sandbox_read,
        ),
        "sandbox_write": ToolSpec(
            name="sandbox_write",
            description=(
                "Create or overwrite a text file inside the sandbox with the "
                "content you provide. Use this to author a script, a config, or a "
                "data file the sandbox's code will then run or read. The path is "
                f"absolute or relative to {SANDBOX_HOME}, and parent directories "
                "are created as needed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": f"Where to write, e.g. main.py or {SANDBOX_HOME}/a.py.",
                    },
                    "content": {"type": "string", "description": "Full file contents to write."},
                    **_SESSION_ARGUMENT,
                },
                "required": ["path", "content"],
            },
            executor=_sandbox_write,
            read_only=False,
            preview=_preview_write,
        ),
        "sandbox_edit": ToolSpec(
            name="sandbox_edit",
            description=(
                "Edit an existing text file in the sandbox by replacing an exact "
                "piece of text with new text — the same find-and-replace loop you "
                "use to change code. `find` must match the file exactly and be "
                "unique unless you pass all=true. Read the file first to copy the "
                "exact text."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": f"File to edit, e.g. main.py or {SANDBOX_HOME}/src/app.py.",
                    },
                    "find": {"type": "string", "description": "Exact text to replace."},
                    "replace": {"type": "string", "description": "Text to put in its place."},
                    "all": {
                        "type": "boolean",
                        "description": "Replace every occurrence, not just a unique match.",
                    },
                    **_SESSION_ARGUMENT,
                },
                "required": ["path", "find", "replace"],
            },
            executor=_sandbox_edit,
            read_only=False,
            preview=_preview_edit,
        ),
        "sandbox_list": ToolSpec(
            name="sandbox_list",
            description=(
                "List files and directories inside the sandbox. Give a `path` "
                f"(defaults to {SANDBOX_HOME}); pass recursive=true to descend a "
                "few levels. Use this to see what the code produced or what was "
                "uploaded."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": f"Directory to list, defaults to {SANDBOX_HOME}.",
                    },
                    "recursive": {"type": "boolean", "description": "Descend into subdirectories."},
                    **_SESSION_ARGUMENT,
                },
            },
            executor=_sandbox_list,
        ),
        "list_sandboxes": ToolSpec(
            name="list_sandboxes",
            description=(
                "List this workspace's sandboxes with their status and network "
                "policy. Only needed to run something in a specific one; the "
                "other tools pick a sandbox on their own."
            ),
            parameters={"type": "object", "properties": {}},
            executor=_list_sandboxes,
        ),
    }
