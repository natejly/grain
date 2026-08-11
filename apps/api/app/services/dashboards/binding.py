"""Binding a dashboard template to real data. The half that has to be right.

A template is a dashboard definition with no dataset: it declares the columns it
requires — names and types — and its spec is written against those declared
names. Binding supplies the map from declared name to a column some real dataset
actually has, and produces an ordinary `Dashboard` pointed at that dataset.

The premise is the one ADR 0007 states for workflows: **a definition pointed at
the wrong data must fail here, loudly, and not on Monday morning.** A dashboard
is opened by someone who did not build it, usually in a hurry, and the failure
mode this module exists to prevent is a tile that renders empty or plausible
instead of refusing. The last moment a person can be shown the mistake is the
moment they bind it.

So this is deliberately the same discipline as `services/workflows/validate.py`,
down to the types: it reports `CompileError`s into a `CompileReport`, collects
every finding rather than stopping at the first, and the route hands the report
back as a 422 body of the identical shape. That is not incidental reuse. A
workflow given the wrong input and a dashboard bound to the wrong dataset are
the same mistake — a declared contract that the supplied thing does not satisfy
— and they should be legible to the same reader, and repairable by the same
model, without learning a second vocabulary.

Two checks run before the schema does, because their findings are better than
anything a schema can say: a binding that names a column the dataset does not
have, and a binding that names a parameter the template never declared. Both
carry a did-you-mean, and both suppress the schema's own duller report of the
same fact — the same early-return `validate.py` uses when a node names a tool
that does not exist and there is no point type-checking its arguments.
"""
from __future__ import annotations

import difflib
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set

from jsonschema import Draft202012Validator

from ...schemas import (
    DashboardColumnRequirement,
    DashboardSpec,
    DatasetColumn,
)

# The vocabulary is imported rather than redeclared. See the module docstring:
# the whole point is that the two failures read the same, and two dataclasses
# that merely resemble each other drift apart on the first bug fix.
from ..workflows.validate import CompileError, CompileReport

#: Which *actual* dataset column types satisfy each *required* type.
#:
#: Widening only, and only where the widening cannot change an answer. An
#: integer column satisfies a requirement for a number because every aggregate
#: this engine computes over it means the same thing. A date or datetime column
#: satisfies a requirement for a string because a template asking for a string
#: is asking for a label — a category axis, a group — and a timestamp is a
#: perfectly good label.
#:
#: Nothing widens the other way, and numbers deliberately do not satisfy
#: `string`. They could: everything casts to text in the end. But a template
#: that asks for a label and silently accepts an amount column produces a chart
#: with one bar per distinct price, which is the class of quietly-wrong output
#: this module exists to refuse.
SATISFIES: Mapping[str, FrozenSet[str]] = {
    "boolean": frozenset({"boolean"}),
    "integer": frozenset({"integer"}),
    "number": frozenset({"integer", "number"}),
    "string": frozenset({"string", "date", "datetime"}),
    "date": frozenset({"date"}),
    "datetime": frozenset({"datetime"}),
}

#: Aggregations that need a numeric column. Mirrors the run-time rule in
#: `services/analytics._validate_query`; declaring types is what lets a template
#: catch the same mistake before any data exists.
NUMERIC_OPERATIONS = frozenset({"sum", "avg"})

#: The column an unaggregated group-by query gets for free.
IMPLICIT_COUNT = "count"


class DashboardBindError(RuntimeError):
    """Raised when a template cannot be bound. Carries every finding."""

    def __init__(self, report: CompileReport) -> None:
        super().__init__(report.render() or "dashboard binding failed")
        self.report = report


# --------------------------------------------------------------------------
# Where a spec names a column
# --------------------------------------------------------------------------


def dataset_fields(spec: DashboardSpec) -> Dict[str, str]:
    """Every name the spec sends to the *dataset*, and where it came from.

    These are checked against the declared columns, because they become SQL
    identifiers against real data. Distinct from `result_fields` below, which
    names columns of the query's *answer* — a metric label like "revenue" exists
    only after the query runs and no dataset need have a column by that name.
    """
    fields: Dict[str, str] = {}
    for index, item in enumerate(spec.query.filters):
        fields.setdefault(item.field, f"query.filters[{index}].field")
    if spec.query.group_by:
        fields.setdefault(spec.query.group_by, "query.group_by")
    for index, metric in enumerate(spec.query.metrics):
        if metric.field:
            fields.setdefault(metric.field, f"query.metrics[{index}].field")
    return fields


