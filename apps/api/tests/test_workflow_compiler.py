"""The NL → DAG compiler, and mostly its validator.

The premise under test is the one in ADR 0007: **a model that hallucinates must
fail at compile time.** Nobody is watching a scheduled workflow run, so every
mistake that can be caught before it is stored has to be caught before it is
stored. These tests are therefore written adversarially — most of them feed the
compiler output a language model plausibly produces on a bad day, and assert on
the specific error code rather than on "it raised something".
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pytest

from app.services.llm_tools import ToolContext, ToolResult, ToolSpec, build_registry
from app.services.workflows import (
    CompileError,
    WorkflowCompileError,
    compile_document,
    compile_workflow,
    cron_error,
    parse_graph,
    render_tool_catalogue,
    summarize,
    topological_order,
    validate_graph,
)


def _noop(db: Any, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    return ToolResult(content="")


def _spec(
    name: str,
    parameters: Dict[str, Any],
    *,
    read_only: bool = True,
    description: str = "",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description or f"The {name} tool.",
        parameters=parameters,
        executor=_noop,
        read_only=read_only,
    )


REGISTRY = {
    spec.name: spec
    for spec in (
        _spec(
            "list_pull_requests",
            {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "state": {"type": "string", "enum": ["open", "closed"]},
                    "limit": {"type": "integer"},
                },
                "required": ["repo"],
                "additionalProperties": False,
            },
        ),
        _spec("list_datasets", {"type": "object", "properties": {}}),
        _spec(
            "post_message",
            {
                "type": "object",
                "properties": {"channel": {"type": "string"}, "text": {"type": "string"}},
                "required": ["channel", "text"],
            },
            read_only=False,
        ),
        _spec("broken_schema_tool", {"type": "object", "properties": {"x": {"type": 7}}}),
    )
}


def _graph(**overrides: Any) -> Dict[str, Any]:
    """The graph the ask in the brief compiles to. Valid; tests mutate copies."""
    document: Dict[str, Any] = {
        "name": "Monday PR digest",
        "description": "Summarise open PRs and post them.",
        "trigger": {"kind": "schedule", "cron": "0 9 * * MON", "timezone": "UTC"},
        "nodes": [
            {
                "id": "pull_prs",
                "kind": "tool",
                "tool": "list_pull_requests",
                "arguments": {"repo": "acme/api", "state": "open"},
            },
            {
                "id": "summarise",
                "kind": "agent",
                "prompt": "Summarise these pull requests: {{ pull_prs.output }}",
            },
            {
                "id": "post",
                "kind": "tool",
                "tool": "post_message",
                "arguments": {"channel": "#eng", "text": "{{ summarise.output }}"},
            },
        ],
        "edges": [
            {"from": "pull_prs", "to": "summarise"},
            {"from": "summarise", "to": "post"},
        ],
    }
    document.update(overrides)
    return document


def _codes(document: Dict[str, Any]) -> List[str]:
    """Every error code a document produces, structural or semantic."""
    graph, errors = parse_graph(document)
    if graph is None:
        return [item.code for item in errors]
    return [item.code for item in validate_graph(graph, REGISTRY).errors]


def _warnings(document: Dict[str, Any]) -> List[CompileError]:
    graph, errors = parse_graph(document)
    assert graph is not None, errors
    return validate_graph(graph, REGISTRY).warnings


# ---------------------------------------------------------------------------
# The happy path, and the shape of a compiled graph
# ---------------------------------------------------------------------------


def test_the_brief_s_example_compiles():
    compiled = compile_document(_graph(), REGISTRY)
    assert compiled.graph.name == "Monday PR digest"
    assert [node.id for node in compiled.graph.nodes] == ["pull_prs", "summarise", "post"]
    assert topological_order(compiled.graph) == ["pull_prs", "summarise", "post"]
    assert compiled.graph.trigger.cron == "0 9 * * MON"


def test_a_compiled_graph_declares_that_it_will_park():
    """The one fact a scheduler needs before running something unattended."""
    compiled = compile_document(_graph(), REGISTRY)
    assert compiled.requires_approval is True
    assert [item.code for item in compiled.warnings] == ["tool_write_capable"]
    assert summarize(compiled)["requires_approval"] is True

    read_only_only = _graph(
        nodes=[{"id": "a", "kind": "tool", "tool": "list_datasets"}], edges=[]
    )
    assert compile_document(read_only_only, REGISTRY).requires_approval is False


def test_a_graph_round_trips_through_its_stored_document():
    compiled = compile_document(_graph(), REGISTRY)
    stored = json.loads(json.dumps(compiled.graph.to_document()))
    assert stored["edges"][0] == {"from": "pull_prs", "to": "summarise"}
    assert compile_document(stored, REGISTRY).graph == compiled.graph


# ---------------------------------------------------------------------------
# The headline requirement: a hallucinated tool dies here
# ---------------------------------------------------------------------------


def test_a_hallucinated_tool_is_a_compile_error():
    document = _graph()
    document["nodes"][0]["tool"] = "github_list_open_pull_requests"
    with pytest.raises(WorkflowCompileError) as caught:
        compile_document(document, REGISTRY)
    errors = caught.value.report.errors
    assert [item.code for item in errors] == ["tool_unknown"]
    assert errors[0].node == "pull_prs"


def test_a_near_miss_tool_name_gets_told_what_it_meant():
    """A repair prompt the model can act on beats a wall it has to guess around."""
    document = _graph(
        nodes=[{"id": "a", "kind": "tool", "tool": "list_dataset"}], edges=[]
    )
    graph, _ = parse_graph(document)
    assert graph is not None
    message = validate_graph(graph, REGISTRY).errors[0].message
    assert "list_datasets" in message


def test_tool_names_are_matched_exactly_not_loosely():
    """Case-folding a tool name would let `Post_Message` reach `post_message`."""
    document = _graph(
        nodes=[
            {
                "id": "a",
                "kind": "tool",
                "tool": "Post_Message",
                "arguments": {"channel": "#x", "text": "hi"},
            }
        ],
        edges=[],
    )
    assert _codes(document) == ["tool_unknown"]


def test_a_tool_absent_from_this_workspace_is_unknown_even_if_it_exists_elsewhere():
    """`build_registry` is per-workspace: MCP servers and database connections
    make one tenant's tool genuinely nonexistent for another. The validator is
    handed the same mapping the executor will use, so there is no static list to
    drift from."""
    narrowed = {"list_datasets": REGISTRY["list_datasets"]}
    document = _graph(
        nodes=[
            {
                "id": "a",
                "kind": "tool",
                "tool": "post_message",
                "arguments": {"channel": "#x", "text": "hi"},
            }
        ],
        edges=[],
    )
    graph, _ = parse_graph(document)
    assert graph is not None
    assert [item.code for item in validate_graph(graph, narrowed).errors] == [
        "tool_unknown"
    ]


# ---------------------------------------------------------------------------
# Graph structure
# ---------------------------------------------------------------------------


def test_a_cycle_is_refused_and_names_the_nodes_in_it():
    document = _graph()
    document["edges"].append({"from": "post", "to": "pull_prs"})
    graph, _ = parse_graph(document)
    assert graph is not None
    errors = validate_graph(graph, REGISTRY).errors
    cycle = [item for item in errors if item.code == "graph_cycle"]
    assert cycle and "pull_prs" in cycle[0].message and "post" in cycle[0].message


def test_a_self_edge_is_refused():
    document = _graph()
    document["edges"].append({"from": "post", "to": "post"})
    assert "edge_self_loop" in _codes(document)


def test_an_edge_to_an_undeclared_node_is_refused():
    document = _graph()
    document["edges"].append({"from": "post", "to": "notify_oncall"})
    codes = _codes(document)
    assert "edge_unknown_node" in codes


def test_a_duplicate_edge_is_refused():
    document = _graph()
    document["edges"].append({"from": "pull_prs", "to": "summarise"})
    assert "edge_duplicate" in _codes(document)


def test_duplicate_and_malformed_node_ids_are_refused():
    assert "node_id_duplicate" in _codes(
        _graph(
            nodes=[
                {"id": "a", "kind": "tool", "tool": "list_datasets"},
                {"id": "a", "kind": "tool", "tool": "list_datasets"},
            ],
            edges=[],
        )
    )
    assert "node_id_invalid" in _codes(
        _graph(nodes=[{"id": "Pull-PRs", "kind": "tool", "tool": "list_datasets"}], edges=[])
    )


def test_a_node_may_not_claim_the_input_namespace():
    """`{{ input.x }}` is the trigger payload; a node called `input` would shadow it."""
    assert "node_id_reserved" in _codes(
        _graph(nodes=[{"id": "input", "kind": "tool", "tool": "list_datasets"}], edges=[])
    )


def test_an_empty_graph_is_refused():
    assert _codes(_graph(nodes=[], edges=[], trigger={"kind": "manual"})) == ["graph_empty"]


def test_a_runaway_graph_is_refused():
    nodes = [
        {"id": f"n{index}", "kind": "tool", "tool": "list_datasets"} for index in range(41)
    ]
    assert "graph_too_large" in _codes(
        _graph(nodes=nodes, edges=[], trigger={"kind": "manual"})
    )


def test_validation_reports_every_problem_at_once():
    """Errors accumulate. One at a time makes both the repair prompt and the
    human review worse, and a model given one error tends to fix it by
    introducing another."""
    document = _graph(
        nodes=[
            {"id": "a", "kind": "tool", "tool": "nope"},
            {"id": "a", "kind": "tool", "tool": "list_datasets"},
            {"id": "c", "kind": "agent", "prompt": ""},
        ],
        edges=[{"from": "a", "to": "zzz"}],
        trigger={"kind": "schedule", "cron": "@weekly"},
    )
    codes = set(_codes(document))
    assert {
        "tool_unknown",
        "node_id_duplicate",
        "agent_prompt_missing",
        "edge_unknown_node",
        "trigger_cron_invalid",
    } <= codes


# ---------------------------------------------------------------------------
# References between nodes
# ---------------------------------------------------------------------------


def test_a_reference_to_a_node_that_is_not_upstream_is_refused():
    """The check that earns its keep. Everything else about this graph is legal;
    at run time `post` would read an output that does not exist yet."""
    document = _graph(
        edges=[{"from": "pull_prs", "to": "summarise"}]  # `post` left dangling
    )
    codes = _codes(document)
    assert "reference_not_upstream" in codes


def test_a_reference_to_a_nonexistent_node_is_refused():
    document = _graph()
    document["nodes"][2]["arguments"]["text"] = "{{ triage.output }}"
    assert "reference_unknown_node" in _codes(document)


def test_the_trigger_payload_is_a_legal_reference():
    document = _graph(
        trigger={"kind": "manual"},
        nodes=[
            {
                "id": "a",
                "kind": "tool",
                "tool": "list_pull_requests",
                "arguments": {"repo": "{{ input.repo }}"},
            }
        ],
        edges=[],
    )
    assert _codes(document) == []


def test_references_are_found_at_any_depth_in_arguments():
    document = _graph()
    document["nodes"][2]["arguments"]["text"] = "fixed"
    document["nodes"][2]["arguments"]["blocks"] = [
        {"body": {"parts": ["{{ ghost.output }}"]}}
    ]
    assert "reference_unknown_node" in _codes(document)


def test_a_reference_reaching_through_an_intermediate_node_is_upstream():
    """Ancestry, not adjacency: `post` may read `pull_prs` through `summarise`."""
    document = _graph()
    document["nodes"][2]["arguments"]["text"] = (
        "{{ pull_prs.output }} / {{ summarise.output }}"
    )
    assert _codes(document) == []


def test_a_dotted_path_into_an_output_is_accepted():
    document = _graph()
    document["nodes"][2]["arguments"]["text"] = "{{ summarise.output.title }}"
    assert _codes(document) == []


# ---------------------------------------------------------------------------
# Arguments against the tool's own JSON Schema
# ---------------------------------------------------------------------------


def test_missing_required_arguments_are_refused():
    document = _graph()
    del document["nodes"][0]["arguments"]["repo"]
    codes = _codes(document)
    assert codes == ["arguments_invalid"]


def test_wrongly_typed_arguments_are_refused():
    document = _graph()
    document["nodes"][0]["arguments"]["limit"] = "twenty"
    assert "arguments_invalid" in _codes(document)


def test_an_argument_outside_the_schema_s_enum_is_refused():
    document = _graph()
    document["nodes"][0]["arguments"]["state"] = "merged"
    assert "arguments_invalid" in _codes(document)


def test_an_invented_argument_is_refused_when_the_schema_forbids_extras():
    document = _graph()
    document["nodes"][0]["arguments"]["assignee"] = "nate"
    assert "arguments_invalid" in _codes(document)


def test_a_whole_value_reference_defers_the_type_check():
    """`{{ x.output }}` in an integer field cannot be typed now and must not be
    guessed at — the upstream node decides. Deferring is the honest answer."""
    document = _graph()
    document["nodes"][0]["arguments"]["limit"] = "{{ input.limit }}"
    assert _codes(document) == []


def test_a_reference_embedded_in_a_larger_string_is_still_typed_as_a_string():
    """Interpolation yields a string whatever the upstream node returned, so an
    integer field fed by `"top {{ x.output }}"` is a real error, not a deferral."""
    document = _graph()
    document["nodes"][0]["arguments"]["limit"] = "top {{ input.limit }}"
    assert _codes(document) == ["arguments_invalid"]


def test_a_deferred_value_still_satisfies_required():
    document = _graph()
    document["nodes"][0]["arguments"]["repo"] = "{{ input.repo }}"
    assert _codes(document) == []


def test_a_tool_with_a_broken_schema_warns_rather_than_blocking_the_author():
    """An MCP server can ship an invalid schema and the workflow author cannot
    fix it. Refusing to compile would punish the wrong person; going quiet would
    hide that the arguments went unchecked."""
    document = _graph(
        trigger={"kind": "manual"},
        nodes=[
            {"id": "a", "kind": "tool", "tool": "broken_schema_tool", "arguments": {"x": 1}}
        ],
        edges=[],
    )
    assert _codes(document) == []
    assert [item.code for item in _warnings(document)] == ["tool_schema_invalid"]


# ---------------------------------------------------------------------------
# Node kinds
# ---------------------------------------------------------------------------


def test_an_agent_node_may_not_also_name_a_tool_or_arguments():
    codes = _codes(
        _graph(
            trigger={"kind": "manual"},
            nodes=[
                {
                    "id": "a",
                    "kind": "agent",
                    "prompt": "do it",
                    "tool": "list_datasets",
                    "arguments": {"x": 1},
                }
            ],
            edges=[],
        )
    )
    assert {"agent_node_names_tool", "agent_node_has_arguments"} <= set(codes)


def test_a_tool_node_may_not_carry_a_prompt():
    assert "tool_node_has_prompt" in _codes(
        _graph(
            trigger={"kind": "manual"},
            nodes=[
                {
                    "id": "a",
                    "kind": "tool",
                    "tool": "list_datasets",
                    "prompt": "and also think about it",
                }
            ],
            edges=[],
        )
    )


def test_a_tool_node_without_a_tool_is_refused():
    assert "tool_missing" in _codes(
        _graph(trigger={"kind": "manual"}, nodes=[{"id": "a", "kind": "tool"}], edges=[])
    )


def test_an_unknown_node_kind_is_refused_by_the_schema():
    assert _codes(
        _graph(
            trigger={"kind": "manual"},
            nodes=[{"id": "a", "kind": "http_request", "tool": "curl"}],
            edges=[],
        )
    ) == ["schema_invalid"]


# ---------------------------------------------------------------------------
# Manual nodes: the human-in-the-loop pause
# ---------------------------------------------------------------------------


def _manual(**node: Any) -> Dict[str, Any]:
    """A one-node manual graph. Manual triggers avoid the cron requirement."""
    base = {"id": "review", "kind": "manual", "prompt": "Ship it?"}
    base.update(node)
    return _graph(trigger={"kind": "manual"}, nodes=[base], edges=[])


def test_a_manual_node_compiles_and_forces_approval():
    """A pause is unattended risk by definition — a person must be there — so a
    graph containing one can never be scheduled to run alone."""
    document = _manual(fields=[{"name": "version", "type": "string"}])
    assert _codes(document) == []
    compiled = compile_document(document, REGISTRY)
    assert compiled.requires_approval is True
    assert "manual_requires_person" in [item.code for item in compiled.warnings]


def test_a_manual_node_needs_a_question_to_ask():
    assert "manual_missing_prompt" in _codes(_manual(prompt=""))


def test_a_manual_node_may_not_name_a_tool_or_carry_arguments():
    """It resolves no tool and runs no call — the pause is the whole node."""
    codes = _codes(_manual(tool="list_datasets", arguments={"x": 1}))
    assert {"manual_unexpected_tool", "manual_unexpected_arguments"} <= set(codes)


def test_a_manual_field_that_is_not_a_reference_slug_is_refused():
    """A field becomes `{{ review.output.<name> }}`, so its name has to be a slug
    the reference grammar can address."""
    assert "manual_field_invalid" in _codes(
        _manual(fields=[{"name": "Bad Name", "type": "string"}])
    )


def test_a_manual_node_with_two_fields_of_one_name_is_refused():
    assert "manual_duplicate_field" in _codes(
        _manual(
            fields=[
                {"name": "version", "type": "string"},
                {"name": "version", "type": "string"},
            ]
        )
    )


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    ["0 9 * * 1", "0 9 * * MON", "*/15 * * * *", "0 0 1 JAN *", "5,20 8-18 * * 1-5"],
)
def test_valid_cron_expressions_are_accepted(expression: str):
    assert cron_error(expression) is None


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "@weekly",
        "0 9 * *",
        "0 9 * * * *",
        "60 9 * * 1",
        "0 25 * * 1",
        "0 9 * FOO 1",
        "*/0 9 * * 1",
        "0 9 * * MONDAY",
    ],
)
def test_invalid_cron_expressions_are_rejected(expression: str):
    assert cron_error(expression) is not None


def test_a_schedule_without_a_workable_cron_is_refused():
    assert "trigger_cron_invalid" in _codes(_graph(trigger={"kind": "schedule", "cron": "@daily"}))


def test_a_manual_trigger_carrying_a_cron_is_refused():
    """Otherwise the author reads "every Monday" in the source prompt, sees a
    cron in the graph, and reasonably concludes it is scheduled."""
    document = _graph(trigger={"kind": "manual", "cron": "0 9 * * 1"})
    assert "trigger_cron_unexpected" in _codes(document)


# ---------------------------------------------------------------------------
# Hostile and malformed model output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "I'd be happy to help! Here is your workflow:",
        "",
        "null",
        "[]",
        '"a workflow"',
        "{",
        '{"name": "x"}',
        '{"name": "x", "nodes": "pull the PRs then post them"}',
        '{"name": "x", "nodes": [{"id": "a"}]}',
        '{"nodes": [{"id": "a", "kind": "tool", "tool": "list_datasets"}]}',
    ],
)
def test_output_that_is_not_a_workflow_fails_closed(raw: str):
    with pytest.raises(WorkflowCompileError) as caught:
        _compile(raw)
    assert caught.value.report.errors


def test_an_invented_top_level_field_is_refused():
    """A model that adds `"retry": 3` has invented a feature. Dropping it
    silently would store a graph that does not retry and looks like it does."""
    document = _graph()
    document["retry"] = 3
    assert _codes(document) == ["schema_invalid"]


def test_an_invented_node_field_is_refused():
    document = _graph()
    document["nodes"][0]["timeout_seconds"] = 30
    assert _codes(document) == ["schema_invalid"]


def test_a_fenced_json_answer_is_accepted():
    compiled = _compile("```json\n" + json.dumps(_graph()) + "\n```")
    assert compiled.graph.name == "Monday PR digest"


def test_the_error_report_is_machine_readable():
    document = _graph()
    document["nodes"][0]["tool"] = "nope"
    with pytest.raises(WorkflowCompileError) as caught:
        compile_document(document, REGISTRY)
    payload = caught.value.report.as_dict()
    assert payload["errors"][0]["code"] == "tool_unknown"
    assert payload["errors"][0]["node"] == "pull_prs"


# ---------------------------------------------------------------------------
# The compile loop
# ---------------------------------------------------------------------------


class ScriptedCompiler:
    """A CompilerStep that replays canned answers and records what it was asked."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: List[tuple] = []

    def __call__(self, instructions: str, input_text: str) -> str:
        self.calls.append((instructions, input_text))
        return self.answers.pop(0) if self.answers else "{}"


