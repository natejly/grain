"""Compile-time validation of a workflow DAG. The half that has to be right.

The premise of this module is stated in ADR 0007: **a model that hallucinates a
tool must fail here, loudly, and not at 3am mid-run.** A workflow is unlike a
chat turn in exactly the way that matters — nobody is watching when it executes,
so the last moment a human can be shown a mistake is the moment it is compiled.

So validation is exhaustive rather than early-exit. Every check runs and every
finding is collected, because the caller's next move is either to show a person
the complete list or to hand it back to the model as a repair prompt, and both
are worse with one error at a time.

The checks, and why each one is here:

- **Structure** — ids are slugs, unique, and do not claim the `input` namespace.
- **Edges** reference declared nodes, never a node itself, never twice.
- **Acyclicity** by Kahn's algorithm. A cycle is not a workflow, and a resume
  that skips completed nodes has no meaning in one.
- **Tools exist in this workspace's registry.** Not a static list — `build_registry`
  is per-workspace and includes MCP servers and database connections, so a tool
  that exists for one tenant genuinely does not exist for another.
- **Arguments type-check against the tool's own JSON Schema**, with values that
  are purely a `{{ reference }}` deferred rather than guessed at.
- **References point upstream.** A node reading `{{ later.output }}` compiles
  fine under every check above and reads an empty value at run time. This is the
  check that catches it.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

import jsonschema
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from ..llm_tools import ToolSpec
from .dag import (
    INPUT_NAMESPACE,
    MAX_EDGES,
    MAX_NODES,
    NODE_KEY_RE,
    WHOLE_REFERENCE_RE,
    NodeSpec,
    WorkflowGraph,
    references,
)


@dataclass(frozen=True)
class CompileError:
    """One finding. `code` is stable and machine-readable; `message` is for people."""

    code: str
    message: str
    node: str = ""

    def render(self) -> str:
        where = f" [{self.node}]" if self.node else ""
        return f"{self.code}{where}: {self.message}"


@dataclass
class CompileReport:
    """Errors block; warnings do not.

    The distinction is not cosmetic. `tool_unknown` is a broken program. A tool
    whose *own* JSON Schema is malformed — which an MCP server can ship and the
    workflow author cannot fix — should not make the author's automation
    uncompilable; it should make the fact that its arguments went unchecked
    visible. Same for a write-capable node: not an error, but the single most
    important thing to know before scheduling something unattended.
    """

    errors: List[CompileError] = field(default_factory=list)
    warnings: List[CompileError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> Dict[str, List[Dict[str, str]]]:
        return {
            "errors": [asdict(item) for item in self.errors],
            "warnings": [asdict(item) for item in self.warnings],
        }

    def render(self) -> str:
        return "\n".join(item.render() for item in self.errors)


class WorkflowCompileError(RuntimeError):
    """Raised when a graph cannot be compiled. Carries every finding."""

    def __init__(self, report: CompileReport) -> None:
        super().__init__(report.render() or "workflow compilation failed")
        self.report = report


# --------------------------------------------------------------------------
# Argument type-checking
# --------------------------------------------------------------------------


class _Deferred:
    """A value only known at run time, standing in for `{{ node.output }}`.

    Deliberately not a `str` subclass. If it were, a schema declaring
    `{"type": "string"}` would accept it for the wrong reason and a schema
    declaring `{"type": "integer"}` would reject it for the wrong reason. Being
    an unrelated object means the two keywords that would judge it — `type` and
    `enum` — are the two this module overrides, and every other keyword
    (`pattern`, `minimum`, `properties`, `items`) already ignores instances of
    the wrong kind. `required` still bites, because the key is present.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<deferred>"


DEFERRED = _Deferred()

_BASE_TYPE = Draft202012Validator.VALIDATORS["type"]
_BASE_ENUM = Draft202012Validator.VALIDATORS["enum"]


def _type_allowing_deferred(
    validator: Any, types: Any, instance: Any, schema: Any
) -> Iterable[Any]:
    if instance is DEFERRED:
        return ()
    return _BASE_TYPE(validator, types, instance, schema)


def _enum_allowing_deferred(
    validator: Any, types: Any, instance: Any, schema: Any
) -> Iterable[Any]:
    if instance is DEFERRED:
        return ()
    return _BASE_ENUM(validator, types, instance, schema)