def result_fields(spec: DashboardSpec) -> Dict[str, str]:
    """Every name the spec reads off the query's *answer*."""
    fields: Dict[str, str] = {}
    if spec.x_field:
        fields.setdefault(spec.x_field, "x_field")
    for index, name in enumerate(spec.y_fields):
        fields.setdefault(name, f"y_fields[{index}]")
    if spec.query.order_by:
        fields.setdefault(spec.query.order_by, "query.order_by")
    return fields


def result_columns(spec: DashboardSpec, declared: Sequence[str]) -> Set[str]:
    """The columns this spec's query will return, given its declared inputs.

    The same case split as `analytics._validate_query`: a query that groups or
    aggregates returns the group and the metric labels, and a query that does
    neither returns the dataset unchanged.
    """
    if spec.query.group_by or spec.query.metrics:
        columns = {spec.query.group_by} if spec.query.group_by else set()
        columns |= {metric.label for metric in spec.query.metrics}
        if spec.query.group_by and not spec.query.metrics:
            columns.add(IMPLICIT_COUNT)
        return {name for name in columns if name}
    return set(declared)


def rebind_spec(spec: DashboardSpec, bindings: Mapping[str, str]) -> DashboardSpec:
    """Rewrite every declared name in `spec` to the column it was bound to.

    Names the template did not declare pass through untouched, which is what
    makes metric labels safe: "revenue" as a label is the author's word for the
    answer, not a request for a column, and renaming it would break the
    visualization's own references to it.
    """
    if not bindings:
        return spec.model_copy(deep=True)

    def bound(name: Optional[str]) -> Optional[str]:
        if name is None:
            return None
        return bindings.get(name, name)

    resolved = spec.model_copy(deep=True)
    for item in resolved.query.filters:
        item.field = bindings.get(item.field, item.field)
    resolved.query.group_by = bound(resolved.query.group_by)
    for metric in resolved.query.metrics:
        metric.field = bound(metric.field)
    resolved.query.order_by = bound(resolved.query.order_by)
    resolved.x_field = bound(resolved.x_field)
    resolved.y_fields = [bindings.get(name, name) for name in resolved.y_fields]
    return resolved


def _did_you_mean(name: str, candidates: Iterable[str]) -> str:
    close = difflib.get_close_matches(name, list(candidates), n=3, cutoff=0.6)
    return f"; did you mean {', '.join(close)}?" if close else ""


# --------------------------------------------------------------------------
# Template validation — run when a template is written
# --------------------------------------------------------------------------


def template_report(
    required: Sequence[DashboardColumnRequirement], spec: DashboardSpec
) -> CompileReport:
    """Is this definition internally consistent, before any data exists?

    A template that passes here and is bound to a dataset that satisfies its
    contract cannot fail at run time for a reason about columns — which is the
    entire promise. Every check below closes one way that promise could be
    broken by the definition itself rather than by the data.
    """
    report = CompileReport()
    declared: List[str] = []
    types: Dict[str, str] = {}
    for requirement in required:
        if requirement.name in types:
            report.errors.append(
                CompileError(
                    "required_column_duplicate",
                    f"column “{requirement.name}” is declared twice",
                    requirement.name,
                )
            )
            continue
        types[requirement.name] = requirement.type
        declared.append(requirement.name)

    for name, where in dataset_fields(spec).items():
        if name not in types:
            report.errors.append(
                CompileError(
                    "spec_column_undeclared",
                    f"{where} reads column “{name}”, which the template does not "
                    "require; declare it in required_columns or the binding can "
                    "never guarantee it exists" + _did_you_mean(name, declared),
                    name,
                )
            )

    for index, metric in enumerate(spec.query.metrics):
        if metric.operation != "count" and not metric.field:
            report.errors.append(
                CompileError(
                    "spec_metric_field_missing",
                    f"query.metrics[{index}] applies {metric.operation} to no "
                    "column; only count may omit one",
                    metric.label,
                )
            )
            continue
        declared_type = types.get(metric.field or "")
        if (
            metric.operation in NUMERIC_OPERATIONS
            and declared_type is not None
            and declared_type not in SATISFIES["number"]
        ):
            report.errors.append(
                CompileError(
                    "spec_metric_not_numeric",
                    f"query.metrics[{index}] applies {metric.operation} to "
                    f"“{metric.field}”, declared {declared_type}; sum and avg need "
                    "a numeric column",
                    metric.field or "",
                )
            )

    available = result_columns(spec, declared)
    for name, where in result_fields(spec).items():
        if name not in available:
            report.errors.append(
                CompileError(
                    "spec_field_unknown",
                    f"{where} names “{name}”, which this query does not return; "
                    "it returns " + (", ".join(sorted(available)) or "nothing")
                    + _did_you_mean(name, available),
                    name,
                )
            )
    return report