def _compile(
    *answers: str, max_repairs: int = 1, step: Optional[ScriptedCompiler] = None
) -> Any:
    caller = step or ScriptedCompiler(*answers)
    return compile_workflow(
        None,  # type: ignore[arg-type]
        ToolContext(workspace_id="w", user_id="u", conversation_id="c"),
        source_prompt="every Monday, pull open PRs, summarise them, post to Slack",
        registry=REGISTRY,
        step=caller,
        max_repairs=max_repairs,
    )


def test_a_bad_first_answer_is_repaired_with_the_error_list():
    broken = _graph()
    broken["nodes"][0]["tool"] = "list_pull_request"
    step = ScriptedCompiler(json.dumps(broken), json.dumps(_graph()))
    compiled = _compile(step=step)
    assert compiled.attempts == 2
    repair_input = step.calls[1][1]
    assert "tool_unknown" in repair_input
    assert "list_pull_requests" in repair_input  # the suggestion travels with it


def test_a_model_that_will_not_be_repaired_fails_closed():
    broken = json.dumps(_graph(nodes=[{"id": "a", "kind": "tool", "tool": "nope"}], edges=[]))
    step = ScriptedCompiler(broken, broken)
    with pytest.raises(WorkflowCompileError) as caught:
        _compile(step=step)
    assert [item.code for item in caught.value.report.errors] == ["tool_unknown"]
    assert len(step.calls) == 2