_ArgumentValidator: Any = jsonschema.validators.extend(
    Draft202012Validator,
    {"type": _type_allowing_deferred, "enum": _enum_allowing_deferred},
)


def mask_deferred(value: Any) -> Any:
    """Replace whole-value references with DEFERRED; leave everything else alone.

    `{"text": "{{ prs.output }}"}` cannot be typed now. `{"text": "PRs: {{ prs.output }}"}`
    can: string interpolation yields a string whatever the upstream node returned.
    """
    if isinstance(value, str):
        return DEFERRED if WHOLE_REFERENCE_RE.match(value) else value
    if isinstance(value, dict):
        return {key: mask_deferred(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_deferred(item) for item in value]
    return value


# --------------------------------------------------------------------------
# Cron
# --------------------------------------------------------------------------

_CRON_BOUNDS: Tuple[Tuple[int, int], ...] = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_CRON_NAMES: Tuple[Mapping[str, int], ...] = (
    {},
    {},
    {},
    {
        name: index
        for index, name in enumerate(
            "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1
        )
    },
    {name: index for index, name in enumerate("sun mon tue wed thu fri sat".split())},
)
_CRON_FIELD_RE = re.compile(r"^(?:\*|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?)(?:/\d+)?$")


def cron_error(expression: str) -> Optional[str]:
    """None when `expression` is a valid 5-field cron, else why it is not.

    Five fields only. Nicknames like `@daily` are rejected with a message that
    says what to write instead, because "the model emitted @weekly" is a repair,
    not a mystery.
    """
    text = expression.strip()
    if not text:
        return "a schedule trigger needs a 5-field cron expression"
    if text.startswith("@"):
        return f"cron nicknames are not supported; write 5 fields instead of “{text}”"
    fields = text.split()
    if len(fields) != 5:
        return (
            f"expected 5 cron fields (minute hour day-of-month month day-of-week), "
            f"got {len(fields)} in “{text}”"
        )
    for index, raw in enumerate(fields):
        for part in raw.split(","):
            if not part or not _CRON_FIELD_RE.match(part):
                return f"cron field {index + 1} is not a valid expression: “{raw}”"
            body, _, step = part.partition("/")
            if step and (not step.isdigit() or int(step) == 0):
                return f"cron field {index + 1} has an invalid step: “{part}”"
            if body == "*":
                continue
            low, high = _CRON_BOUNDS[index]
            for token in body.split("-"):
                value = _cron_value(token, _CRON_NAMES[index])
                if value is None:
                    return f"cron field {index + 1} has an unrecognised value: “{token}”"
                if not low <= value <= high:
                    return (
                        f"cron field {index + 1} value {token} is outside {low}-{high}"
                    )
    return None


def _cron_value(token: str, names: Mapping[str, int]) -> Optional[int]:
    if token.isdigit():
        return int(token)
    return names.get(token.lower())


def cron_matches(expression: str, moment: datetime) -> bool:
    """Does `moment` — already in the workflow's own timezone — fall on this cron?

    Deliberately beside `cron_error` rather than in the scheduler. The validator
    decides what a cron expression *is* and the ticker decides when it *fires*,
    and the one bug neither can survive is the two disagreeing: a field the
    validator accepts and the matcher expands differently is an automation that
    compiles and then runs on the wrong day. Sharing the bounds tables and the
    field regex is what stops that.

    Day-of-month and day-of-week follow POSIX: when both are restricted, either
    matching is enough. It is a strange rule and it is the rule every crontab in
    the world already behaves by.
    """
    if cron_error(expression) is not None:
        return False
    fields = expression.strip().split()
    minute, hour, dom, month, dow = (
        _cron_set(index, raw) for index, raw in enumerate(fields)
    )
    if moment.minute not in minute or moment.hour not in hour:
        return False
    if moment.month not in month:
        return False
    # Python's Monday=0 against cron's Sunday=0.
    weekday = (moment.weekday() + 1) % 7
    dom_restricted = fields[2] != "*"
    dow_restricted = fields[4] != "*"
    if dom_restricted and dow_restricted:
        return moment.day in dom or weekday in dow
    if dom_restricted:
        return moment.day in dom
    if dow_restricted:
        return weekday in dow
    return True


def _cron_set(index: int, raw: str) -> Set[int]:
    """Expand one validated cron field into the values it fires on."""
    low, high = _CRON_BOUNDS[index]
    names = _CRON_NAMES[index]
    values: Set[int] = set()
    for part in raw.split(","):
        body, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if body == "*":
            start, end = low, high
        else:
            tokens = body.split("-")
            first = _cron_value(tokens[0], names)
            last = _cron_value(tokens[-1], names) if len(tokens) > 1 else first
            if first is None or last is None:  # pragma: no cover - cron_error ran
                continue
            # `5/15` means "from 5 to the top of the range, every 15", which is
            # not what a bare `5` means and is easy to expand wrongly.
            start, end = first, (high if step_text and len(tokens) == 1 else last)
        values.update(range(start, end + 1, step))
    if index == 4:
        # Both 0 and 7 are Sunday, and a cron written with one must match a
        # weekday computed as the other.
        values = {0 if value == 7 else value for value in values}
    return values


# --------------------------------------------------------------------------
# Graph parsing and validation
# --------------------------------------------------------------------------


def parse_graph(document: Any) -> Tuple[Optional[WorkflowGraph], List[CompileError]]:
    """Pydantic parse, with its errors flattened into CompileErrors.

    A hostile document is far more likely to fail here than anywhere below, and
    the caller needs the same error shape either way — a repair prompt built from
    two different vocabularies is a repair prompt the model gets wrong.
    """
    if not isinstance(document, dict):
        kind = type(document).__name__
        return None, [
            CompileError("schema_invalid", f"expected a JSON object, got {kind}")
        ]
    try:
        return WorkflowGraph.model_validate(document), []
    except ValidationError as exc:
        errors: List[CompileError] = []
        for detail in exc.errors()[:20]:
            location = ".".join(str(part) for part in detail.get("loc", ()))
            errors.append(
                CompileError(
                    "schema_invalid",
                    f"{location or '(root)'}: {detail.get('msg', 'invalid')}",
                )
            )
        return None, errors


def validate_graph(
    graph: WorkflowGraph, registry: Mapping[str, ToolSpec]
) -> CompileReport:
    """Every check, all of them, in one pass. See the module docstring."""
    report = CompileReport()
    ids = _check_identity(graph, report)
    _check_size(graph, report)
    edges = _check_edges(graph, ids, report)
    cyclic = _check_acyclic(ids, edges, report)
    _check_trigger(graph, report)
    for node in graph.nodes:
        _check_node_kind(node, registry, report)
    ancestors = {} if cyclic else _ancestors(ids, edges)
    for node in graph.nodes:
        _check_references(node, ids, ancestors, cyclic=cyclic, report=report)
    return report


def _check_identity(graph: WorkflowGraph, report: CompileReport) -> List[str]:
    """Node ids: slug-shaped, unique, and not squatting on `input`."""
    seen: Set[str] = set()
    ids: List[str] = []
    for node in graph.nodes:
        if not NODE_KEY_RE.match(node.id):
            report.errors.append(
                CompileError(
                    "node_id_invalid",
                    "node ids must be lowercase slugs matching "
                    f"{NODE_KEY_RE.pattern}, got “{node.id}”",
                    node.id,
                )
            )
            continue
        if node.id == INPUT_NAMESPACE:
            report.errors.append(
                CompileError(
                    "node_id_reserved",
                    f"“{INPUT_NAMESPACE}” names the trigger payload and cannot be "
                    "a node id",
                    node.id,
                )
            )
            continue
        if node.id in seen:
            report.errors.append(
                CompileError("node_id_duplicate", f"duplicate node id “{node.id}”", node.id)
            )
            continue
        seen.add(node.id)
        ids.append(node.id)
    if not graph.nodes:
        report.errors.append(CompileError("graph_empty", "a workflow needs at least one node"))
    return ids


def _check_size(graph: WorkflowGraph, report: CompileReport) -> None:
    if len(graph.nodes) > MAX_NODES:
        report.errors.append(
            CompileError(
                "graph_too_large",
                f"{len(graph.nodes)} nodes exceeds the {MAX_NODES}-node ceiling",
            )
        )
    if len(graph.edges) > MAX_EDGES:
        report.errors.append(
            CompileError(
                "graph_too_large",
                f"{len(graph.edges)} edges exceeds the {MAX_EDGES}-edge ceiling",
            )
        )


def _check_edges(
    graph: WorkflowGraph, ids: List[str], report: CompileReport
) -> List[Tuple[str, str]]:
    known = set(ids)
    seen: Set[Tuple[str, str]] = set()
    edges: List[Tuple[str, str]] = []
    for edge in graph.edges:
        pair = (edge.source, edge.target)
        if edge.source == edge.target:
            report.errors.append(
                CompileError(
                    "edge_self_loop", f"node “{edge.source}” depends on itself", edge.source
                )
            )
            continue
        missing = [name for name in pair if name not in known]
        if missing:
            report.errors.append(
                CompileError(
                    "edge_unknown_node",
                    f"edge {edge.source} -> {edge.target} references undeclared "
                    f"node(s): {', '.join(missing)}",
                )
            )
            continue
        if pair in seen:
            report.errors.append(
                CompileError(
                    "edge_duplicate", f"edge {edge.source} -> {edge.target} appears twice"
                )
            )
            continue
        seen.add(pair)
        edges.append(pair)
    return edges


def _check_acyclic(
    ids: List[str], edges: List[Tuple[str, str]], report: CompileReport
) -> bool:
    """Kahn's algorithm. Returns True when a cycle was found and reported."""
    indegree = {name: 0 for name in ids}
    outgoing: Dict[str, List[str]] = {name: [] for name in ids}
    for source, target in edges:
        outgoing[source].append(target)
        indegree[target] += 1
    ready = [name for name in ids if indegree[name] == 0]
    settled = 0
    while ready:
        current = ready.pop()
        settled += 1
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if settled == len(ids):
        return False
    stuck = sorted(name for name in ids if indegree[name] > 0)
    report.errors.append(
        CompileError(
            "graph_cycle",
            "the graph has a cycle; these nodes can never become ready: "
            + ", ".join(stuck),
        )
    )
    return True


def _ancestors(ids: List[str], edges: List[Tuple[str, str]]) -> Dict[str, Set[str]]:
    """Every node reachable *backwards* from each node. Acyclic input only.

    Computed in topological order so each node's ancestor set is the union of its
    parents' — one pass, no repeated traversal.
    """
    incoming: Dict[str, List[str]] = {name: [] for name in ids}
    indegree = {name: 0 for name in ids}
    outgoing: Dict[str, List[str]] = {name: [] for name in ids}
    for source, target in edges:
        incoming[target].append(source)
        outgoing[source].append(target)
        indegree[target] += 1

    order: List[str] = []
    ready = [name for name in ids if indegree[name] == 0]
    while ready:
        current = ready.pop()
        order.append(current)
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)

    ancestors: Dict[str, Set[str]] = {name: set() for name in ids}
    for name in order:
        for parent in incoming[name]:
            ancestors[name].add(parent)
            ancestors[name] |= ancestors[parent]
    return ancestors


