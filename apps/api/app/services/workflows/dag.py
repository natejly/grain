"""The workflow DAG document: what a compiled automation actually is.

A graph is a plain JSON document — nodes, edges, a trigger — and this module is
its grammar. Nothing here executes anything or touches the database; it is the
shape the compiler produces, the validator checks, and `workflows.graph_json`
stores.

Two design choices are load-bearing:

**Every model is `extra="forbid"`.** A language model that invents a field is
telling you it has invented something else too. Silently dropping `"retry": 3`
would produce a graph that validates, stores, and then does not retry — a lie
discovered at 3am. Refusing it produces a compile error the repair pass can fix.

**References are a closed syntax.** `{{ node_id.output }}` names an upstream
node's result and `{{ input.field }}` names the run's inputs. That is the whole
language. It is deliberately not an expression evaluator: a workflow is a stored
program that a scheduler may run unattended, and the smallest thing that carries
data between nodes is the one with the least to audit.

**Inputs are declared, not discovered.** `{{ input.repo }}` used to mean
"whatever the caller happened to put under `repo`". An `InputSpec` gives it a
type, a label and a default, which is what makes a workflow a *parameterised*
program rather than a fixed one: the same graph runs against a different repo,
a UI can render the form without guessing, and a wrong value is refused at run
start instead of at node six. See `inputs.py`.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterator, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

#: Node ids are slugs, so they can be a `{{ reference }}` prefix, a database
#: `node_key`, and a UI anchor without any escaping anywhere.
NODE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: The namespace a node may not claim, because the run's inputs own it.
INPUT_NAMESPACE = "input"

#: Input names the system supplies itself, so a graph may read them without
#: declaring them and may not declare them at all. `schedule.dispatch_due` puts
#: `scheduled_for` on every run it starts; a workflow that declared it would be
#: promising a value the ticker is going to overwrite anyway.
TRIGGER_INPUT_NAMES = ("scheduled_for",)

#: The JSON types an input may declare. Deliberately JSON Schema's own names —
#: `inputs.property_schema` emits them verbatim, so there is no mapping table
#: between what an author writes and what the validator enforces.
INPUT_TYPES = ("string", "number", "integer", "boolean", "object", "array")

#: `{{ node.output }}`, `{{ node.output.some.field }}`, `{{ input.field }}`.
#: Group 1 is the namespace — a node id or `input` — which is the only part that
#: can be checked before the run exists.
REFERENCE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)((?:\.[A-Za-z0-9_]+)*)\s*\}\}")

#: A value that is *nothing but* one reference. Its type is whatever the upstream
#: node returns, so it cannot be type-checked now; a reference embedded in a
#: larger string is a string at run time and is checked as one.
WHOLE_REFERENCE_RE = re.compile(
    r"^\s*\{\{\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*\s*\}\}\s*$"
)

#: A ceiling, not a target. A model asked for "summarise my week" that emits
#: sixty nodes has misunderstood, and the failure should be legible.
MAX_NODES = 40
MAX_EDGES = 120

#: A form nobody will fill in is a workflow nobody will run. The ceiling is a
#: statement about the UI at the other end, not about the executor.
MAX_INPUTS = 20


class TriggerSpec(BaseModel):
    """What starts the workflow.

    `schedule` records a cron expression and a timezone. Nothing dispatches one
    yet — see ADR 0007 — so a schedule is a recorded intent, and the compiler
    still validates the expression rather than storing a string nobody has read.

    `webhook` means an external system starts runs through
    `POST /api/hooks/workflows/{id}/trigger` with a workspace API token; like
    `manual` it carries no cron, and the trigger's JSON body becomes the run's
    `{{ input.* }}` payload.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["manual", "schedule", "webhook"] = "manual"
    cron: str = ""
    timezone: str = "UTC"