def test_repairs_can_be_switched_off():
    step = ScriptedCompiler("not json", json.dumps(_graph()))
    with pytest.raises(WorkflowCompileError):
        _compile(step=step, max_repairs=0)
    assert len(step.calls) == 1


def test_the_prompt_names_every_tool_and_marks_the_ones_that_write():
    catalogue = render_tool_catalogue(REGISTRY)
    assert all(name in catalogue for name in REGISTRY)
    assert "post_message (writes)" in catalogue
    assert "list_datasets (writes)" not in catalogue


def test_the_catalogue_is_bounded():
    wide = {f"tool_{index}": _spec(f"tool_{index}", {"type": "object"}) for index in range(200)}
    catalogue = render_tool_catalogue(wide, limit=10)
    assert catalogue.count("\n- ") == 10  # ten tools plus the "+190 more" line
    assert "+190 more" in catalogue


# ---------------------------------------------------------------------------
# Against a real workspace registry
# ---------------------------------------------------------------------------


def test_compiling_against_a_live_workspace_registry(client):
    """The registry the validator checks against is the one the executor will
    use, built from this workspace's database — not a static list."""
    from app.database import SessionLocal

    identity = client.get("/api/bootstrap").json()["identity"]
    db = SessionLocal()
    try:
        context = ToolContext(
            workspace_id=identity["workspace_id"],
            user_id=identity["user_id"],
            conversation_id="",
        )
        registry = build_registry(db, context)
        assert "search_sources" in registry

        compiled = compile_workflow(
            db, context, source_prompt="summarise what we know about onboarding"
        )
        assert [node.tool for node in compiled.graph.nodes if node.kind == "tool"] == [
            "search_sources"
        ]

        with pytest.raises(WorkflowCompileError) as caught:
            compile_workflow(
                db,
                context,
                source_prompt="x",
                step=ScriptedCompiler(
                    json.dumps(
                        {
                            "name": "bad",
                            "nodes": [
                                {"id": "a", "kind": "tool", "tool": "send_slack_message"}
                            ],
                            "edges": [],
                        }
                    )
                ),
                max_repairs=0,
            )
        assert caught.value.report.errors[0].code == "tool_unknown"
    finally:
        db.close()