def _check_node_kind(
    node: NodeSpec, registry: Mapping[str, ToolSpec], report: CompileReport
) -> None:
    if node.kind == "agent":
        if not node.prompt.strip():
            report.errors.append(
                CompileError("agent_prompt_missing", "an agent node needs a prompt", node.id)
            )
        if node.tool:
            report.errors.append(
                CompileError(
                    "agent_node_names_tool",
                    f"agent node names tool “{node.tool}”; use kind=\"tool\" for a "
                    "specific tool, or drop the field and let the agent choose",
                    node.id,
                )
            )
        if node.arguments:
            report.errors.append(
                CompileError(
                    "agent_node_has_arguments",
                    "an agent node takes a prompt, not tool arguments",
                    node.id,
                )
            )
        return

    if node.prompt.strip():
        report.errors.append(
            CompileError(
                "tool_node_has_prompt",
                "a tool node takes arguments, not a prompt; use kind=\"agent\" to "
                "hand a step to the agent",
                node.id,
            )
        )
    if not node.tool:
        report.errors.append(
            CompileError("tool_missing", "a tool node must name a tool", node.id)
        )
        return
    spec = registry.get(node.tool)
    if spec is None:
        report.errors.append(
            CompileError("tool_unknown", _unknown_tool_message(node.tool, registry), node.id)
        )
        return
    if not spec.read_only:
        report.warnings.append(
            CompileError(
                "tool_write_capable",
                f"“{spec.name}” can write, so this node parks for approval unless "
                "the workspace has granted it a standing allow",
                node.id,
            )
        )
    _check_arguments(node, spec, report)


