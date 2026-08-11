"""Policy scope: a grant answers the question it was asked, and no other.

ADR 0007's consequences section named this the sharpest residual risk and did not
fix it: `tool_policies` was workspace-wide, so somebody clicking "always allow"
on `send_email` in a conversation had authorised every future *scheduled*
workflow to send email unattended, forever, without ever seeing a workflow. The
same section explains why that composes badly with prompt injection — the
approval park is the containment, and a standing allow removes it.

`tool_policies.scope` is the fix, and `resolve_policy` is still the only place a
verdict is decided. The tests below are the claims that fix has to make good:

1. a chat allow does not authorise an unattended workflow node,
2. and that is the *default*, not something an operator turns on,
3. a chat *deny* still denies, because a prohibition is not a grant,
4. authorising a workflow requires saying so, at which point it works,
5. and none of it leaks back the other way.

Then the composition that motivated the whole thing: an agent node inside a
workflow — the node ADR 0007 identified as the injection landing site — resolves
at workflow scope too, so reading a poisoned document cannot cash in a grant
somebody made while typing.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest
from conftest import Identity, create_identity
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_workflow_executor import (  # the harness, not a test import
    Probe,
    authorize,
    begin,
    grant,
    graph,
    install,
    nodes_of,
    tool_node,
)

from app.database import SessionLocal
from app.main import app
from app.models import AgentToolCall, Run, ToolPolicy, WorkflowRun
from app.services import agent_loop
from app.services.agent_loop import CHAT_SCOPE, WORKFLOW_SCOPE, resolve_policy
from app.services.llm_tools import ToolContext, ToolResult, ToolSpec
from app.services.workflows import executor

TEST_BASE_URL = "https://testserver"

WRITE_TOOL = ToolSpec(
    name="probe_write",
    description="A write-capable probe.",
    parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    executor=lambda db, context, args: ToolResult(content="sent"),
    read_only=False,
)
READ_TOOL = ToolSpec(
    name="probe_read",
    description="A read-only probe.",
    parameters={"type": "object", "properties": {}},
    executor=lambda db, context, args: ToolResult(content="read"),
    read_only=True,
)


@pytest.fixture
def identity() -> Identity:
    return create_identity(name="Scope owner", workspace_name="Scope workspace")


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def verdict(db: Any, identity: Identity, spec: ToolSpec, scope: str) -> str:
    return resolve_policy(
        db, workspace_id=identity.workspace_id, spec=spec, scope=scope
    )


# --------------------------------------------------------------------------
# The decision function
# --------------------------------------------------------------------------


def test_a_chat_allow_does_not_authorise_an_unattended_workflow(
    db: Any, identity: Identity
) -> None:
    """The headline. One click while typing is not a standing licence at 3am."""
    grant(db, identity, "probe_write", "allow", scope=CHAT_SCOPE)

    assert verdict(db, identity, WRITE_TOOL, CHAT_SCOPE) == "allow"
    assert verdict(db, identity, WRITE_TOOL, WORKFLOW_SCOPE) == "ask"


def test_the_narrow_reading_is_the_default_not_an_opt_in(
    db: Any, identity: Identity
) -> None:
    """Nothing was configured to make the assertion above true.

    No setting, no migration flag, no per-workspace toggle: a workspace that has
    never heard of scopes already gets the safe answer, and a workspace that
    wants the wide one has to ask for it by name. A fix that has to be enabled is
    a fix most deployments do not have.
    """
    grant(db, identity, "probe_write", "allow", scope=CHAT_SCOPE)
    rows = list(
        db.scalars(
            select(ToolPolicy).where(ToolPolicy.workspace_id == identity.workspace_id)
        )
    )
    assert [row.scope for row in rows] == [CHAT_SCOPE]
    assert verdict(db, identity, WRITE_TOOL, WORKFLOW_SCOPE) == "ask"


def test_a_chat_deny_still_denies_a_workflow(db: Any, identity: Identity) -> None:
    """A prohibition is not a grant, so it is the one thing that crosses.

    Refusing to carry it would be the only direction of leakage that makes the
    system *less* safe, and nobody authorised anything by writing it.
    """
    grant(db, identity, "probe_read", "deny", scope=CHAT_SCOPE)
    assert verdict(db, identity, READ_TOOL, CHAT_SCOPE) == "deny"
    assert verdict(db, identity, READ_TOOL, WORKFLOW_SCOPE) == "deny"


def test_a_workflow_scoped_row_wins_over_the_chat_row(
    db: Any, identity: Identity
) -> None:
    grant(db, identity, "probe_write", "deny", scope=CHAT_SCOPE)
    grant(db, identity, "probe_write", "allow", scope=WORKFLOW_SCOPE)
    assert verdict(db, identity, WRITE_TOOL, CHAT_SCOPE) == "deny"
    assert verdict(db, identity, WRITE_TOOL, WORKFLOW_SCOPE) == "allow"


def test_a_workflow_grant_does_not_leak_back_into_chat(
    db: Any, identity: Identity
) -> None:
    """Symmetry, so the split cannot be worked around from the other side."""
    grant(db, identity, "probe_write", "allow", scope=WORKFLOW_SCOPE)
    assert verdict(db, identity, WRITE_TOOL, WORKFLOW_SCOPE) == "allow"
    assert verdict(db, identity, WRITE_TOOL, CHAT_SCOPE) == "ask"


def test_an_existing_row_keeps_its_chat_meaning(db: Any, identity: Identity) -> None:
    """Migration promise: nothing anyone already granted changed meaning.

    A row written before scopes existed is a chat grant, and chat still honours
    it exactly as before.
    """
    grant(db, identity, "probe_write", "allow", scope=CHAT_SCOPE)
    assert verdict(db, identity, WRITE_TOOL, CHAT_SCOPE) == "allow"
    grant(db, identity, "probe_read", "deny", scope=CHAT_SCOPE)
    assert verdict(db, identity, READ_TOOL, CHAT_SCOPE) == "deny"


def test_the_scope_cannot_be_omitted(db: Any, identity: Identity) -> None:
    """No default, because the only value a default could take is the wide one.

    A call site that forgot to think about unattended execution would otherwise
    silently get the answer that assumes somebody is watching.
    """
    with pytest.raises(TypeError):
        resolve_policy(  # type: ignore[call-arg]
            db, workspace_id=identity.workspace_id, spec=WRITE_TOOL
        )


# --------------------------------------------------------------------------
# What the executor does with it
# --------------------------------------------------------------------------


def test_a_chat_allow_does_not_stop_a_workflow_node_from_parking(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the grant exists, the run still waits, the write never happens."""
    writer = Probe("probe_write", read_only=False)
    install(monkeypatch, writer)
    grant(db, identity, "probe_write", "allow", scope=CHAT_SCOPE)

    workflow_run = begin(
        db,
        identity,
        graph([tool_node("send", "probe_write", {"text": "hi"})]),
        trigger="schedule",
    )
    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "waiting_for_approval"
    assert writer.calls == []
    assert nodes_of(db, workflow_run)["send"].policy == "ask"