def test_compiling_grants_no_authority(client):
    """Compilation is a proposal. It must not write a policy row, and the write
    tool it names must still resolve to `ask`."""
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import ToolPolicy
    from app.services.agent_loop import resolve_policy

    identity = client.get("/api/bootstrap").json()["identity"]
    db = SessionLocal()
    try:
        workspace_id = identity["workspace_id"]
        before = db.scalars(
            select(ToolPolicy).where(ToolPolicy.workspace_id == workspace_id)
        ).all()
        compile_document(_graph(), REGISTRY)
        after = db.scalars(
            select(ToolPolicy).where(ToolPolicy.workspace_id == workspace_id)
        ).all()
        assert len(before) == len(after)
        # In both scopes: compiling grants nothing anywhere.
        for scope in ("chat", "workflow"):
            assert (
                resolve_policy(
                    db,
                    workspace_id=workspace_id,
                    spec=REGISTRY["post_message"],
                    scope=scope,
                )
                == "ask"
            )
    finally:
        db.close()


# --------------------------------------------------------------------------
# `when` guards — validated like any other reference, plus their own shape
# --------------------------------------------------------------------------


def _guard_on_post(when: Dict[str, Any]) -> Dict[str, Any]:
    """The base graph with a `when` guard on its final node."""
    document = _graph()
    for node in document["nodes"]:
        if node["id"] == "post":
            node["when"] = when
    return document