class InputSpec(BaseModel):
    """One parameter of the workflow: what a run must supply, and how to ask.

    Every field earns its place by being needed *somewhere no code can guess it*.
    `name` is the `{{ input.name }}` key and the form field's id. `type` is what
    the run-start check enforces and what tells the form to render a checkbox
    rather than a text box. `label` and `description` are what the form says to a
    person, because a field labelled `dom_repo` is a field they will get wrong.
    `required` decides whether a run may omit it. `default` pre-fills the form
    and — the case that matters — supplies the value when a *scheduled* run fires
    with nobody there to type one. `choices` turns the field into a select and is
    enforced as an enum, so "which environment" cannot become "prodd".

    `default = None` means *no default*, not "defaults to null". A JSON null that
    a workflow could distinguish from absence would buy a distinction nothing
    else in this grammar makes.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    type: Literal["string", "number", "integer", "boolean", "object", "array"] = "string"
    label: str = ""
    description: str = ""
    required: bool = True
    default: Any = None
    choices: List[Any] = Field(default_factory=list)


#: The comparisons a node's `when` guard may make. A closed, named set rather
#: than an expression language, for exactly the reason the reference syntax is
#: closed: "when should this step run" is part of a stored program a scheduler
#: runs unattended, and the smallest thing that can gate it is the one with the
#: least to audit. `truthy/falsy/present/absent` read only the left operand.
GUARD_OPERATORS = (
    "eq",
    "ne",
    "gt",
    "lt",
    "gte",
    "lte",
    "truthy",
    "falsy",
    "present",
    "absent",
    "in",
)

#: The operators that read only `left`; `right` is meaningless for them and the
#: validator refuses one.
UNARY_OPERATORS = ("truthy", "falsy", "present", "absent")


class GuardSpec(BaseModel):
    """A structured condition on whether a node runs.

    `left` is a reference — `{{ node.output.field }}` or `{{ input.field }}` —
    resolved against the run's values; `op` names the comparison; `right` is the
    literal (or a second reference) it is compared against, and is unused by the
    unary operators. It is *data*, not a parsed expression, so a person reviewing
    a workflow can read the condition without running an evaluator in their head,
    and nothing here can reach beyond a comparison. A reference that names a step
    which was itself skipped resolves to *absent*, which is false for every
    comparison and answerable by `present`/`absent`.
    """

    model_config = ConfigDict(extra="forbid")

    left: str
    op: Literal[
        "eq", "ne", "gt", "lt", "gte", "lte", "truthy", "falsy", "present", "absent", "in"
    ]
    right: Any = None


class NodeSpec(BaseModel):
    """One step. A named tool call, one turn of the agent loop, or a human pause.

    A `tool` node is deterministic: the tool, the arguments, the workspace's
    policy for it. An `agent` node is a prompt handed to the existing agent loop,
    which may call tools of its own — each one policy-gated exactly as in chat.
    The distinction matters for review: a reader can see every tool a `tool` node
    will call, and cannot for an `agent` node.

    A `manual` node runs no tool at all: it parks the workflow for a person, shows
    them `prompt`, and collects the values declared in `fields` — the same
    `InputSpec` shape a workflow's own inputs use. Proceeding turns those values
    into the node's output object, readable downstream as `{{ node.output.field }}`;
    rejecting cancels the run. It calls nothing and names no `tool`, so its `fields`
    are the only thing a reviewer has to read to know what it does.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["tool", "agent", "manual"]
    description: str = ""
    tool: str = ""
    arguments: Dict[str, Any] = Field(default_factory=dict)
    prompt: str = ""
    #: A manual node's collected values, declared exactly as workflow inputs are.
    #: Empty on `tool`/`agent` nodes, which collect nothing. See `NodeSpec` above
    #: and `validate._check_manual`.
    fields: List[InputSpec] = Field(default_factory=list)
    #: An optional condition on whether this node runs at all. When it evaluates
    #: false the node is skipped — and so is anything reachable only through it,
    #: which is how a `when` on one node prunes a whole branch. See
    #: `validate._check_guard` and `executor._walk`.
    when: Optional[GuardSpec] = None
    #: The authored agent an `agent` node runs as, by id. "" means the workspace
    #: default (its oldest enabled agent) — which is what every graph compiled
    #: before this field existed ran as, so stored graphs keep their meaning.
    #: Set by the workflow editor's picker, never by the compiler: the model is
    #: not taught agent ids. See `validate._check_agents` and
    #: `executor._execute_agent_node`.
    agent: str = ""


class EdgeSpec(BaseModel):
    """A dependency: `target` runs after `source` and may reference its output."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: str = Field(alias="from")
    target: str = Field(alias="to")


class WorkflowGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    trigger: TriggerSpec = Field(default_factory=TriggerSpec)
    #: Declared parameters. Empty means the old contract — `{{ input.x }}` reads
    #: whatever the caller passed and nothing is checked — which is what keeps
    #: graphs compiled before inputs existed running unchanged.
    inputs: List[InputSpec] = Field(default_factory=list)
    nodes: List[NodeSpec]
    edges: List[EdgeSpec] = Field(default_factory=list)

    def input_names(self) -> List[str]:
        return [item.name for item in self.inputs]

    def node_ids(self) -> List[str]:
        return [node.id for node in self.nodes]

    def to_document(self) -> Dict[str, Any]:
        """The JSON stored in `workflows.graph_json`, edges spelled from/to."""
        return self.model_dump(by_alias=True)


def _matches(value: Any) -> Iterator[re.Match[str]]:
    """Every reference inside a value, in document order.

    Walks dicts, lists and strings, because arguments are arbitrary JSON and a
    reference can be buried at any depth. One walk, so the namespace check and
    the input-field check cannot come to disagree about where a reference is
    allowed to hide.
    """
    if isinstance(value, str):
        yield from REFERENCE_RE.finditer(value)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _matches(item)
    elif isinstance(value, list):
        for item in value:
            yield from _matches(item)


def references(value: Any) -> List[str]:
    """Every namespace referenced anywhere inside a value, in first-seen order."""
    found: List[str] = []
    for match in _matches(value):
        if match.group(1) not in found:
            found.append(match.group(1))
    return found


def input_fields(value: Any) -> List[str]:
    """Every `{{ input.field }}` name referenced, in first-seen order.

    The first dotted segment only: `{{ input.repo.owner }}` is the declared input
    `repo` read one level in, which is a legal thing to do to an `object` input
    and is nobody's business but `refs._descend`'s. A bare `{{ input }}` names
    the whole payload and yields nothing to check.
    """
    found: List[str] = []
    for match in _matches(value):
        if match.group(1) != INPUT_NAMESPACE:
            continue
        parts = [part for part in match.group(2).split(".") if part]
        if parts and parts[0] not in found:
            found.append(parts[0])
    return found
