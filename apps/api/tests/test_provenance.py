"""Unit tests on `services/provenance.py`. No run required, by design.

The module is deliberately shaped like `screen.py` — it decides nothing about
the run — so everything load-bearing about it is testable without a model, a
turn, or a park. What this file pins:

* the REGISTRY SWEEP, which is the drift guard the risk register names: every
  spec the registry would offer carries a known provenance class, and every
  spec that reaches outside this deployment is `networked`. A family added next
  year that forgets is caught here rather than by a silent hole in the gate.
* `gated_action`'s full truth table, including the two cases a reader gets
  wrong: `spec=None` is "" (an unknown tool is answered "no such tool", never
  parked on a phantom approval) and read-only + networked is "egress".
* that the builders produce byte-identical text to the `_screen` calls they
  replaced.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Agent, DbConnection, McpServer, McpTool, Workspace
from app.services import provenance
from app.services.llm_tools import ToolContext, ToolResult, ToolSpec, registry_families
from app.services.retrieval import Evidence
from app.services.web_search import WebEvidence

#: Families whose every tool reaches outside this deployment. The sweep asserts
#: `networked` over exactly these, because "does this leave the building?" is a
#: property of the family, and a family-level assertion is what survives a new
#: tool being added to one of them.
EGRESS_FAMILIES = {"mcp", "integrations", "databases", "web"}


def _spec(**kwargs: Any) -> ToolSpec:
    defaults: Dict[str, Any] = {
        "name": "probe",
        "description": "A probe.",
        "parameters": {"type": "object", "properties": {}},
        "executor": lambda db, context, args: ToolResult(content="ok"),
    }
    defaults.update(kwargs)
    return ToolSpec(**defaults)


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def owner(identity_client: Callable[..., TestClient]) -> TestClient:
    return identity_client(name="Provenance owner", workspace_name="Provenance ws")


# --------------------------------------------------------------------------
# The classes themselves


def test_only_the_user_is_trusted() -> None:
    """The whole threat model in one assertion.

    Everything that is not the user typing is untrusted — including this
    workspace's own documents and its own saved memories, which are merely not
    *gated* by default. Labelling them trusted would make the honest coverage
    gap invisible instead of configurable.
    """
    assert provenance.TRUSTED == frozenset({provenance.USER_DIRECT})
    assert provenance.UNTRUSTED == set(provenance.CLASSES) - {provenance.USER_DIRECT}
    assert len(set(provenance.CLASSES)) == len(provenance.CLASSES)


def test_the_default_class_set_is_the_two_external_ones() -> None:
    settings = get_settings()
    assert settings.taint_gating_class_set == frozenset(
        {provenance.WEB_FETCH, provenance.MCP_RESULT}
    )


def test_an_unknown_class_name_is_dropped_not_carried() -> None:
    """A typo must not disarm the classes spelled correctly beside it."""
    settings = get_settings().model_copy(
        update={"taint_gating_classes": "web_fetch, mpc_result ,mcp_result"}
    )
    assert settings.taint_gating_class_set == frozenset(
        {provenance.WEB_FETCH, provenance.MCP_RESULT}
    )


def test_an_empty_class_set_is_a_legitimate_posture() -> None:
    settings = get_settings().model_copy(update={"taint_gating_classes": ""})
    assert settings.taint_gating_class_set == frozenset()


# --------------------------------------------------------------------------
# The registry sweep — the drift guard


def test_every_registry_spec_carries_a_known_provenance_class(
    owner: TestClient, db: Any
) -> None:
    identity = owner.identity  # type: ignore[attr-defined]
    # A populated workspace: a connected MCP server with a tool, and a database
    # connection, so the two families that must be `networked` are non-empty.
    server = McpServer(
        workspace_id=identity.workspace_id,
        name="probe-server",
        url="https://mcp.example.com/sse",
        status="connected",
    )
    db.add(server)
    db.flush()
    db.add(
        McpTool(
            workspace_id=identity.workspace_id,
            server_id=server.id,
            name="lookup",
            description="Look something up.",
            input_schema_json='{"type": "object", "properties": {}}',
        )
    )
    db.add(
        DbConnection(
            workspace_id=identity.workspace_id,
            name="warehouse",
            engine="postgres",
            dsn_encrypted="x",
            status="connected",
        )
    )
    db.commit()

    context = ToolContext(
        workspace_id=identity.workspace_id,
        user_id=identity.user_id,
        conversation_id="",
    )
    families = registry_families(db, context)
    seen = 0
    for family, tools in families:
        for name, spec in tools.items():
            seen += 1
            assert spec.provenance in provenance.CLASSES, (
                f"{family}.{name} carries an unknown provenance class "
                f"{spec.provenance!r} — add it to provenance.CLASSES or fix "
                "the spec"
            )
            if family in EGRESS_FAMILIES:
                assert spec.networked, (
                    f"{family}.{name} reaches outside this deployment but is "
                    "not marked networked, so the taint gate will not hold it"
                )
    assert seen > 10, "the sweep swept nothing; the registry did not build"
    # The sweep is only worth anything if the two egress families it exists for
    # actually built. An empty family passes its assertion vacuously, so the
    # fixture above is part of the test and this is what says so.
    populated = {family for family, tools in families if tools}
    assert {"mcp", "databases"} <= populated, populated

    by_name = {name: spec for _family, tools in families for name, spec in tools.items()}
    # The stamps that are not merely "a class exists", spot-checked.
    assert by_name["search_sources"].provenance == provenance.WORKSPACE_CHUNK
    assert by_name["recall_memory"].provenance == provenance.MEMORY_ITEM
    assert by_name["ask_user"].provenance == provenance.USER_DIRECT
    assert not by_name["search_sources"].networked
    mcp_names = [name for name in by_name if "probe-server" in name]
    assert mcp_names, "the MCP family did not build"
    assert by_name[mcp_names[0]].provenance == provenance.MCP_RESULT


def test_the_plan_exit_gesture_is_user_direct() -> None:
    """Not a registry family, so the sweep above cannot see it."""
    from app.services.llm_tools import exit_plan_mode_spec

    assert exit_plan_mode_spec().provenance == provenance.USER_DIRECT


# --------------------------------------------------------------------------
# classify_evidence


def _passage(excerpt: str) -> Evidence:
    return Evidence(
        chunk_id="c1",
        source_id="s1",
        filename="notes.txt",
        ordinal=0,
        excerpt=excerpt,
        score=0.5,
    )


def _web(excerpt: str) -> WebEvidence:
    return WebEvidence(
        chunk_id="web:abc",
        source_id="",
        filename="example.com",
        ordinal=0,
        excerpt=excerpt,
        score=0.0,
        url="https://example.com/a",
    )


def test_classify_evidence_separates_the_web_from_the_library() -> None:
    assert provenance.classify_evidence(_passage("x")) == provenance.WORKSPACE_CHUNK
    assert provenance.classify_evidence(_web("x")) == provenance.WEB_FETCH


# --------------------------------------------------------------------------
# gated_action — the full truth table


def test_gated_action_answers_nothing_for_an_unknown_tool() -> None:
    """`spec is None` must never park a run on an approval for a tool that
    does not exist: `evaluate_policy` resolves it to allow so the model gets a
    plain "no such tool" error back."""
    assert provenance.gated_action(None) == ""


def test_gated_action_egress_is_checked_before_write() -> None:
    """A READ-ONLY networked call is still gated.

    This is the half a write-only gate misses entirely: a tool that reaches the
    network on the workspace's behalf can carry data out of it, whatever it
    claims to do with what it finds.
    """
    assert provenance.gated_action(_spec(read_only=True, networked=True)) == "egress"
    assert provenance.gated_action(_spec(read_only=False, networked=True)) == "egress"
    assert provenance.gated_action(_spec(read_only=False, networked=False)) == "write"
    assert provenance.gated_action(_spec(read_only=True, networked=False)) == ""


# --------------------------------------------------------------------------
# gate_reason


def test_gate_reason_sorts_its_classes() -> None:
    assert (
        provenance.gate_reason(
            classes={provenance.WEB_FETCH, provenance.MCP_RESULT}, action="write"
        )
        == "taint:mcp_result,web_fetch:write"
    )


def test_gate_reason_is_empty_without_an_action_or_a_known_class() -> None:
    assert provenance.gate_reason(classes={provenance.WEB_FETCH}, action="") == ""
    assert provenance.gate_reason(classes={"nonsense"}, action="write") == ""


def test_gate_reason_fits_the_column_and_stays_parseable() -> None:
    """Over 64 characters, WHOLE classes come off the end — and say so.

    The web parses this string exactly and renders nothing it cannot parse, so
    a truncation that split a class name mid-word would cost the reviewer the
    entire sentence — strictly worse than an incomplete list. A drop is
    announced as a `+N` segment rather than left silent; see
    `test_gate_reason_announces_the_classes_it_had_to_drop`.
    """
    rendered = provenance.gate_reason(classes=set(provenance.CLASSES), action="egress")
    assert len(rendered) <= provenance.MAX_GATE_REASON_CHARS
    body = rendered[len("taint:") : -len(":egress")]
    assert body
    for name in body.split(","):
        assert name in provenance.CLASSES or name.startswith("+")


# --------------------------------------------------------------------------
# The builders


def test_turn_start_blocks_are_byte_identical_for_one_evidence_class() -> None:
    evidence = [_passage("alpha"), _passage("beta")]
    blocks = provenance.turn_start_blocks(
        prompt="hello",
        evidence=evidence,
        spliced_context="the open doc",
        memory_context="a memory",
    )
    by_kind = {block.kind: block for block in blocks}
    # Exactly what `_screen(kind="evidence", ...)` was handed before.
    assert by_kind["evidence"].text == "\n\n".join(item.excerpt for item in evidence)
    assert by_kind["evidence"].provenance == provenance.WORKSPACE_CHUNK
    assert by_kind["document"].text == "the open doc"
    assert by_kind["memory"].text == "a memory"
    # The prompt is present and TRUSTED, so `_ingest` never screens it.
    assert by_kind["prompt"].provenance == provenance.USER_DIRECT
    assert not by_kind["prompt"].untrusted
    assert not by_kind["prompt"].recordable


def test_a_mixed_evidence_list_splits_by_class() -> None:
    """The one deliberate behaviour change at turn start.

    A hosted web-search excerpt beside a library passage is recorded as
    `web_fetch`, not averaged into `workspace_chunk`. The screen's own combine
    rule is max-over-chunks, so no verdict can flip — only the event count for
    such a turn, which no existing test asserts.
    """
    blocks = provenance.turn_start_blocks(
        prompt="hi",
        evidence=[_passage("mine"), _web("theirs")],
        spliced_context="",
        memory_context="",
    )
    evidence_blocks = [block for block in blocks if block.kind == "evidence"]
    assert {block.provenance for block in evidence_blocks} == {
        provenance.WORKSPACE_CHUNK,
        provenance.WEB_FETCH,
    }
    web = [
        block for block in evidence_blocks if block.provenance == provenance.WEB_FETCH
    ]
    assert [block.text for block in web] == ["theirs"]


def test_tool_result_blocks_keep_the_old_joined_string() -> None:
    spec = _spec(name="probe_read", provenance=provenance.MCP_RESULT)
    result = ToolResult(content="body", evidence=[_passage("quoted")])
    blocks = provenance.tool_result_blocks(spec, result)
    assert len(blocks) == 1
    assert blocks[0].text == "body\n\nquoted"
    assert blocks[0].provenance == provenance.MCP_RESULT
    assert blocks[0].kind == "tool_output"


def test_a_reported_class_becomes_a_block_of_its_own() -> None:
    """The delegation seam. A child writes no run events, so the only way the
    parent learns its sub-agent read a web page is the result saying so."""
    spec = _spec(name="delegate", provenance=provenance.TOOL_RESULT)
    result = ToolResult(content="the child answered", provenance=[provenance.WEB_FETCH])
    blocks = provenance.tool_result_blocks(spec, result)
    assert [block.provenance for block in blocks] == [
        provenance.TOOL_RESULT,
        provenance.WEB_FETCH,
    ]
    # No text, so nothing extra is screened — but it IS recordable, which is
    # the whole point: the class has to reach the ledger.
    assert blocks[1].text == ""
    assert blocks[1].recordable


def test_a_reported_user_direct_class_cannot_launder_anything() -> None:
    """An executor reporting `user_direct` adds no block: only `ask_user`'s own
    spec may class output as the user speaking, and a result that claimed it
    would be the one way an untrusted path could wash itself clean."""
    spec = _spec(name="probe", provenance=provenance.TOOL_RESULT)
    result = ToolResult(content="x", provenance=[provenance.USER_DIRECT])
    assert len(provenance.tool_result_blocks(spec, result)) == 1


def test_web_search_blocks_keep_the_old_joined_string() -> None:
    items: List[Evidence] = [_web("one"), _web("two")]
    blocks = provenance.web_search_blocks(items)
    assert [block.text for block in blocks] == ["one\n\ntwo"]
    assert blocks[0].kind == "web_search"
    assert blocks[0].provenance == provenance.WEB_FETCH


# --------------------------------------------------------------------------
# gating_classes — the posture resolution


def test_gating_classes_follows_the_deployment_by_default(
    owner: TestClient, db: Any
) -> None:
    identity = owner.identity  # type: ignore[attr-defined]
    assert provenance.gating_classes(
        db, workspace_id=identity.workspace_id, settings=get_settings()
    ) == frozenset({provenance.WEB_FETCH, provenance.MCP_RESULT})


def test_the_deployment_flag_off_gates_nothing(owner: TestClient, db: Any) -> None:
    identity = owner.identity  # type: ignore[attr-defined]
    settings = get_settings().model_copy(update={"taint_gating_enabled": False})
    assert (
        provenance.gating_classes(
            db, workspace_id=identity.workspace_id, settings=settings
        )
        == frozenset()
    )


@pytest.mark.parametrize(
    "stored, gated",
    [
        ("", True),
        ("on", True),
        ("off", False),
        # An unrecognised column value must TIGHTEN, never disarm — the same
        # rule `approval_mode_for_run` applies to an unknown approval_mode.
        ("maybe", True),
        ("OFF", False),
    ],
)
def test_the_workspace_override_resolves_one_way_only(
    owner: TestClient, db: Any, stored: str, gated: bool
) -> None:
    identity = owner.identity  # type: ignore[attr-defined]
    workspace = db.scalar(
        select(Workspace).where(Workspace.id == identity.workspace_id)
    )
    workspace.taint_gating = stored
    db.commit()
    try:
        resolved = provenance.gating_classes(
            db, workspace_id=identity.workspace_id, settings=get_settings()
        )
        assert bool(resolved) is gated
    finally:
        workspace.taint_gating = ""
        db.commit()


def test_gate_reason_announces_the_classes_it_had_to_drop() -> None:
    """A short list must not read as a complete one.

    The names sort alphabetically and the column holds 64 characters, so the
    tail this drops is always `workspace_chunk` then `web_fetch` — the class
    the feature is named after. A reviewer shown "an MCP result and a saved
    memory", with the fetched page silently absent, would approve an egress
    call on an incomplete account of what the turn read.
    """
    rendered = provenance.gate_reason(classes=provenance.UNTRUSTED, action="egress")
    assert len(rendered) <= provenance.MAX_GATE_REASON_CHARS
    head, names, action = rendered.split(":")
    assert (head, action) == ("taint", "egress")
    listed = names.split(",")
    assert listed[-1].startswith("+"), rendered
    dropped = int(listed[-1][1:])
    assert dropped == len(provenance.UNTRUSTED) - (len(listed) - 1)
    # The unelided case is untouched — the default posture never truncates.
    assert (
        provenance.gate_reason(
            classes=[provenance.WEB_FETCH, provenance.MCP_RESULT], action="write"
        )
        == "taint:mcp_result,web_fetch:write"
    )


def test_a_prompt_is_trusted_unless_the_caller_says_where_it_came_from() -> None:
    """`user_direct` is a claim about the prompt, and it has to be true.

    A workflow agent node's prompt is a template with an upstream tool's
    output spliced into it. Stamping that `user_direct` laundered a
    compromised MCP server through the one class that is neither screened nor
    able to arm the gate.
    """
    default = provenance.prompt_blocks("What changed?", ())
    assert [block.provenance for block in default] == [provenance.USER_DIRECT]
    assert default[0].untrusted is False

    classed = provenance.prompt_blocks(
        "Handle this ticket: <payload>",
        [provenance.TOOL_RESULT, provenance.MCP_RESULT],
    )
    # The worst class carries the text (so it is the one screened and the one
    # an incident reader meets first); the rest ride as text-less reports.
    assert [block.provenance for block in classed] == [
        provenance.MCP_RESULT,
        provenance.TOOL_RESULT,
    ]
    assert classed[0].text == "Handle this ticket: <payload>"
    assert [block.recordable for block in classed] == [True, True]
    assert classed[1].text == ""
    assert classed[1].reported is True


def test_turn_start_blocks_pass_the_prompt_classes_through() -> None:
    blocks = provenance.turn_start_blocks(
        prompt="Summarise {{ fetch.output }}",
        evidence=[],
        spliced_context="",
        memory_context="",
        prompt_classes=[provenance.WEB_FETCH],
    )
    assert blocks[0].provenance == provenance.WEB_FETCH
    assert blocks[0].kind == "prompt"
    # Unchanged without them, which is every chat turn.
    plain = provenance.turn_start_blocks(
        prompt="Summarise it", evidence=[], spliced_context="", memory_context=""
    )
    assert plain[0].provenance == provenance.USER_DIRECT
    assert plain[0].label == "what you asked"


def test_an_agent_exists_for_the_probe_workspace(owner: TestClient, db: Any) -> None:
    """Guard on the fixture itself: `identity_client` is supposed to create an
    agent, and a sweep over an agent-less workspace would pass vacuously."""
    identity = owner.identity  # type: ignore[attr-defined]
    assert (
        db.scalar(select(Agent).where(Agent.workspace_id == identity.workspace_id))
        is not None
    )