def test_a_guard_referencing_an_upstream_node_is_accepted() -> None:
    document = _guard_on_post(
        {"left": "{{ summarise.output }}", "op": "present"}
    )
    assert _codes(document) == []


def test_a_unary_guard_may_not_carry_a_right_operand() -> None:
    document = _guard_on_post(
        {"left": "{{ summarise.output }}", "op": "present", "right": "x"}
    )
    assert "guard_unexpected_right" in _codes(document)


def test_a_guard_that_references_no_node_or_input_is_flagged_constant() -> None:
    document = _guard_on_post({"left": "high", "op": "eq", "right": "low"})
    assert "guard_constant" in [item.code for item in _warnings(document)]


def test_a_guard_referencing_a_non_upstream_node_is_refused() -> None:
    # `post` does not have `pull_prs`… actually it does (transitively); reference a
    # node that is genuinely not an ancestor by pointing at a sibling with no path.
    document = _graph()
    document["nodes"].append(
        {"id": "aside", "kind": "agent", "prompt": "unrelated"}
    )
    for node in document["nodes"]:
        if node["id"] == "summarise":
            node["when"] = {"left": "{{ aside.output }}", "op": "present"}
    assert "reference_not_upstream" in _codes(document)


def test_a_guard_referencing_an_unknown_input_is_refused() -> None:
    document = _graph()
    document["inputs"] = [
        {"name": "threshold", "type": "integer", "label": "Threshold", "required": True}
    ]
    for node in document["nodes"]:
        if node["id"] == "post":
            node["when"] = {"left": "{{ input.nonexistent }}", "op": "present"}
    assert "reference_unknown_input" in _codes(document)