def _unknown_tool_message(name: str, registry: Mapping[str, ToolSpec]) -> str:
    """Name the mistake and, when it is a near miss, name the fix.

    A model that emitted `list_dataset` for `list_datasets` gets a repair prompt
    it can act on instead of a wall it has to guess its way around.
    """
    close = difflib.get_close_matches(name, list(registry), n=3, cutoff=0.6)
    suffix = f"; did you mean {', '.join(close)}?" if close else ""
    return f"no tool named “{name}” exists in this workspace{suffix}"


def _check_arguments(node: NodeSpec, spec: ToolSpec, report: CompileReport) -> None:
    schema = spec.parameters
    if not isinstance(schema, dict):
        report.warnings.append(
            CompileError(
                "tool_schema_invalid",
                f"“{spec.name}” does not publish an object JSON Schema, so its "
                "arguments were not checked",
                node.id,
            )
        )
        return
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.exceptions.SchemaError as exc:
        report.warnings.append(
            CompileError(
                "tool_schema_invalid",
                f"“{spec.name}” publishes an invalid JSON Schema "
                f"({str(exc.message)[:120]}), so its arguments were not checked",
                node.id,
            )
        )
        return
    validator = _ArgumentValidator(schema)
    for failure in sorted(validator.iter_errors(mask_deferred(node.arguments)), key=str)[:5]:
        location = "/".join(str(part) for part in failure.absolute_path)
        where = f"arguments.{location}" if location else "arguments"
        report.errors.append(
            CompileError(
                "arguments_invalid",
                f"{where} does not match the schema for “{spec.name}”: {failure.message}",
                node.id,
            )
        )


