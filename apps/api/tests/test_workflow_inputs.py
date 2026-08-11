"""Typed workflow inputs: declared at compile time, bound before the first node.

A compiled DAG used to be a fixed document — `{{ input.repo }}` meant "whatever
the caller happened to pass", which is not a parameter, it is a hope. These tests
are the two halves of turning that into one:

**The declaration is checked when a person is present.** A reference to an input
nobody declared is a field missing from the form. A default that does not satisfy
its own type is a form that opens invalid. A scheduled workflow with a required
input and no default cannot fire at all, and finding that out on Monday is
finding it out every Monday.

**The supplied values are checked at run start, not mid-DAG.** The assertion
that carries the feature is `probe.calls == []`: a run whose inputs are wrong
must fail having called *nothing*, because a graph that gets as far as node six
before noticing has already sent five nodes' worth of real email.

The compile-time half runs against a fake registry; the run-time half runs the
real executor against fake tools, like `test_workflow_executor.py`.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest
from conftest import Identity, create_identity
from test_workflow_executor import (
    Probe,
    authorize,
    graph,
    install,
    nodes_of,
    tool_node,
)

from app.database import SessionLocal
from app.main import app
from app.services.llm_tools import ToolContext, ToolResult, ToolSpec
from app.services.workflows import executor, inputs, parse_graph, validate_graph
from app.services.workflows.dag import WorkflowGraph

TEST_BASE_URL = "https://testserver"


# --------------------------------------------------------------------------
# Compile time
# --------------------------------------------------------------------------


def _noop(db: Any, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return ToolResult(content="")


REGISTRY = {
    "probe_read": ToolSpec(
        name="probe_read",
        description="The probe_read tool.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}, "limit": {"type": "integer"}},
        },
        executor=_noop,
        read_only=True,
    )
}


def _with_inputs(
    declarations: List[Dict[str, Any]],
    *,
    arguments: Optional[Dict[str, Any]] = None,
    trigger: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    document = graph(
        [tool_node("only", "probe_read", arguments or {"text": "static"})],
        trigger=trigger,
    )
    document["inputs"] = declarations
    return document


def _codes(document: Dict[str, Any]) -> List[str]:
    parsed, errors = parse_graph(document)
    if parsed is None:
        return [item.code for item in errors]
    return [item.code for item in validate_graph(parsed, REGISTRY).errors]


def _graph_of(document: Dict[str, Any]) -> WorkflowGraph:
    parsed, errors = parse_graph(document)
    assert parsed is not None, errors
    assert validate_graph(parsed, REGISTRY).ok
    return parsed


def test_a_declared_input_a_node_reads_compiles() -> None:
    assert (
        _codes(
            _with_inputs(
                [{"name": "repo", "type": "string", "label": "Repository"}],
                arguments={"text": "{{ input.repo }}"},
            )
        )
        == []
    )


def test_an_undeclared_input_reference_is_refused_and_suggests_the_near_miss() -> None:
    """The form and the graph must agree about what a run will be asked for."""
    document = _with_inputs(
        [{"name": "repo", "default": "acme/api"}],
        arguments={"text": "{{ input.repos }}"},
    )
    parsed, _ = parse_graph(document)
    assert parsed is not None
    errors = validate_graph(parsed, REGISTRY).errors
    assert [item.code for item in errors] == ["reference_unknown_input"]
    assert "did you mean repo?" in errors[0].message
    assert errors[0].node == "only"


def test_a_graph_declaring_nothing_keeps_the_free_form_payload() -> None:
    """Back-compatibility, and it is load-bearing rather than polite.

    Graphs compiled before inputs existed are stored in `workflows.graph_json`
    and re-validated against the live registry at *every* run start. Making an
    undeclared reference an error would fail those runs with `graph_stale` on a
    graph nobody edited.
    """
    document = graph([tool_node("only", "probe_read", {"text": "{{ input.repo }}"})])
    assert "inputs" not in document
    assert _codes(document) == []


def test_the_trigger_supplies_scheduled_for_without_a_declaration() -> None:
    """`schedule.dispatch_due` puts it on every run, so a graph may read it."""
    assert (
        _codes(
            _with_inputs(
                [{"name": "repo", "default": "acme/api"}],
                arguments={"text": "{{ input.scheduled_for }}"},
            )
        )
        == []
    )
    assert _codes(_with_inputs([{"name": "scheduled_for"}])) == ["input_name_reserved"]


@pytest.mark.parametrize(
    ("declaration", "code"),
    [
        ({"name": "Repo"}, "input_name_invalid"),
        ({"name": "repo", "type": "integer", "default": "seven"}, "input_default_invalid"),
        (
            {
                "name": "repo",
                "choices": ["prod", "staging"],
                "default": "prodd",
            },
            "input_default_invalid",
        ),
        ({"name": "repo", "type": "integer", "choices": ["prod"]}, "input_choice_invalid"),
        ({"name": "repo", "required": False}, "input_optional_without_default"),
    ],
)
def test_an_incoherent_declaration_is_refused(
    declaration: Dict[str, Any], code: str
) -> None:
    assert code in _codes(_with_inputs([declaration]))


def test_a_duplicate_input_is_refused() -> None:
    assert _codes(
        _with_inputs([{"name": "repo", "default": "a"}, {"name": "repo", "default": "b"}])
    ) == ["input_name_duplicate"]


def test_a_scheduled_workflow_cannot_require_a_value_nobody_will_type() -> None:
    """The sharpest of these: it does not fail once, it fails every Monday."""
    schedule = {"kind": "schedule", "cron": "0 9 * * MON", "timezone": "UTC"}
    assert _codes(_with_inputs([{"name": "repo"}], trigger=schedule)) == [
        "input_required_unattended"
    ]
    # A default is what makes the same declaration schedulable: the run has an
    # answer without a person in the room.
    assert _codes(
        _with_inputs([{"name": "repo", "default": "acme/api"}], trigger=schedule)
    ) == []
    # And a manual trigger may ask, because somebody is filling in the form.
    assert _codes(_with_inputs([{"name": "repo"}])) == []


def test_an_unknown_field_in_a_declaration_is_refused() -> None:
    """`extra="forbid"`, for the reason `dag.py` gives: an invented field is a lie."""
    assert _codes(_with_inputs([{"name": "repo", "widget": "textarea"}])) == [
        "schema_invalid"
    ]


def test_too_many_inputs_is_a_graph_that_misunderstood() -> None:
    assert _codes(
        _with_inputs(
            [{"name": f"field_{index}", "default": "x"} for index in range(21)]
        )
    ) == ["graph_too_large"]


# --------------------------------------------------------------------------
# Binding
# --------------------------------------------------------------------------


def test_binding_applies_defaults_and_leaves_undeclared_keys_alone() -> None:
    parsed = _graph_of(
        _with_inputs(
            [
                {"name": "repo", "default": "acme/api"},
                {"name": "limit", "type": "integer", "default": 5},
            ]
        )
    )
    bound = inputs.bind(parsed, {"limit": 9, "scheduled_for": "2026-08-11T09:00:00"})
    assert bound == {
        "repo": "acme/api",
        "limit": 9,
        "scheduled_for": "2026-08-11T09:00:00",
    }


def test_every_declared_input_has_a_value_after_binding() -> None:
    """The post-condition the compile-time supply check exists to guarantee.

    It is what stops `{{ input.x }}` raising `reference_unresolved` at node six
    for a declared `x`: after binding there is no declared name left unset.
    """
    parsed = _graph_of(
        _with_inputs(
            [
                {"name": "repo"},
                {"name": "note", "required": False, "default": ""},
                {"name": "limit", "type": "integer", "default": 5},
            ]
        )
    )
    bound = inputs.bind(parsed, {"repo": "acme/api"})
    assert set(parsed.input_names()) <= set(bound)


def test_a_mutable_default_is_copied_not_shared() -> None:
    parsed = _graph_of(
        _with_inputs([{"name": "tags", "type": "array", "default": ["a"]}])
    )
    bound = inputs.bind(parsed, {})
    bound["tags"].append("b")
    assert parsed.inputs[0].default == ["a"]


def test_binding_reports_every_problem_rather_than_the_first() -> None:
    """A form marks all its bad fields at once or it is filled in twice."""
    parsed = _graph_of(
        _with_inputs([{"name": "repo"}, {"name": "limit", "type": "integer"}])
    )
    with pytest.raises(inputs.InputBindingError) as caught:
        inputs.bind(parsed, {"limit": "seven"})
    assert len(caught.value.problems) == 2
    assert any("repo" in problem for problem in caught.value.problems)
    assert any("limit" in problem for problem in caught.value.problems)


def test_a_value_outside_its_choices_is_refused() -> None:
    parsed = _graph_of(
        _with_inputs(
            [{"name": "env", "choices": ["prod", "staging"], "default": "staging"}]
        )
    )
    assert inputs.bind(parsed, {"env": "prod"}) == {"env": "prod"}
    with pytest.raises(inputs.InputBindingError):
        inputs.bind(parsed, {"env": "dev"})


def test_the_generated_schema_is_what_a_form_needs() -> None:
    """One schema, generated once, checked by `bind` and rendered by a client."""
    parsed = _graph_of(
        _with_inputs(
            [
                {
                    "name": "repo",
                    "type": "string",
                    "label": "Repository",
                    "description": "owner/name",
                    "default": "acme/api",
                },
                {"name": "limit", "type": "integer", "required": False, "default": 5},
            ]
        )
    )
    schema = inputs.input_schema(parsed)
    assert schema["properties"]["repo"] == {
        "type": "string",
        "title": "Repository",
        "description": "owner/name",
        "default": "acme/api",
    }
    assert schema["required"] == ["repo"]
    # Open, because the scheduler's own `scheduled_for` arrives on every run.
    assert schema["additionalProperties"] is True


# --------------------------------------------------------------------------
# Run start
# --------------------------------------------------------------------------


@pytest.fixture
def identity() -> Identity:
    return create_identity(name="Inputs owner", workspace_name="Inputs workspace")


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _begin(
    db: Any,
    identity: Identity,
    document: Dict[str, Any],
    payload: Optional[Dict[str, Any]] = None,
) -> Any:
    from test_workflow_executor import begin

    return begin(db, identity, document, payload=payload)


def test_a_supplied_input_reaches_the_node_with_its_declared_type(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole-value `{{ input.limit }}` is the integer, not the string "9"."""
    probe = Probe(
        "probe_read",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}, "limit": {"type": "integer"}},
        },
    )
    install(monkeypatch, probe)
    workflow_run = _begin(
        db,
        identity,
        _with_inputs(
            [{"name": "repo"}, {"name": "limit", "type": "integer", "default": 5}],
            arguments={"text": "PRs for {{ input.repo }}", "limit": "{{ input.limit }}"},
        ),
        {"repo": "acme/widgets", "limit": 9},
    )
    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "succeeded", workflow_run.error
    assert probe.calls == [{"text": "PRs for acme/widgets", "limit": 9}]