# --------------------------------------------------------------------------
# Authored agents on agent nodes
# --------------------------------------------------------------------------


def test_an_agent_on_a_non_agent_node_is_misplaced() -> None:
    """`agent` names who runs a step; a tool node is run by nobody."""
    document = _graph()
    document["nodes"][0]["agent"] = "agent-1"
    assert "agent_misplaced" in _codes(document)


def test_agent_existence_is_checked_only_when_ids_are_supplied() -> None:
    """The compiler's repair loop is db-free, so it can only check shape; the
    save and run paths pass the workspace's enabled ids and get the real check."""
    document = _graph()
    for node in document["nodes"]:
        if node["id"] == "summarise":
            node["agent"] = "agent-1"
    # No ids supplied: shape passes, existence not judged.
    assert _codes(document) == []

    graph, errors = parse_graph(document)
    assert graph is not None, errors
    assert not validate_graph(graph, REGISTRY, agents={"agent-1"}).errors
    unknown = validate_graph(graph, REGISTRY, agents=set()).errors
    assert [item.code for item in unknown] == ["agent_unknown"]


def test_an_empty_agent_field_is_the_default_and_always_valid() -> None:
    """"" is what every stored graph already means; it must never be judged."""
    document = _graph()
    for node in document["nodes"]:
        if node["id"] == "summarise":
            node["agent"] = ""
    graph, errors = parse_graph(document)
    assert graph is not None, errors
    assert not validate_graph(graph, REGISTRY, agents=set()).errors
