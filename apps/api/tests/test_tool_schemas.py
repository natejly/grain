"""Two tool schemas the model was guessing at.

`search_sources` had no way to ask for breadth; `query_dataset` advertised
`{"type": "object"}` for a query the validator then rejected, which cost a
round trip per query — the model guessed a shape, pydantic refused it, and the
turn spent an iteration reading the errors back.

The schema is pinned against `schemas.DatasetQuery` itself rather than restated
here, because a second copy of the rules is exactly what drifts.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

from app.database import SessionLocal
from app.schemas import DatasetQuery
from app.services import llm_tools
from app.services.llm_tools import (
    DEFAULT_SOURCE_LIMIT,
    MAX_SOURCE_LIMIT,
    ToolContext,
    _clamped_limit,
    _search_sources,
    registry_families,
)


def _spec(name: str):
    """One spec, read off the LIVE registry rather than a copy of it.

    A real session because several families query for their descriptions; the
    two specs under test are in `core` and query nothing, but reading them out
    of the real assembly is what makes this a test of what the model is sent.
    """
    db = SessionLocal()
    try:
        for _family, tools in registry_families(
            db, ToolContext(workspace_id="ws", user_id="u", conversation_id="c")
        ):
            if name in tools:
                return tools[name]
    finally:
        db.close()
    raise AssertionError(f"{name} is not in the registry")


# --- search_sources limit ----------------------------------------------------


def test_the_limit_is_declared_with_its_bounds():
    properties = _spec("search_sources").parameters["properties"]
    assert properties["limit"]["type"] == "integer"
    assert properties["limit"]["minimum"] == 1
    assert properties["limit"]["maximum"] == MAX_SOURCE_LIMIT
    assert properties["limit"]["default"] == DEFAULT_SOURCE_LIMIT
    # Optional: a call that names no limit must stay valid.
    assert _spec("search_sources").parameters["required"] == ["query"]


def test_a_missing_limit_reproduces_the_pre_parameter_call(monkeypatch):
    """Byte-identical to before the parameter existed — including the token
    budget, which travels with the limit."""
    seen: Dict[str, Any] = {}

    def spy(db, **kwargs: Any) -> List[Any]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(llm_tools, "search_evidence", spy)
    _search_sources(
        None,  # type: ignore[arg-type]
        ToolContext(workspace_id="ws", user_id="u", conversation_id="c"),
        {"query": "launch"},
    )
    assert seen["limit"] == 5
    assert seen["token_budget"] == 1200


def test_a_wider_search_widens_the_token_budget_with_it(monkeypatch):
    """Left at its default, ten passages would share the five-passage budget
    and each arrive as a sentence — more citations, less evidence."""
    seen: Dict[str, Any] = {}

    def spy(db, **kwargs: Any) -> List[Any]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(llm_tools, "search_evidence", spy)
    _search_sources(
        None,  # type: ignore[arg-type]
        ToolContext(workspace_id="ws", user_id="u", conversation_id="c"),
        {"query": "launch", "limit": 10},
    )
    assert seen["limit"] == 10
    assert seen["token_budget"] == 2400


def test_a_model_supplied_limit_is_clamped_not_refused():
    """A bad count is not worth failing a search over: an error result would
    cost the turn a whole round to recover from something the tool can decide."""
    assert _clamped_limit(0, default=5, ceiling=20) == 1
    assert _clamped_limit(999, default=5, ceiling=20) == 20
    assert _clamped_limit("7", default=5, ceiling=20) == 7
    assert _clamped_limit("many", default=5, ceiling=20) == 5
    assert _clamped_limit(None, default=5, ceiling=20) == 5
    # True is an int in Python and a nonsense limit anywhere else.
    assert _clamped_limit(True, default=5, ceiling=20) == 5


# --- query_dataset schema ----------------------------------------------------


def _query_schema() -> Dict[str, Any]:
    return _spec("query_dataset").parameters["properties"]["query"]


def test_the_query_is_no_longer_an_opaque_object():
    schema = _query_schema()
    assert set(schema["properties"]) == {
        "filters",
        "group_by",
        "metrics",
        "order_by",
        "order_direction",
        "limit",
    }


def test_the_advertised_fields_are_the_validators_fields():
    """The model is told the same field names the validator will apply."""
    assert set(_query_schema()["properties"]) == set(DatasetQuery.model_fields)


def test_the_enums_match_the_schemas_own_literals():
    schema = _query_schema()["properties"]
    operator = schema["filters"]["items"]["properties"]["operator"]["enum"]
    operation = schema["metrics"]["items"]["properties"]["operation"]["enum"]
    assert operator == ["eq", "ne", "gt", "gte", "lt", "lte", "contains"]
    assert operation == ["count", "sum", "avg", "min", "max"]
    assert schema["order_direction"]["enum"] == ["asc", "desc"]


def test_the_bounds_match_the_schemas_own_constraints():
    schema = _query_schema()["properties"]
    assert schema["limit"]["minimum"] == 1
    assert schema["limit"]["maximum"] == 500
    assert schema["limit"]["default"] == 100
    assert schema["filters"]["maxItems"] == 20
    assert schema["metrics"]["maxItems"] == 10


def test_the_worked_example_is_a_query_the_validator_accepts():
    """The one assertion that would catch a stale example: parse it back out of
    the description and run it through `DatasetQuery`."""
    description = _spec("query_dataset").description
    start = description.index('{"dataset_id"')
    end = description.index("}}", start) + 2
    payload = json.loads(description[start:end])
    query = DatasetQuery.model_validate(payload["query"])
    assert query.group_by == "region"
    assert query.order_direction == "desc"
    assert [metric.label for metric in query.metrics] == ["total"]


def test_metrics_require_only_what_the_validator_requires():
    """`field` is optional because `count` needs none; `label` is not, because
    `order_by` refers to it."""
    item = _query_schema()["properties"]["metrics"]["items"]
    assert item["required"] == ["operation", "label"]
