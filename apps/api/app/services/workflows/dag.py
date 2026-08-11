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
node's result and `{{ input.field }}` names the trigger payload. That is the
whole language. It is deliberately not an expression evaluator: a workflow is a
stored program that a scheduler may run unattended, and the smallest thing that
carries data between nodes is the one with the least to audit.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Node ids are slugs, so they can be a `{{ reference }}` prefix, a database
#: `node_key`, and a UI anchor without any escaping anywhere.
NODE_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: The namespace a node may not claim, because the trigger payload owns it.
INPUT_NAMESPACE = "input"

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


class TriggerSpec(BaseModel):
    """What starts the workflow.

    `schedule` records a cron expression and a timezone. Nothing dispatches one
    yet — see ADR 0007 — so a schedule is a recorded intent, and the compiler
    still validates the expression rather than storing a string nobody has read.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["manual", "schedule"] = "manual"
    cron: str = ""
    timezone: str = "UTC"


class NodeSpec(BaseModel):
    """One step. Either a named tool call or one turn of the agent loop.

    A `tool` node is deterministic: the tool, the arguments, the workspace's
    policy for it. An `agent` node is a prompt handed to the existing agent loop,
    which may call tools of its own — each one policy-gated exactly as in chat.
    The distinction matters for review: a reader can see every tool a `tool` node
    will call, and cannot for an `agent` node.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["tool", "agent"]
    description: str = ""
    tool: str = ""
    arguments: Dict[str, Any] = Field(default_factory=dict)
    prompt: str = ""


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
    nodes: List[NodeSpec]
    edges: List[EdgeSpec] = Field(default_factory=list)

    def node_ids(self) -> List[str]:
        return [node.id for node in self.nodes]

    def to_document(self) -> Dict[str, Any]:
        """The JSON stored in `workflows.graph_json`, edges spelled from/to."""
        return self.model_dump(by_alias=True)


def references(value: Any) -> List[str]:
    """Every namespace referenced anywhere inside a value, in first-seen order.

    Walks dicts, lists and strings, because arguments are arbitrary JSON and a
    reference can be buried at any depth.
    """
    found: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            for match in REFERENCE_RE.finditer(node):
                name = match.group(1)
                if name not in found:
                    found.append(name)
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return found
