"""Natural language in, a validated DAG out.

This is the half of workflow automation that is genuinely hard and genuinely
independent of how the graph later executes. "Every Monday, pull the open PRs,
summarise them, and post to Slack" becomes a document that has been checked
against *this workspace's* tool registry before anybody can save it.

The shape of the loop:

    render the workspace's tools  ->  ask the model for JSON  ->  parse  ->
    validate  ->  on failure, hand the errors back once and try again

The repair pass is not a nicety. Structured-output models fail on the long tail —
a tool name off by an `s`, an edge to a node they renamed, a cron with six
fields — and every one of those is mechanically describable. Handing the model a
list of `CompileError`s recovers most of them, and the ones it cannot recover
still fail closed. What never happens is a graph reaching the database because
we gave up checking.

Compilation grants nothing. The graph names tools; the workspace's
`tool_policies` decide at execution time whether each one runs or parks. A
compiled workflow is a proposal, not a permission.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

from sqlalchemy.orm import Session

from ...config import Settings, get_settings
from ..llm_tools import ToolContext, ToolSpec, build_registry
from ..model import _openai_client, privacy_safe_identifier
from .dag import MAX_NODES, WorkflowGraph
from .validate import (
    CompileError,
    CompileReport,
    WorkflowCompileError,
    parse_graph,
    validate_graph,
)

#: Takes (instructions, input_text) and returns the model's raw text. Narrower
#: than agent_loop.ModelStep on purpose: compiling a workflow is one structured
#: completion with no tools and no streaming, and a seam that small is a seam a
#: test can fill with a literal string.
CompilerStep = Callable[[str, str], str]

MAX_CATALOGUE_TOOLS = 120
MAX_SCHEMA_CHARS = 700

WORKFLOW_INSTRUCTIONS = f"""You turn a description of an automation into a workflow DAG.

Reply with one JSON object and nothing else. No prose, no code fence.

{{
  "name": "short human name",
  "description": "one sentence",
  "trigger": {{"kind": "manual" | "schedule", "cron": "5-field cron", "timezone": "IANA zone"}},
  "nodes": [
    {{"id": "slug", "kind": "tool", "tool": "exact_tool_name",
      "arguments": {{...}}, "description": "why this step exists"}},
    {{"id": "slug", "kind": "agent", "prompt": "what to do", "description": "..."}}
  ],
  "edges": [{{"from": "slug", "to": "slug"}}]
}}

Rules, all enforced by a validator that will reject your answer:

1. `tool` MUST be one of the tool names listed below, spelled exactly. Never
   invent a tool. If the automation needs something no listed tool provides,
   use an "agent" node and describe the goal in its prompt.
2. `arguments` MUST match the tool's JSON Schema shown below.
3. Node ids are lowercase slugs (letters, digits, underscore) and unique.
   "input" is reserved.
4. Every edge's "from" and "to" MUST name a declared node. No cycles. No node
   depending on itself. No duplicate edges.
5. To use an earlier node's result, write {{{{ node_id.output }}}} inside a
   string. The referenced node MUST be upstream — connected by edges that reach
   this node. To use the trigger payload, write {{{{ input.field }}}}.
6. "kind": "schedule" REQUIRES a 5-field cron (minute hour day-of-month month
   day-of-week). No @daily / @weekly nicknames. "kind": "manual" must omit cron.
7. A tool node has `tool` and `arguments` and no `prompt`. An agent node has
   `prompt` and neither `tool` nor `arguments`.
8. No fields other than those shown. Unknown fields are rejected.
9. At most {MAX_NODES} nodes. Prefer the smallest graph that does the job.

Tools marked (writes) will pause the workflow for human approval before they
run. That is expected — do not avoid them, and do not pretend a read-only tool
can do a write.
"""

REPAIR_INSTRUCTIONS = """Your previous workflow JSON failed validation.

