"""Workspace-defined sandbox tools: the registry source for custom descriptors.

A `SandboxTool` row is a tool the workspace authored; this module turns each
enabled one into a `ToolSpec` the model can call, and runs the call in a sandbox
frozen to *that tool's* egress allowlist under *that tool's* approval policy.

Three properties are load-bearing and each has one home:

*Per-tool egress restricts and never widens.* Execution goes through
`ensure_tool_session`, whose spec flows into `provider.create` -> `egress_rules`,
so policy.py's metadata/RFC1918/loopback denials are reapplied unconditionally.
`tool_egress` only ever narrows to the declared hosts (or tighter) — there is no
branch that returns `open`.

*No shell/command injection.* `_substitute` renders `{{param}}` placeholders and
then `shlex.quote`s every token, so each argv element becomes exactly one shell
word no matter what an argument contains. That is the one construction that is
safe across both the E2B bash path (`commands.run`) and the local `shlex.split`
path. A leftover placeholder renders empty rather than passing `{{x}}` through.

*Approval only tightens.* `force_ask=(approval == "always")` on the spec makes
`evaluate_policy` clamp any resulting `allow` to `ask`; a deny is untouched.

The exec plumbing (which run, the runaway cap, result rendering) is imported
from `tools.py` rather than forked: a custom execution must share the same
per-turn cap and the same result budget as `run_python`/`run_command`.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...config import get_settings
from ...models import SandboxTool
from ..llm_tools import ToolContext, ToolResult, ToolSpec
from .outputs import persist_artifacts
from .provider import get_provider
from .session import (
    ensure_tool_session,
    handle_for,
    record_execution,
    tool_egress,
)
from .tools import _current_call, _over_exec_cap, _render
from .types import SandboxError, SandboxQuotaError

#: A `{{name}}` reference. Names are validated at the create route against the
#: schema's declared properties, so any that survives substitution is a param
#: the model chose not to supply; it renders empty rather than leaking.
_PLACEHOLDER_RE = re.compile(r"\{\{[a-zA-Z0-9_]+\}\}")


def _scalar(value: Any) -> str:
    """One argument value as a single unquoted string.

    Quoting happens once, in `_substitute`, over the whole rendered token — so
    this only has to produce the text, not make it shell-safe.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    # Arrays/objects become a JSON string, still a single token once quoted.
    return json.dumps(value)


def _substitute(argv_template: Sequence[str], args: Dict[str, Any]) -> List[str]:
    """Render `{{param}}` placeholders, then `shlex.quote` every token.

    No shell injection: each token becomes exactly one shell word regardless of
    what the arg contains, so the E2B bash path and the local shlex-split path
    both see the argv the author declared. A placeholder with no supplied arg
    renders empty — never passed through as the literal `{{x}}`.
    """
    import shlex

    out: List[str] = []
    for token in argv_template:
        rendered = str(token)
        for key, value in args.items():
            rendered = rendered.replace("{{" + str(key) + "}}", _scalar(value))
        rendered = _PLACEHOLDER_RE.sub("", rendered)
        out.append(shlex.quote(rendered))
    return out


def _egress_line(tool: SandboxTool) -> str:
    """The network sentence an approval card must show: code without its egress
    is unreviewable, so the same fact the builtin `_policy_line` renders is shown
    here from the tool's own (tightened) egress."""
    settings = get_settings()
    try:
        hosts = json.loads(tool.egress_hosts_json or "[]")
    except ValueError:
        hosts = []
    hosts = [str(h) for h in hosts] if isinstance(hosts, list) else []
    policy_value, allowed = tool_egress(settings, hosts)
    if policy_value == "none":
        return "network: none — this tool has no outbound access"
    allowed_text = ", ".join(allowed) if allowed else "nothing"
    return f"network: allowlist — it can reach {allowed_text}, and nothing else"