def test_a_missing_required_input_fails_before_any_node_runs(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assertion the whole feature is for: nothing ran.

    A graph that noticed at node six would have done five nodes of real work
    first, and no error message afterwards takes an email back.
    """
    probe = Probe("probe_read")
    install(monkeypatch, probe)
    workflow_run = _begin(
        db,
        identity,
        _with_inputs([{"name": "repo"}], arguments={"text": "{{ input.repo }}"}),
        {},
    )
    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "failed"
    assert workflow_run.error.startswith("inputs_invalid:")
    assert "repo" in workflow_run.error
    assert probe.calls == []
    # And the run record still reads: the node that never ran says so.
    assert nodes_of(db, workflow_run)["only"].status == "skipped"


def test_a_wrongly_typed_input_is_rejected_not_coerced(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule `_resolved_arguments` applies to a node's arguments.

    Turning "seven" into an integer field would make the tool do something
    subtly wrong; refusing makes it do nothing and say why.
    """
    probe = Probe("probe_read")
    install(monkeypatch, probe)
    workflow_run = _begin(
        db,
        identity,
        _with_inputs(
            [{"name": "limit", "type": "integer", "default": 5}],
            arguments={"text": "{{ input.limit }}"},
        ),
        {"limit": "seven"},
    )
    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "failed"
    assert "inputs_invalid" in workflow_run.error
    assert probe.calls == []


def test_a_run_records_the_inputs_it_actually_ran_with(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defaults are part of the answer to "what did this run do"."""
    probe = Probe("probe_read")
    install(monkeypatch, probe)
    workflow_run = _begin(
        db,
        identity,
        _with_inputs(
            [{"name": "repo", "default": "acme/api"}],
            arguments={"text": "{{ input.repo }}"},
        ),
        {},
    )
    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "succeeded", workflow_run.error
    assert probe.calls == [{"text": "acme/api"}]
    assert json.loads(workflow_run.input_json) == {"repo": "acme/api"}


def test_a_run_of_a_graph_declaring_nothing_is_unchanged(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The free-form payload still resolves, and still is not checked."""
    probe = Probe("probe_read")
    install(monkeypatch, probe)
    workflow_run = _begin(
        db,
        identity,
        graph([tool_node("only", "probe_read", {"text": "{{ input.repo }}"})]),
        {"repo": "acme/widgets"},
    )
    executor.advance_run(db, workflow_run)
    assert probe.calls == [{"text": "acme/widgets"}]


# --------------------------------------------------------------------------
# The HTTP surface
# --------------------------------------------------------------------------


@pytest.fixture
def client(identity: Identity) -> Any:
    from fastapi.testclient import TestClient

    with TestClient(app, base_url=TEST_BASE_URL) as test_client:
        authorize(test_client, identity)
        yield test_client


def test_running_with_bad_inputs_is_a_422_and_creates_no_run(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The person is still holding the form, so tell them now.

    The executor checks again and is the authority — the scheduler never comes
    through here — but a 202 followed by a failed run is a worse way to say
    "you left a field blank".
    """
    install(monkeypatch, Probe("probe_read"))
    document = _with_inputs(
        [{"name": "repo", "label": "Repository"}],
        arguments={"text": "{{ input.repo }}"},
    )
    created = client.post("/api/workflows", json={"graph": document})
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    # The declaration travels on the graph, which is what a client renders.
    assert created.json()["graph"]["inputs"][0]["label"] == "Repository"

    refused = client.post(f"/api/workflows/{workflow_id}/run", json={"payload": {}})
    assert refused.status_code == 422, refused.text
    assert "repo" in " ".join(refused.json()["detail"]["inputs"])
    assert client.get(f"/api/workflows/{workflow_id}/runs").json() == []

    accepted = client.post(
        f"/api/workflows/{workflow_id}/run", json={"payload": {"repo": "acme/api"}}
    )
    assert accepted.status_code == 202, accepted.text
    client.delete(f"/api/workflows/{workflow_id}")
