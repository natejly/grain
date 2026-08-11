"""Evaluate a node's `when` guard against a run's resolved values.

`dag.GuardSpec` is the shape a guard has and `validate._check_guard` proves at
compile time that its operands name real, upstream things. This module is what
happens when the walk reaches a guarded node and the condition has to become a
yes or a no.

One rule carries the behaviour: **a reference that cannot be resolved is
`absent`, not an error.** Everywhere else in this subsystem an unresolved
reference raises — a node that quietly read an empty value is how an automation
does the wrong thing at 3am. A guard is the one place that rule inverts, because
"run this step only if the earlier step produced a high severity" *has* to have
a defined answer when the earlier step was itself skipped: the answer is no. So
`present`/`absent` can ask the question directly, and every comparison against an
absent value is false — the guard does not fire, the node does not run.
"""
from __future__ import annotations

from typing import Any, Mapping

from .dag import GuardSpec
from .refs import ReferenceResolutionError, resolve

#: A sentinel distinct from a legitimate ``None`` output: a reference that did
#: not resolve at all, versus one that resolved to JSON null.
_ABSENT = object()


def _operand(value: Any, *, outputs: Mapping[str, Any], payload: Mapping[str, Any]) -> Any:
    """A guard operand resolved to a value, or ``_ABSENT`` if it cannot be."""
    try:
        return resolve(value, outputs=outputs, payload=payload)
    except ReferenceResolutionError:
        return _ABSENT


def evaluate_guard(
    guard: GuardSpec, *, outputs: Mapping[str, Any], payload: Mapping[str, Any]
) -> bool:
    """Does this guard permit its node to run?

    Total: it returns a bool for every guard and every state of the run's values,
    never raising, because the walk needs a decision here and a guard that threw
    would strand the run on a question it asked itself.
    """
    left = _operand(guard.left, outputs=outputs, payload=payload)
    op = guard.op

    if op == "present":
        return left is not _ABSENT and left is not None
    if op == "absent":
        return left is _ABSENT or left is None
    if op == "truthy":
        return left is not _ABSENT and bool(left)
    if op == "falsy":
        return left is _ABSENT or not bool(left)

    # Every remaining operator compares two operands. A side that never arrived
    # makes the comparison false rather than an error: the node simply does not
    # run, which is the safe reading of "we could not tell".
    if left is _ABSENT:
        return False
    right = _operand(guard.right, outputs=outputs, payload=payload)
    if right is _ABSENT:
        return False

    if op == "eq":
        return bool(left == right)
    if op == "ne":
        return bool(left != right)
    if op == "in":
        try:
            return left in right
        except TypeError:
            return False
    try:
        if op == "gt":
            return bool(left > right)
        if op == "lt":
            return bool(left < right)
        if op == "gte":
            return bool(left >= right)
        if op == "lte":
            return bool(left <= right)
    except TypeError:
        # Ordering a number against a string is not a crash; it is a guard that
        # cannot be true, the same as any other unanswerable comparison.
        return False
    return False
