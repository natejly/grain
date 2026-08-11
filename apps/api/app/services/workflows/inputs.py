"""Typed workflow inputs: what a run must supply, judged before it starts.

`{{ input.field }}` has always been resolvable — `refs.py` reads it out of
whatever dict the caller passed to `start_run`. What was missing is the other
half of a parameter: a *declaration*. A workflow that says nothing about its
inputs is a workflow whose form cannot be rendered, whose scheduled run cannot
be checked, and whose mistakes surface as a `reference_unresolved` on node six.

So `WorkflowGraph.inputs` declares them and this module is the two things a
declaration is for:

**A JSON Schema for the whole payload**, which is what both the check below and a
form renderer want, and which is generated from one place so the two cannot
disagree about what "required" means.

**Binding, once, at run start.** ADR 0007's argument for compile-time validation
is that nobody is watching; the same argument applies one level down. A run that
fails on node six because input two was a string has already sent five nodes'
worth of real email. `bind` applies defaults, checks the result, and raises with
*every* problem rather than the first — the caller's next move is to show a
person a form with its fields marked, and one error at a time makes that worse.

Binding's post-condition is what the rest of the system leans on: **every
declared input has a value afterwards.** `validate._check_input_supply` is what
makes that reachable, by refusing at compile time the two declarations that
could not satisfy it. So a node reading `{{ input.x }}` for a declared `x` never
raises `reference_unresolved`, and the run-start check is the only place inputs
can fail.

Two rules that look like omissions and are not:

**A missing value is never coerced.** `"5"` in an integer input is refused, for
the reason `executor._resolved_arguments` refuses it: a coerced value makes the
tool do something subtly wrong instead of nothing at all. The declaration is
published, so the caller knows which JSON type to send.

**An undeclared key is kept, not rejected.** The scheduler supplies
`scheduled_for` on every dispatched run and workflows written before this module
existed carry free-form payloads. Strictness lives on the declaration side —
what is declared is checked — and `validate.py` is what catches a node reading an
`{{ input.x }}` nobody declared.
"""
from __future__ import annotations

import copy
from typing import Any, Dict, List, Mapping

from jsonschema import Draft202012Validator

from .dag import InputSpec, WorkflowGraph

#: Enough findings to fix a form in one pass. A payload producing more than this
#: is wrong in a way the eleventh message will not clarify.
MAX_PROBLEMS = 10


class InputBindingError(ValueError):
    """A run's supplied inputs do not satisfy the workflow's declaration."""

    def __init__(self, problems: List[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def property_schema(spec: InputSpec) -> Dict[str, Any]:
    """One input as a JSON Schema property.

    `title` and `description` are carried because a form renderer reads this
    schema, and a field labelled by its slug is a field a person has to guess at.
    """
    schema: Dict[str, Any] = {"type": spec.type}
    if spec.choices:
        schema["enum"] = list(spec.choices)
    if spec.label:
        schema["title"] = spec.label
    if spec.description:
        schema["description"] = spec.description
    if spec.default is not None:
        schema["default"] = spec.default
    return schema


def input_schema(graph: WorkflowGraph) -> Dict[str, Any]:
    """The payload contract for one run of this workflow.

    `additionalProperties` is open on purpose — see the module docstring on
    undeclared keys. `required` lists the inputs with no default, *and* the
    required ones that have one: a default is applied before this schema is
    checked, so both are present by then and the list stays honest about which
    fields a form must not leave empty.
    """
    return {
        "type": "object",
        "properties": {spec.name: property_schema(spec) for spec in graph.inputs},
        "required": [spec.name for spec in graph.inputs if spec.required],
        "additionalProperties": True,
    }


def apply_defaults(
    graph: WorkflowGraph, supplied: Mapping[str, Any]
) -> Dict[str, Any]:
    """The supplied payload with every unset declared input filled in.

    `None` counts as unset, which is the price of spelling "no default" as
    `None` in the declaration. A workflow that genuinely wants to pass JSON null
    can declare no default and send it; nothing else in the grammar treats null
    as a value worth distinguishing from absence.
    """
    values = dict(supplied)
    for spec in graph.inputs:
        if spec.default is not None and values.get(spec.name) is None:
            # Copied so a mutable default cannot be edited into the declaration
            # by whatever the run does with its payload afterwards.
            values[spec.name] = copy.deepcopy(spec.default)
    return values


def bind(graph: WorkflowGraph, supplied: Mapping[str, Any]) -> Dict[str, Any]:
    """Defaults in, types checked, every problem reported. Raises on any.

    The result is the payload the run actually executes against, so it is worth
    storing back onto the run: a run detail that shows the inputs *after*
    defaults is one that answers "what did this actually run with".
    """
    values = apply_defaults(graph, supplied)
    validator = Draft202012Validator(input_schema(graph))
    problems = [
        _problem(failure)
        for failure in sorted(validator.iter_errors(values), key=str)[:MAX_PROBLEMS]
    ]
    if problems:
        raise InputBindingError(problems)
    return values


def _problem(failure: Any) -> str:
    location = ".".join(str(part) for part in failure.absolute_path)
    if not location:
        # A `required` failure has no path, and its message already names the
        # input it is about.
        return str(failure.message)
    return f"input “{location}”: {failure.message}"