def _load_argv(tool: SandboxTool) -> List[str]:
    try:
        parsed = json.loads(tool.argv_json or "[]")
    except ValueError:
        return []
    return [str(token) for token in parsed] if isinstance(parsed, list) else []


def _preview(tool_id: str):
    def preview(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
        tool = db.get(SandboxTool, tool_id)
        if tool is None or tool.workspace_id != context.workspace_id:
            return "This custom sandbox tool is no longer available."
        command = " ".join(_substitute(_load_argv(tool), args))
        return (
            f"Run this custom tool `{tool.name}` in a sandbox "
            f"({_egress_line(tool)}):\n\n```bash\n{command}\n```"
        )

    return preview


def _executor(tool_id: str):
    """A closure over the tool id that re-loads the row on every call.

    Reloading rather than capturing the row means an edit or a disable takes
    effect on the next call — a custom tool the workspace turned off must stop
    running, and a tightened egress must apply immediately, not after a restart.
    """

    def execute(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
        settings = get_settings()
        tool = db.get(SandboxTool, tool_id)
        if (
            tool is None
            or tool.workspace_id != context.workspace_id
            or not tool.enabled
        ):
            return ToolResult(content="Error: that sandbox tool is no longer available.")

        run_id, tool_call_id = _current_call(db, context)
        refusal = _over_exec_cap(
            db, workspace_id=context.workspace_id, run_id=run_id, settings=settings
        )
        if refusal:
            return ToolResult(content=refusal)

        command = " ".join(_substitute(_load_argv(tool), args))
        try:
            session = ensure_tool_session(
                db,
                workspace_id=context.workspace_id,
                user_id=context.user_id,
                settings=settings,
                tool=tool,
            )
        except SandboxQuotaError as exc:
            return ToolResult(content=str(exc))
        except SandboxError as exc:
            return ToolResult(content=f"Error: {exc}")

        try:
            provider = get_provider(settings)
            result = provider.run_command(
                handle_for(session),
                command,
                timeout=settings.sandbox_exec_timeout_seconds,
            )
        except SandboxQuotaError as exc:
            return ToolResult(content=str(exc))
        except SandboxError as exc:
            # Infrastructure, not the tool's fault — named so the model retries
            # the turn rather than "fixing" a command that was never wrong.
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
            kind="command",
            source=command,
            result=result,
            settings=settings,
            artifacts=descriptors,
        )
        dropped = len(result.artifacts) - len(descriptors)
        return ToolResult(
            content=_render(result, descriptors, dropped=dropped),
            artifacts=descriptors,
        )

    return execute


def registry_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """Every enabled custom tool as a ToolSpec, or nothing.

    Empty when SANDBOX_ENABLED=0 for the same reason the builtin run_* family is:
    without an execution provider these tools would fail on first use and burn a
    turn teaching the model that they do.
    """
    settings = get_settings()
    if not settings.sandbox_enabled:
        return {}
    rows = db.scalars(
        select(SandboxTool).where(
            SandboxTool.workspace_id == context.workspace_id,
            SandboxTool.enabled.is_(True),
        )
    )
    specs: Dict[str, ToolSpec] = {}
    for tool in rows:
        try:
            schema = json.loads(tool.input_schema_json)
            if not isinstance(schema, dict):
                raise ValueError
        except (ValueError, TypeError):
            schema = {"type": "object", "properties": {}}
        specs[tool.name] = ToolSpec(
            name=tool.name,
            description=(tool.description or f"Custom sandbox tool {tool.name}")[:1000],
            parameters=schema,
            executor=_executor(tool.id),
            # It executes, so it is ask-by-default like every write tool; a
            # workspace ToolPolicy row can promote it to allow.
            read_only=False,
            # approval="always" tightens that to a forced prompt — see
            # evaluate_policy. It can only escalate an allow, never loosen a deny.
            force_ask=(tool.approval == "always"),
            preview=_preview(tool.id),
        )
    return specs