Fix every listed error and reply with the corrected JSON object only. Keep the
parts that were right. Obey the original rules — especially: never invent a tool
name, and only reference nodes that are upstream.
"""


@dataclass
class CompiledWorkflow:
    """A graph that passed every check, plus what the checks want you to know."""

    graph: WorkflowGraph
    warnings: List[CompileError]
    attempts: int
    raw_response: str = ""

    @property
    def requires_approval(self) -> bool:
        """True when at least one node names a write-capable tool.

        The question a scheduler needs answered before it runs anything
        unattended: will this park, or will it complete on its own?
        """
        return any(item.code == "tool_write_capable" for item in self.warnings)


def render_tool_catalogue(
    registry: Mapping[str, ToolSpec], *, limit: int = MAX_CATALOGUE_TOOLS
) -> str:
    """The workspace's tools, as the model will see them.

    The registry is per-workspace — MCP servers, database connections and
    integrations all contribute — so this catalogue is the definition of "a tool
    that exists" for this compile, and the validator checks against the same
    mapping. There is no static list to drift from.
    """
    lines: List[str] = []
    for name in sorted(registry)[:limit]:
        spec = registry[name]
        marker = " (writes)" if not spec.read_only else ""
        schema = json.dumps(spec.parameters, sort_keys=True)
        if len(schema) > MAX_SCHEMA_CHARS:
            schema = schema[:MAX_SCHEMA_CHARS] + "…"
        lines.append(f"- {name}{marker}: {spec.description}\n  schema: {schema}")
    if len(registry) > limit:
        lines.append(f"- (+{len(registry) - limit} more tools not shown)")
    return "\n".join(lines) if lines else "(this workspace has no tools)"


def _compile_input(source_prompt: str, catalogue: str) -> str:
    return (
        f"Automation to build:\n{source_prompt.strip()}\n\n"
        f"Tools available in this workspace:\n{catalogue}"
    )


def _repair_input(
    source_prompt: str, catalogue: str, previous: str, report: CompileReport
) -> str:
    return (
        f"{_compile_input(source_prompt, catalogue)}\n\n"
        f"Your previous answer:\n{previous}\n\n"
        f"Validation errors:\n{report.render()}"
    )


def default_step(settings: Settings, *, user_id: str) -> CompilerStep:
    """The model behind a compile: OpenAI in product, the script double in tests."""
    if settings.active_model_provider == "scripted":
        from ..scripted_model import scripted_workflow_graph

        def scripted(instructions: str, input_text: str) -> str:
            return scripted_workflow_graph(input_text)

        return scripted

    client = _openai_client(settings)

    def step(instructions: str, input_text: str) -> str:
        response = client.responses.create(
            model=settings.openai_model,
            instructions=instructions,
            input=input_text,
            reasoning={"effort": settings.openai_reasoning_effort},
            text={"verbosity": "low"},
            max_output_tokens=settings.openai_codegen_max_output_tokens,
            safety_identifier=privacy_safe_identifier(user_id),
            store=False,
        )
        return str(response.output_text or "")

    return step


def parse_model_json(raw: str) -> Any:
    """The model's text as JSON, tolerating a fence and nothing else.

    Deliberately not `model._parsed_json_object`, which returns `{}` on failure
    because a memory extraction that finds nothing is fine. Here, "the model did
    not return JSON" is a compile error the user must see, so the malformed text
    is returned as-is and `parse_graph` reports it.
    """
    text = raw.strip()
    if text.startswith("```"):
        newline = text.find("\n")
        text = text[newline + 1 :] if newline >= 0 else text
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def compile_document(
    document: Any, registry: Mapping[str, ToolSpec]
) -> CompiledWorkflow:
    """Validate an already-formed graph document. Raises WorkflowCompileError.

    The entry point for a hand-edited graph, and the one the compiler below uses
    for every attempt — so a graph typed by a person and a graph written by a
    model are held to exactly the same standard.
    """
    graph, errors = parse_graph(document)
    if graph is None:
        raise WorkflowCompileError(CompileReport(errors=errors))
    report = validate_graph(graph, registry)
    if not report.ok:
        raise WorkflowCompileError(report)
    return CompiledWorkflow(graph=graph, warnings=report.warnings, attempts=0)


def compile_workflow(
    db: Session,
    context: ToolContext,
    *,
    source_prompt: str,
    settings: Optional[Settings] = None,
    step: Optional[CompilerStep] = None,
    max_repairs: int = 1,
    registry: Optional[Mapping[str, ToolSpec]] = None,
) -> CompiledWorkflow:
    """Compile natural language into a validated DAG, or raise with every error.

    `registry` defaults to this workspace's live tool registry. It is injectable
    only so a caller that already built one does not build it twice — never to
    widen it. Passing a registry the workspace does not have would let a
    workflow name a tool the workspace cannot call, which the executor would then
    refuse at run time; the point of this function is that it refuses now.
    """
    settings = settings or get_settings()
    tools = registry if registry is not None else build_registry(db, context)
    catalogue = render_tool_catalogue(tools)
    call = step or default_step(settings, user_id=context.user_id)

    raw = call(WORKFLOW_INSTRUCTIONS, _compile_input(source_prompt, catalogue))
    attempt = 1
    last: WorkflowCompileError
    while True:
        try:
            compiled = compile_document(parse_model_json(raw), tools)
        except WorkflowCompileError as exc:
            last = exc
        else:
            compiled.attempts = attempt
            compiled.raw_response = raw
            return compiled
        if attempt > max_repairs:
            raise last
        raw = call(
            REPAIR_INSTRUCTIONS,
            _repair_input(source_prompt, catalogue, raw, last.report),
        )
        attempt += 1


def summarize(compiled: CompiledWorkflow) -> Dict[str, Any]:
    """A compact record of a successful compile, for an API response or a log."""
    return {
        "name": compiled.graph.name,
        "description": compiled.graph.description,
        "trigger": compiled.graph.trigger.model_dump(),
        "node_count": len(compiled.graph.nodes),
        "edge_count": len(compiled.graph.edges),
        "requires_approval": compiled.requires_approval,
        "attempts": compiled.attempts,
        "warnings": [
            {"code": item.code, "node": item.node, "message": item.message}
            for item in compiled.warnings
        ],
    }