def _check_references(
    node: NodeSpec,
    ids: List[str],
    ancestors: Dict[str, Set[str]],
    *,
    cyclic: bool,
    report: CompileReport,
) -> None:
    """Every `{{ ... }}` names a real, upstream node — or the trigger payload."""
    known = set(ids)
    for name in references([node.arguments, node.prompt, node.description]):
        if name == INPUT_NAMESPACE:
            continue
        if name not in known:
            report.errors.append(
                CompileError(
                    "reference_unknown_node",
                    f"“{{{{ {name}.… }}}}” references a node that does not exist",
                    node.id,
                )
            )
            continue
        if cyclic:
            continue
        if name not in ancestors.get(node.id, set()):
            report.errors.append(
                CompileError(
                    "reference_not_upstream",
                    f"“{name}” is referenced but is not upstream of this node; add "
                    f"an edge from “{name}” or the value will be empty at run time",
                    node.id,
                )
            )


def edges_of(graph: WorkflowGraph) -> List[Tuple[str, str]]:
    """The edge list as pairs, for callers that already know the graph is valid."""
    return [(edge.source, edge.target) for edge in graph.edges]


def topological_order(graph: WorkflowGraph) -> List[str]:
    """Execution order for a validated graph.

    Ties are broken by declaration order so a workflow runs the same way twice —
    a re-run that visits nodes in a different order is a re-run whose logs cannot
    be diffed against the first.
    """
    ids = graph.node_ids()
    position = {name: index for index, name in enumerate(ids)}
    indegree = {name: 0 for name in ids}
    outgoing: Dict[str, List[str]] = {name: [] for name in ids}
    for source, target in edges_of(graph):
        outgoing[source].append(target)
        indegree[target] += 1
    ready = sorted((name for name in ids if indegree[name] == 0), key=position.get)  # type: ignore[arg-type]
    order: List[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for target in sorted(outgoing[current], key=position.get):  # type: ignore[arg-type]
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
        ready.sort(key=position.get)  # type: ignore[arg-type]
    if len(order) != len(ids):
        raise WorkflowCompileError(
            CompileReport(errors=[CompileError("graph_cycle", "graph is not acyclic")])
        )
    return order


def _check_trigger(graph: WorkflowGraph, report: CompileReport) -> None:
    trigger = graph.trigger
    if trigger.kind == "schedule":
        problem = cron_error(trigger.cron)
        if problem:
            report.errors.append(CompileError("trigger_cron_invalid", problem))
    elif trigger.cron.strip():
        report.errors.append(
            CompileError(
                "trigger_cron_unexpected",
                "a manual trigger must not carry a cron expression; set "
                'kind="schedule" if it should run on a schedule',
            )
        )
