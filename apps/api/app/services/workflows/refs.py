"""Run-time resolution of `{{ node.output }}` — the other half of the closed syntax.

`dag.py` defines the grammar and `validate.py` proves at compile time that every
reference names an upstream node. This module is what happens when the run
actually reaches that node and the value has to become a real one.

Three rules carry the whole thing:

**A whole-value reference keeps its type.** `{"limit": "{{ count.output }}"}` is
the integer the upstream node produced, not the string `"5"`. That is the reason
`validate.py` refuses to type-check a whole reference at compile time and the
reason the executor re-checks the *resolved* arguments against the tool's schema
before calling it: the type is only knowable here, so here is where it is judged.

**An embedded reference is a string.** `"PRs: {{ prs.output }}"` interpolates,
because a string with something spliced into it is a string whatever the
upstream node returned. That is also what compile-time checking assumed.

**A reference that cannot be resolved raises.** Not "", not None. A missing key
that silently became an empty string is how an automation emails somebody a
blank report at 3am and nobody finds out for a week; a node that fails loudly
lands in `workflow_node_runs.error` where the run detail shows it.
"""
from __future__ import annotations

import json
from typing import Any, List, Mapping, Sequence

from .dag import INPUT_NAMESPACE, REFERENCE_RE, WHOLE_REFERENCE_RE

#: The only attribute a node exposes. `{{ gather.output }}` reads it; anything
#: deeper (`{{ gather.output.items.0 }}`) walks into the value it holds.
OUTPUT_ATTRIBUTE = "output"


class ReferenceResolutionError(RuntimeError):
    """A reference could not be resolved against this run's actual values."""


def resolve(
    value: Any, *, outputs: Mapping[str, Any], payload: Mapping[str, Any]
) -> Any:
    """Substitute every reference in `value` from finished nodes and the trigger.

    Walks dicts and lists because arguments are arbitrary JSON and a reference
    can be buried at any depth — the same walk `dag.references` performs, which
    is what makes the compile-time check and this substitution agree about where
    references can appear.
    """
    if isinstance(value, str):
        return _resolve_string(value, outputs=outputs, payload=payload)
    if isinstance(value, dict):
        return {
            key: resolve(item, outputs=outputs, payload=payload)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [resolve(item, outputs=outputs, payload=payload) for item in value]
    return value


def _resolve_string(
    text: str, *, outputs: Mapping[str, Any], payload: Mapping[str, Any]
) -> Any:
    whole = WHOLE_REFERENCE_RE.match(text)
    if whole:
        match = REFERENCE_RE.search(text)
        # Unreachable: WHOLE_REFERENCE_RE matched, so REFERENCE_RE must too.
        assert match is not None
        return _lookup(match.group(1), _path(match.group(2)), outputs, payload)

    def substitute(match: Any) -> str:
        found = _lookup(match.group(1), _path(match.group(2)), outputs, payload)
        return found if isinstance(found, str) else json.dumps(found, default=str)

    return REFERENCE_RE.sub(substitute, text)


def _path(suffix: str) -> List[str]:
    return [part for part in suffix.split(".") if part]


def _lookup(
    namespace: str,
    path: Sequence[str],
    outputs: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> Any:
    """`input.field…` reads the trigger payload; anything else reads a node."""
    if namespace == INPUT_NAMESPACE:
        return _descend(payload, path, f"{{{{ {INPUT_NAMESPACE} }}}}")
    if namespace not in outputs:
        # Compile time proved the node exists and is upstream, so reaching here
        # means it did not produce a value — it was skipped after an earlier
        # failure, or the stored graph and this run disagree.
        raise ReferenceResolutionError(
            f"“{namespace}” has no output in this run; it did not complete"
        )
    if not path or path[0] != OUTPUT_ATTRIBUTE:
        attribute = ".".join(path) or "(nothing)"
        raise ReferenceResolutionError(
            f"“{namespace}.{attribute}” is not readable; a node exposes "
            f"“{namespace}.{OUTPUT_ATTRIBUTE}”"
        )
    return _descend(outputs[namespace], path[1:], f"{{{{ {namespace}.output }}}}")


def _descend(cursor: Any, path: Sequence[str], label: str) -> Any:
    """Walk the remaining dotted segments into an already-resolved value.

    A JSON string is parsed once on the way in, so a tool that returns a JSON
    document is addressable field by field without the graph author having to
    know whether the tool serialised it. A string that is not JSON simply has no
    fields, and saying so is more useful than returning it whole and letting a
    downstream schema check report the wrong problem.
    """
    for index, part in enumerate(path):
        if isinstance(cursor, str):
            try:
                cursor = json.loads(cursor)
            except (ValueError, TypeError):
                raise ReferenceResolutionError(
                    f"{label} is text, so “{'.'.join(path[: index + 1])}” "
                    "cannot be read from it"
                ) from None
        if isinstance(cursor, Mapping) and part in cursor:
            cursor = cursor[part]
            continue
        if isinstance(cursor, list) and part.isdigit() and int(part) < len(cursor):
            cursor = cursor[int(part)]
            continue
        raise ReferenceResolutionError(
            f"{label} has no “{'.'.join(path[: index + 1])}”"
        )
    return cursor