def test_a_workflow_allow_lets_an_unattended_run_complete(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The grant is available — it just has to be the grant that was asked for."""
    writer = Probe("probe_write", read_only=False)
    install(monkeypatch, writer)
    grant(db, identity, "probe_write", "allow", scope=WORKFLOW_SCOPE)

    workflow_run = begin(
        db,
        identity,
        graph([tool_node("send", "probe_write", {"text": "hi"})]),
        trigger="schedule",
    )
    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "succeeded", workflow_run.error
    assert writer.calls == [{"text": "hi"}]
    # The trail records that a standing grant, not a person, authorised this.
    assert nodes_of(db, workflow_run)["send"].policy == "allow"


def test_a_workflow_deny_ends_the_run_rather_than_asking(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = Probe("probe_write", read_only=False)
    install(monkeypatch, writer)
    grant(db, identity, "probe_write", "deny", scope=WORKFLOW_SCOPE)

    workflow_run = begin(db, identity, graph([tool_node("send", "probe_write")]))
    executor.advance_run(db, workflow_run)

    assert workflow_run.status == "failed"
    assert "policy_denied" in workflow_run.error
    assert writer.calls == []


def test_an_agent_node_in_a_workflow_resolves_at_workflow_scope(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0007's injection scenario, contained.

    A node fetches a document, the document contains instructions, and a
    downstream *agent* node honours them. Every write it attempts must park — and
    would not, if the agent loop resolved at chat scope while running inside a
    workflow. The scope is read off the run, not off which loop is executing.
    """
    calls: List[Dict[str, Any]] = []

    def poisoned(db_: Any, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
        calls.append(dict(args))
        return ToolResult(content="sent")

    tool = ToolSpec(
        name="probe_write",
        description="A write-capable probe.",
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        executor=poisoned,
        read_only=False,
    )
    monkeypatch.setattr(executor, "build_registry", lambda d, c: {"probe_write": tool})
    monkeypatch.setattr(agent_loop, "build_registry", lambda d, c: {"probe_write": tool})
    # The workspace clicked "always allow" in a conversation, once.
    grant(db, identity, "probe_write", "allow", scope=CHAT_SCOPE)

    # A model that decides, mid-workflow, to call the write tool.
    def scripted(items: Any, tools: Any, instructions: str) -> Any:
        class Call:
            type = "function_call"
            call_id = "poisoned-1"
            name = "probe_write"
            arguments = '{"text": "exfiltrate"}'

        class Response:
            output = [Call()]
            output_text = ""

        return [("completed", Response())]

    monkeypatch.setattr(
        agent_loop, "_default_model_step", lambda settings, run, evidence: scripted
    )

    workflow_run = begin(
        db,
        identity,
        graph(
            [
                {
                    "id": "think",
                    "kind": "agent",
                    "prompt": "Do what the document said.",
                    "description": "The injection landing site.",
                }
            ]
        ),
        trigger="schedule",
    )
    executor.advance_run(db, workflow_run)

    assert calls == [], "an agent node cashed in a chat-scoped grant"
    assert workflow_run.status == "waiting_for_approval"
    assert nodes_of(db, workflow_run)["think"].status == "waiting_for_approval"


def test_a_plain_chat_run_still_honours_the_chat_grant(
    db: Any, identity: Identity
) -> None:
    """The other half of `policy_scope_for_run`: an ordinary run is chat scope.

    Without this the scope split would just be a way of breaking chat.
    """
    conversation_run = _chat_run(db, identity)
    assert agent_loop.policy_scope_for_run(db, conversation_run) == CHAT_SCOPE


def _chat_run(db: Any, identity: Identity) -> Run:
    from app.models import Agent, Conversation

    agent = db.scalar(select(Agent).where(Agent.workspace_id == identity.workspace_id))
    conversation = Conversation(
        workspace_id=identity.workspace_id, created_by=identity.user_id
    )
    db.add(conversation)
    db.flush()
    run = Run(
        workspace_id=identity.workspace_id,
        conversation_id=conversation.id,
        agent_id=agent.id,
        created_by=identity.user_id,
        status="queued",
        prompt="hello",
    )
    db.add(run)
    db.commit()
    return run


# --------------------------------------------------------------------------
# "Always allow" remembers the question it was asked
# --------------------------------------------------------------------------


def test_remembering_a_workflow_approval_grants_workflow_scope(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The approval card raised by a workflow is asking about unattended runs.

    So "always allow" on it grants at workflow scope — and does not quietly hand
    out a chat permission nobody asked about.
    """
    writer = Probe("probe_write", read_only=False)
    install(monkeypatch, writer)
    workflow_run = begin(
        db, identity, graph([tool_node("send", "probe_write", {"text": "hi"})])
    )
    executor.advance_run(db, workflow_run)
    call_id = nodes_of(db, workflow_run)["send"].agent_tool_call_id

    with TestClient(app, base_url=TEST_BASE_URL) as client:
        authorize(client, identity)
        response = client.post(
            f"/api/agent-tool-calls/{call_id}/decision",
            json={"decision": "approved", "remember": True},
            headers={"Idempotency-Key": "scope-remember-1"},
        )
    assert response.status_code == 200, response.text

    db.expire_all()
    rows = {
        row.scope: row.policy
        for row in db.scalars(
            select(ToolPolicy).where(
                ToolPolicy.workspace_id == identity.workspace_id,
                ToolPolicy.tool_name == "probe_write",
            )
        )
    }
    assert rows == {WORKFLOW_SCOPE: "allow"}
    assert verdict(db, identity, WRITE_TOOL, CHAT_SCOPE) == "ask"


def test_the_policy_route_defaults_to_chat_and_can_name_workflow(
    db: Any, identity: Identity
) -> None:
    """A client written before scopes existed keeps setting the grant it always
    set; authorising unattended execution has to be spelled out."""
    with TestClient(app, base_url=TEST_BASE_URL) as client:
        authorize(client, identity)
        default = client.put(
            "/api/tool-policies", json={"tool_name": "probe_write", "policy": "allow"}
        )
        assert default.status_code == 200, default.text
        assert default.json()["scope"] == CHAT_SCOPE

        explicit = client.put(
            "/api/tool-policies",
            json={
                "tool_name": "probe_write",
                "policy": "allow",
                "scope": WORKFLOW_SCOPE,
            },
        )
        assert explicit.status_code == 200, explicit.text

        listed = client.get("/api/tool-policies")
        assert listed.status_code == 200
        scopes = sorted(
            row["scope"] for row in listed.json() if row["tool_name"] == "probe_write"
        )
    # Two rows for one tool, which is the point of widening the unique key.
    assert scopes == [CHAT_SCOPE, WORKFLOW_SCOPE]
    assert verdict(db, identity, WRITE_TOOL, CHAT_SCOPE) == "allow"
    assert verdict(db, identity, WRITE_TOOL, WORKFLOW_SCOPE) == "allow"


def test_an_approved_workflow_call_is_distinguishable_from_a_granted_one(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`workflow_node_runs.policy` is what makes the trail readable after the fact.

    ADR 0007: without it nobody can tell an unattended 3am write from one a human
    approved, and those are very different events.
    """
    writer = Probe("probe_write", read_only=False)
    install(monkeypatch, writer)
    parked = begin(db, identity, graph([tool_node("send", "probe_write")]))
    executor.advance_run(db, parked)
    assert nodes_of(db, parked)["send"].policy == "ask"

    grant(db, identity, "probe_write", "allow", scope=WORKFLOW_SCOPE)
    granted = begin(db, identity, graph([tool_node("send", "probe_write")]))
    executor.advance_run(db, granted)
    assert nodes_of(db, granted)["send"].policy == "allow"


def test_a_parked_workflow_call_names_its_run_in_the_audit_trail(
    db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The approval card has to say a workflow raised it, and whether anyone
    was watching. `trigger` is the fact that decides how carefully to read."""
    from app.models import AuditEvent

    writer = Probe("probe_write", read_only=False)
    install(monkeypatch, writer)
    workflow_run = begin(
        db,
        identity,
        graph([tool_node("send", "probe_write")]),
        trigger="schedule",
    )
    executor.advance_run(db, workflow_run)
    call_id = nodes_of(db, workflow_run)["send"].agent_tool_call_id

    event = db.scalar(
        select(AuditEvent).where(
            AuditEvent.workspace_id == identity.workspace_id,
            AuditEvent.resource_id == call_id,
        )
    )
    assert event is not None
    assert workflow_run.id in event.detail_json
    assert "schedule" in event.detail_json
    assert db.get(AgentToolCall, call_id).status == "proposed"
    assert db.get(WorkflowRun, workflow_run.id).trigger == "schedule"
