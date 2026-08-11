"""`when` guard evaluation: the one place an unresolved reference is a no, not a raise.

`evaluate_guard` is total — it returns a bool for every guard against every state
of a run's values — because the walk asks it a question it must answer to decide
whether a node runs. These pin the two things that make it total: a reference to
a skipped or missing node is *absent* (false for a comparison, answerable by
present/absent), and an incomparable pair (a number against a string) is false
rather than a crash.
"""
from __future__ import annotations

from app.services.workflows.dag import GuardSpec
from app.services.workflows.guards import evaluate_guard


def check(op: str, left: str, right=None, *, outputs=None, payload=None) -> bool:
    guard = GuardSpec(left=left, op=op, right=right)
    return evaluate_guard(guard, outputs=outputs or {}, payload=payload or {})


def test_equality_reads_the_resolved_upstream_value() -> None:
    outputs = {"triage": {"severity": "high"}}
    assert check("eq", "{{ triage.output.severity }}", "high", outputs=outputs)
    assert not check("eq", "{{ triage.output.severity }}", "low", outputs=outputs)
    assert check("ne", "{{ triage.output.severity }}", "low", outputs=outputs)


def test_ordered_comparisons_on_numbers() -> None:
    outputs = {"count": {"total": 12}}
    assert check("gt", "{{ count.output.total }}", 10, outputs=outputs)
    assert check("gte", "{{ count.output.total }}", 12, outputs=outputs)
    assert not check("lt", "{{ count.output.total }}", 12, outputs=outputs)


def test_membership_and_literals() -> None:
    assert check("in", "{{ input.env }}", ["prod", "staging"], payload={"env": "prod"})
    assert not check("in", "{{ input.env }}", ["prod", "staging"], payload={"env": "dev"})


def test_a_reference_to_a_skipped_node_is_absent_not_an_error() -> None:
    # `outputs` has no `page` — the branch that would produce it was pruned.
    assert check("absent", "{{ page.output.id }}")
    assert not check("present", "{{ page.output.id }}")
    # Every comparison against an absent value is false: the node does not run.
    assert not check("eq", "{{ page.output.id }}", "x")
    assert not check("ne", "{{ page.output.id }}", "x")


def test_truthy_and_falsy_read_only_the_left_operand() -> None:
    assert check("truthy", "{{ flag.output }}", outputs={"flag": True})
    assert not check("truthy", "{{ flag.output }}", outputs={"flag": False})
    assert check("falsy", "{{ flag.output }}", outputs={"flag": 0})
    # Absent is falsy and not truthy.
    assert check("falsy", "{{ missing.output }}")
    assert not check("truthy", "{{ missing.output }}")


def test_an_incomparable_pair_is_false_rather_than_a_crash() -> None:
    # gt between a string and a number would raise TypeError if evaluated naively.
    assert not check("gt", "{{ label.output }}", 5, outputs={"label": "nope"})


def test_a_literal_left_operand_is_compared_as_written() -> None:
    # A guard whose left is a plain literal (the validator warns about this) still
    # evaluates rather than erroring.
    assert check("eq", "high", "high")
    assert not check("eq", "high", "low")