# --------------------------------------------------------------------------
# Bind validation — run when a template meets a dataset
# --------------------------------------------------------------------------


def required_shape_schema(
    required: Sequence[DashboardColumnRequirement], *, skip: Set[str]
) -> Dict[str, Any]:
    """The declared contract, as a JSON Schema over {declared name: actual type}.

    Expressing it as a schema is not decoration. It is the same move
    `validate.py` makes for tool arguments — compile the declaration once, let
    one validator produce every finding — and it means "required" and "the type
    is one of these" are enforced by the same library that enforces them for
    workflow inputs rather than by a second hand-written loop that agrees with
    it only until someone edits one of them.

    `skip` holds columns already reported by name. Asking the schema about them
    too would report one mistake twice, in a duller way the second time.
    """
    properties: Dict[str, Any] = {}
    for requirement in required:
        if requirement.name in skip:
            continue
        properties[requirement.name] = {
            "enum": sorted(SATISFIES[requirement.type]),
            "title": requirement.type,
        }
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
    }


def bind_report(
    required: Sequence[DashboardColumnRequirement],
    *,
    bindings: Mapping[str, str],
    columns: Sequence[DatasetColumn],
) -> CompileReport:
    """Does this dataset satisfy this template under this binding?

    Returns every reason it does not. The caller shows the whole list to a
    person or hands it to a model to repair, and both are worse one at a time.
    """
    report = CompileReport()
    declared = {requirement.name: requirement for requirement in required}
    actual = {column.name: column for column in columns}

    for name in sorted(bindings):
        if name not in declared:
            report.errors.append(
                CompileError(
                    "binding_unknown_parameter",
                    f"the template does not require a column called “{name}”"
                    + _did_you_mean(name, declared),
                    name,
                )
            )

    instance: Dict[str, str] = {}
    reported: Set[str] = set()
    for name in declared:
        source = bindings.get(name, name)
        column = actual.get(source)
        if column is not None:
            instance[name] = column.type
            continue
        if name in bindings:
            # An explicit binding at a column that is not there. Naming both the
            # declared name and the column that was asked for is the difference
            # between a fixable message and a puzzle.
            report.errors.append(
                CompileError(
                    "binding_column_unknown",
                    f"“{name}” is bound to “{source}”, which this dataset does not "
                    "have" + _did_you_mean(source, actual),
                    name,
                )
            )
            reported.add(name)

    schema = required_shape_schema(required, skip=reported)
    # `required` yields one failure per missing property and names the whole
    # required list on each, so the missing set is unioned rather than read off
    # any single failure — reading it per failure reports every absence once per
    # absence, which is n² findings for one mistake.
    missing: Set[str] = set()
    mistyped: List[str] = []
    for failure in Draft202012Validator(schema).iter_errors(instance):
        if failure.validator == "required":
            missing |= set(failure.validator_value) - set(instance)
        elif failure.absolute_path:
            mistyped.append(str(failure.absolute_path[0]))

    for name in sorted(missing):
        requirement = declared[name]
        report.errors.append(
            CompileError(
                "template_column_missing",
                f"the template requires a {requirement.type} column “{name}” and "
                "this dataset has none; bind it to one of "
                + (", ".join(sorted(actual)) or "no columns at all")
                + _did_you_mean(name, actual),
                name,
            )
        )
    for name in sorted(set(mistyped)):
        requirement = declared[name]
        source = bindings.get(name, name)
        report.errors.append(
            CompileError(
                "template_column_type",
                f"the template requires “{name}” to be {requirement.type}, but "
                f"“{source}” is {actual[source].type}",
                name,
            )
        )
    return report


def effective_bindings(
    required: Sequence[DashboardColumnRequirement], bindings: Mapping[str, str]
) -> Dict[str, str]:
    """The map that was actually applied, identity entries included.

    Stored on the bound dashboard so the trace is complete: a spec grouping by
    "territory" is only traceable to a template that declared "region" if the
    pairs that were left implicit were written down too.
    """
    return {
        requirement.name: bindings.get(requirement.name, requirement.name)
        for requirement in required
    }
