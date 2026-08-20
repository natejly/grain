"""Approval modes: how much one conversation asks before acting.

Three modes, held on the `Conversation` and applied by `resolve_policy`, which is
still the only place a verdict is decided:

    ask_writes   read-only tools run, writes park. The default, and what every
                 thread that predates this feature has always done.
    ask_all      everything parks, searches included.
    auto_writes  writes execute without parking. The bypass.

The mode is the first thing in this system that can *remove* an approval park,
and the approval park is the containment ADR 0007 leans on. So the tests below
are not mostly about the happy path; they are about the four properties that
make a bypass safe enough to ship at all:

1. a chat mode cannot reach workflow scope, so nothing set while typing governs
   an unattended 3am run — including through the Conversation the workflow
   executor makes for its own backing Run, which is the back door;
2. a `deny` row still denies under `auto_writes`, because a prohibition is not a
   grant and the bypass is permission to skip asking, not to overrule a refusal;
3. the audit does not lie — a call the mode let through records the *mode* as its
   decider, never a person, and a call the policy table already allowed does not
   get attributed to the mode either;
4. mode changes are themselves audited.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pytest
from conftest import Identity
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    Agent,
    AgentToolCall,
    AuditEvent,
    Conversation,
    Run,
    RunEvent,
    ToolPolicy,
    Workflow,
    WorkflowRun,
)
from app.services import agent_loop
from app.services.agent_loop import (
    ASK_ALL,
    ASK_WRITES,
    AUTO_WRITES,
    CHAT_SCOPE,
    WORKFLOW_SCOPE,
    approval_mode_for_run,
    evaluate_policy,
    resolve_policy,
    run_agent_turn,
)
from app.services.llm_tools import ToolContext, ToolResult, ToolSpec

# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, output: Optional[List[Any]] = None, output_text: str = ""):
        self.output = output or []
        self.output_text = output_text


class Probe:
    """A tool that records what it was actually asked to do.

    Every assertion about a bypass eventually comes down to "did the side effect
    happen", and a policy verdict read back out of the database cannot answer
    that: a run can record `approved` and still never reach the executor.
    """

    def __init__(self, name: str, *, read_only: bool) -> None:
        self.name = name
        self.read_only = read_only
        self.calls: List[Dict[str, Any]] = []

    def spec(self) -> ToolSpec:
        def run(db: Any, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
            self.calls.append(dict(args))
            return ToolResult(content="ok")

        return ToolSpec(
            name=self.name,
            description="A probe.",
            parameters={"type": "object", "properties": {}},
            executor=run,
            read_only=self.read_only,
        )


def install(monkeypatch: pytest.MonkeyPatch, *probes: Probe) -> None:
    """Make `probes` the entire registry the agent loop builds."""
    registry = {probe.name: probe.spec() for probe in probes}
    monkeypatch.setattr(
        agent_loop,
        "build_registry",
        lambda db, context, allowed=None: {
            name: spec
            for name, spec in registry.items()
            if allowed is None or name in set(allowed)
        },
    )


def calls_then_answers(
    name: str, arguments: str = "{}"
) -> Callable[[List[Any], List[Dict[str, Any]], str], Iterable[Tuple[str, Any]]]:
    """A model that asks for one tool call, then answers."""
    seen = {"steps": 0}

    def step(
        input_items: List[Any], tools: List[Dict[str, Any]], instructions: str
    ) -> Iterable[Tuple[str, Any]]:
        seen["steps"] += 1
        if seen["steps"] == 1:
            return [
                (
                    "completed",
                    FakeResponse(
                        output=[
                            SimpleNamespace(
                                type="function_call",
                                name=name,
                                call_id="probe-1",
                                arguments=arguments,
                            )
                        ]
                    ),
                )
            ]
        return [("completed", FakeResponse(output=[], output_text="Done."))]

    return step


def make_run(db: Any, identity: Identity, conversation_id: str) -> Run:
    agent = db.scalar(
        select(Agent).where(Agent.workspace_id == identity.workspace_id)
    )
    assert agent is not None
    run = Run(
        workspace_id=identity.workspace_id,
        conversation_id=conversation_id,
        agent_id=agent.id,
        created_by=identity.user_id,
        status="running",
        prompt="Do the thing",
    )
    db.add(run)
    db.commit()
    return run


def new_conversation(client: TestClient, title: str = "Modes") -> Dict[str, Any]:
    response = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "mode-conv-" + os.urandom(6).hex()},
        json={"title": title},
    )
    assert response.status_code == 201, response.text
    payload: Dict[str, Any] = response.json()
    return payload


def set_mode(client: TestClient, conversation_id: str, mode: str) -> Any:
    return client.put(
        f"/api/conversations/{conversation_id}/approval-mode", json={"mode": mode}
    )


def only_call(db: Any, run_id: str) -> AgentToolCall:
    calls = list(
        db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run_id))
    )
    assert len(calls) == 1, calls
    return calls[0]


def grant(db: Any, identity: Identity, tool_name: str, policy: str, scope: str) -> None:
    db.add(
        ToolPolicy(
            workspace_id=identity.workspace_id,
            tool_name=tool_name,
            policy=policy,
            scope=scope,
            created_by=identity.user_id,
        )
    )
    db.commit()


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def owner(identity_client: Callable[..., TestClient]) -> TestClient:
    """A client in a workspace of its own.

    Each test gets a fresh tenant so that a `tool_policies` row written here can
    never be read by a neighbouring spec — these rows are workspace-wide and the
    suite shares one database.
    """
    return identity_client(name="Mode owner", workspace_name="Mode workspace")


def identity_of(client: TestClient) -> Identity:
    return client.identity  # type: ignore[attr-defined,no-any-return]


WRITE = "probe_write"
READ = "probe_read"


# --------------------------------------------------------------------------
# The default is what it always was
# --------------------------------------------------------------------------


def test_a_new_conversation_asks_before_writes_and_says_so(owner: TestClient) -> None:
    conversation = new_conversation(owner)
    assert conversation["approval_mode"] == ASK_WRITES
    listed = owner.get("/api/conversations").json()
    assert [row["approval_mode"] for row in listed if row["id"] == conversation["id"]] == [
        ASK_WRITES
    ]


def test_ask_writes_parks_a_write_and_runs_a_read(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behaviour every existing thread has. Nothing below changes it."""
    write, read = Probe(WRITE, read_only=False), Probe(READ, read_only=True)
    install(monkeypatch, write, read)
    identity = identity_of(owner)

    parked = make_run(db, identity, new_conversation(owner)["id"])
    assert run_agent_turn(db, parked, evidence=[], model_step=calls_then_answers(WRITE)) is None
    assert write.calls == []
    assert only_call(db, parked.id).status == "proposed"

    ran = make_run(db, identity, new_conversation(owner)["id"])
    assert run_agent_turn(db, ran, evidence=[], model_step=calls_then_answers(READ)) is not None
    assert read.calls == [{}]


def test_an_unattended_read_claims_no_decision(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only tool nobody had to think about leaves `decided_by` empty.

    That blank is load-bearing for the rest of this file: it is what a stamped
    row is distinguishable *from*. If every executed call carried a decider,
    "the mode let this through" would be indistinguishable from "there was
    nothing to decide".
    """
    read = Probe(READ, read_only=True)
    install(monkeypatch, read)
    run = make_run(db, identity_of(owner), new_conversation(owner)["id"])

    assert run_agent_turn(db, run, evidence=[], model_step=calls_then_answers(READ)) is not None
    call = only_call(db, run.id)
    assert call.status == "succeeded"
    assert not call.decided_by
    assert call.decided_at is None


# --------------------------------------------------------------------------
# ask_all
# --------------------------------------------------------------------------


def test_ask_all_parks_even_a_read_only_tool(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of the mode: see each search before it runs."""
    read = Probe(READ, read_only=True)
    install(monkeypatch, read)
    conversation = new_conversation(owner)
    assert set_mode(owner, conversation["id"], ASK_ALL).status_code == 200
    run = make_run(db, identity_of(owner), conversation["id"])

    assert run_agent_turn(db, run, evidence=[], model_step=calls_then_answers(READ)) is None
    assert read.calls == []
    assert only_call(db, run.id).status == "proposed"


def test_ask_all_overrides_a_standing_allow(owner: TestClient, db: Any) -> None:
    """"Always allow", clicked once, is not a reason to skip the look a thread
    has just explicitly asked to take."""
    identity = identity_of(owner)
    grant(db, identity, READ, "allow", CHAT_SCOPE)
    spec = Probe(READ, read_only=True).spec()

    assert resolve_policy(
        db,
        workspace_id=identity.workspace_id,
        user_id=identity.user_id,
        spec=spec,
        scope=CHAT_SCOPE,
    ) == "allow"
    assert resolve_policy(
        db,
        workspace_id=identity.workspace_id,
        user_id=identity.user_id,
        spec=spec,
        scope=CHAT_SCOPE,
        mode=ASK_ALL,
    ) == "ask"


# --------------------------------------------------------------------------
# auto_writes — the bypass
# --------------------------------------------------------------------------


def test_auto_writes_executes_a_write_without_parking(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    write = Probe(WRITE, read_only=False)
    install(monkeypatch, write)
    conversation = new_conversation(owner)
    assert set_mode(owner, conversation["id"], AUTO_WRITES).status_code == 200
    run = make_run(db, identity_of(owner), conversation["id"])

    result = run_agent_turn(db, run, evidence=[], model_step=calls_then_answers(WRITE))
    assert result is not None
    assert write.calls == [{}]
    assert only_call(db, run.id).status == "succeeded"


def test_the_audit_names_the_mode_and_never_a_person(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Property 3. A row saying a human reviewed a write nobody looked at is
    worse than no row, because it is a false one."""
    write = Probe(WRITE, read_only=False)
    install(monkeypatch, write)
    identity = identity_of(owner)
    conversation = new_conversation(owner)
    set_mode(owner, conversation["id"], AUTO_WRITES)
    run = make_run(db, identity, conversation["id"])

    run_agent_turn(db, run, evidence=[], model_step=calls_then_answers(WRITE))

    call = only_call(db, run.id)
    assert call.decided_by == "mode:auto_writes"
    assert call.decided_by != identity.user_id
    assert call.decided_at is not None

    trail = [
        json.loads(event.detail_json)
        for event in db.scalars(
            select(AuditEvent).where(
                AuditEvent.workspace_id == identity.workspace_id,
                AuditEvent.resource_id == call.id,
            )
        )
    ]
    assert [detail.get("decided_by") for detail in trail] == ["mode:auto_writes"]


def test_a_standing_allow_is_not_attributed_to_the_mode(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mode is credited only where it changed the answer.

    A tool the policy table already allowed was not let through by the bypass,
    and stamping it as if it were would put a claim on the audit row that
    `tool_policies` contradicts. Over-attribution is a lie in the same family as
    under-attribution.
    """
    write = Probe(WRITE, read_only=False)
    install(monkeypatch, write)
    identity = identity_of(owner)
    grant(db, identity, WRITE, "allow", CHAT_SCOPE)
    conversation = new_conversation(owner)
    set_mode(owner, conversation["id"], AUTO_WRITES)
    run = make_run(db, identity, conversation["id"])

    run_agent_turn(db, run, evidence=[], model_step=calls_then_answers(WRITE))

    call = only_call(db, run.id)
    assert call.status == "succeeded"
    assert not call.decided_by


def test_a_deny_row_still_denies_under_the_bypass(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Property 2, end to end. The tool is never reached."""
    write = Probe(WRITE, read_only=False)
    install(monkeypatch, write)
    identity = identity_of(owner)
    grant(db, identity, WRITE, "deny", CHAT_SCOPE)
    conversation = new_conversation(owner)
    set_mode(owner, conversation["id"], AUTO_WRITES)
    run = make_run(db, identity, conversation["id"])

    assert run_agent_turn(db, run, evidence=[], model_step=calls_then_answers(WRITE)) is not None
    assert write.calls == []
    call = only_call(db, run.id)
    assert call.status == "denied"
    # And the denial is not dressed up as a decision somebody made.
    assert not call.decided_by


def test_no_mode_can_clear_a_deny(owner: TestClient, db: Any) -> None:
    """The same claim as a table, so a fourth mode added later has to face it."""
    identity = identity_of(owner)
    grant(db, identity, WRITE, "deny", CHAT_SCOPE)
    spec = Probe(WRITE, read_only=False).spec()
    for mode in (ASK_WRITES, ASK_ALL, AUTO_WRITES, agent_loop.PLAN):
        assert (
            resolve_policy(
                db,
                workspace_id=identity.workspace_id,
                user_id=identity.user_id,
                spec=spec,
                scope=CHAT_SCOPE,
                mode=mode,
            )
            == "deny"
        )


# --------------------------------------------------------------------------
# Property 1: a chat mode cannot reach workflow scope
# --------------------------------------------------------------------------


def test_the_mode_is_ignored_at_workflow_scope(owner: TestClient, db: Any) -> None:
    """The decision function's half of the lock."""
    identity = identity_of(owner)
    spec = Probe(WRITE, read_only=False).spec()
    assert (
        resolve_policy(
            db,
            workspace_id=identity.workspace_id,
            user_id=identity.user_id,
            spec=spec,
            scope=WORKFLOW_SCOPE,
            mode=AUTO_WRITES,
        )
        == "ask"
    )
    read_spec = Probe(READ, read_only=True).spec()
    assert (
        resolve_policy(
            db,
            workspace_id=identity.workspace_id,
            user_id=identity.user_id,
            spec=read_spec,
            scope=WORKFLOW_SCOPE,
            mode=ASK_ALL,
        )
        == "allow"
    )


def test_a_workflow_backed_run_reads_no_mode_at_all(owner: TestClient, db: Any) -> None:
    """The lookup's half of the lock, on the back door that actually exists.

    The executor gives every workflow run a Conversation of its own to hold the
    transcript. That conversation has an `approval_mode` column like any other,
    so `approval_mode_for_run` must refuse to read it — otherwise setting a
    workflow's own thread to `auto_writes` would buy exactly the standing
    unattended grant the chat|workflow split exists to withhold.
    """
    identity = identity_of(owner)
    conversation = new_conversation(owner, "Workflow: probe")
    assert set_mode(owner, conversation["id"], AUTO_WRITES).status_code == 200
    run = make_run(db, identity, conversation["id"])

    # Before it belongs to a workflow, the mode is honoured.
    assert approval_mode_for_run(db, run, scope=CHAT_SCOPE) == AUTO_WRITES

    workflow = Workflow(
        workspace_id=identity.workspace_id,
        created_by=identity.user_id,
        name="Probe workflow",
        graph_json="{}",
    )
    db.add(workflow)
    db.flush()
    db.add(
        WorkflowRun(
            workspace_id=identity.workspace_id,
            workflow_id=workflow.id,
            created_by=identity.user_id,
            run_id=run.id,
            trigger="schedule",
            status="running",
        )
    )
    db.commit()

    assert agent_loop.policy_scope_for_run(db, run) == WORKFLOW_SCOPE
    assert approval_mode_for_run(db, run, scope=WORKFLOW_SCOPE) == ASK_WRITES


def test_a_bypassed_thread_still_parks_an_unattended_node(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Property 1, composed: the mode is on, the scope is workflow, it parks."""
    write = Probe(WRITE, read_only=False)
    install(monkeypatch, write)
    identity = identity_of(owner)
    conversation = new_conversation(owner, "Workflow: bypass attempt")
    set_mode(owner, conversation["id"], AUTO_WRITES)
    run = make_run(db, identity, conversation["id"])
    workflow = Workflow(
        workspace_id=identity.workspace_id,
        created_by=identity.user_id,
        name="Bypass attempt",
        graph_json="{}",
    )
    db.add(workflow)
    db.flush()
    db.add(
        WorkflowRun(
            workspace_id=identity.workspace_id,
            workflow_id=workflow.id,
            created_by=identity.user_id,
            run_id=run.id,
            trigger="schedule",
            status="running",
        )
    )
    db.commit()

    outcome = run_agent_turn(
        db,
        run,
        evidence=[],
        model_step=calls_then_answers(WRITE),
        # What the executor asserts when an agent node borrows this loop.
        workflow_node=True,
    )
    assert outcome is None
    assert write.calls == []
    assert only_call(db, run.id).status == "proposed"


# --------------------------------------------------------------------------
# The mode belongs to one conversation
# --------------------------------------------------------------------------


def test_the_mode_governs_one_thread_and_not_the_workspace(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bypass switched on for one refactor does not follow you into the next
    conversation — which is the whole reason this is not a workspace setting."""
    write = Probe(WRITE, read_only=False)
    install(monkeypatch, write)
    identity = identity_of(owner)
    bypassed = new_conversation(owner, "Refactor")
    ordinary = new_conversation(owner, "Something else")
    set_mode(owner, bypassed["id"], AUTO_WRITES)

    first = make_run(db, identity, bypassed["id"])
    assert run_agent_turn(db, first, evidence=[], model_step=calls_then_answers(WRITE)) is not None

    second = make_run(db, identity, ordinary["id"])
    assert run_agent_turn(db, second, evidence=[], model_step=calls_then_answers(WRITE)) is None
    assert only_call(db, second.id).status == "proposed"
    assert write.calls == [{}]


def test_switching_back_mid_turn_is_felt_by_the_very_next_call(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bypass is a safety switch, so turning it off cannot wait for the turn.

    The model asks for two writes in one response. Between them — from a second
    session, as the HTTP request a worried user makes really would — the thread
    goes back to `ask_writes`. The first write executes and the second parks.

    What this pins is the *behaviour*, which is the part a user depends on: the
    mode is consulted per tool call and not cached for the turn. It deliberately
    does not try to pin the `db.refresh` in `approval_mode_for_run`, because
    removing that line leaves this test green — SQLAlchemy's identity map holds
    weak references, so the Conversation is collected between calls and re-read
    anyway. That is the accident the refresh exists to stop being load-bearing,
    and a test cannot distinguish the two without a stronger reference than any
    caller currently takes.
    """
    identity = identity_of(owner)
    conversation = new_conversation(owner, "Alarming")
    set_mode(owner, conversation["id"], AUTO_WRITES)

    switched: List[str] = []

    class Switcher(Probe):
        def spec(self) -> ToolSpec:
            inner = super().spec()

            def run(db_: Any, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
                result = inner.executor(db_, context, args)
                if not switched:
                    switched.append("yes")
                    other = SessionLocal()
                    try:
                        row = other.get(Conversation, conversation["id"])
                        assert row is not None
                        row.approval_mode = ASK_WRITES
                        other.commit()
                    finally:
                        other.close()
                return result

            return ToolSpec(
                name=inner.name,
                description=inner.description,
                parameters=inner.parameters,
                executor=run,
                read_only=inner.read_only,
            )

    write = Switcher(WRITE, read_only=False)
    install(monkeypatch, write)
    run = make_run(db, identity, conversation["id"])

    def two_writes(
        input_items: List[Any], tools: List[Dict[str, Any]], instructions: str
    ) -> Iterable[Tuple[str, Any]]:
        return [
            (
                "completed",
                FakeResponse(
                    output=[
                        SimpleNamespace(
                            type="function_call",
                            name=WRITE,
                            call_id=f"probe-{index}",
                            arguments="{}",
                        )
                        for index in (1, 2)
                    ]
                ),
            )
        ]

    assert run_agent_turn(db, run, evidence=[], model_step=two_writes) is None
    assert len(write.calls) == 1, "the second write must not have run"
    statuses = sorted(
        call.status
        for call in db.scalars(select(AgentToolCall).where(AgentToolCall.run_id == run.id))
    )
    assert statuses == ["proposed", "succeeded"]


def test_switching_back_is_felt_by_the_next_call(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mode is read at each decision, not cached for the life of a turn."""
    write = Probe(WRITE, read_only=False)
    install(monkeypatch, write)
    identity = identity_of(owner)
    conversation = new_conversation(owner)
    set_mode(owner, conversation["id"], AUTO_WRITES)
    first = make_run(db, identity, conversation["id"])
    assert run_agent_turn(db, first, evidence=[], model_step=calls_then_answers(WRITE)) is not None

    assert set_mode(owner, conversation["id"], ASK_WRITES).status_code == 200
    db.expire_all()
    second = make_run(db, identity, conversation["id"])
    assert run_agent_turn(db, second, evidence=[], model_step=calls_then_answers(WRITE)) is None
    assert only_call(db, second.id).status == "proposed"


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------


def test_setting_the_mode_is_audited(owner: TestClient, db: Any) -> None:
    """Property 4. Turning the approval park off is the most consequential
    switch in the product; "who turned it off, and when" must have an answer."""
    identity = identity_of(owner)
    conversation = new_conversation(owner)
    assert set_mode(owner, conversation["id"], AUTO_WRITES).json()["approval_mode"] == (
        AUTO_WRITES
    )
    assert set_mode(owner, conversation["id"], ASK_WRITES).status_code == 200

    rows = [
        row
        for row in owner.get("/api/audit-events").json()
        if row["action"] == "conversation.approval_mode_set"
        and row["resource_id"] == conversation["id"]
    ]
    assert [(row["detail"]["from"], row["detail"]["to"]) for row in rows] == [
        (AUTO_WRITES, ASK_WRITES),
        (ASK_WRITES, AUTO_WRITES),
    ]
    assert all(row["resource_type"] == "conversation" for row in rows)
    db.expire_all()
    stored = db.get(Conversation, conversation["id"])
    assert stored is not None and stored.workspace_id == identity.workspace_id


def test_an_unknown_mode_is_refused(owner: TestClient) -> None:
    conversation = new_conversation(owner)
    assert set_mode(owner, conversation["id"], "yolo").status_code == 422
    assert set_mode(owner, conversation["id"], "").status_code == 422
    assert owner.get("/api/conversations").json()[0]["approval_mode"] == ASK_WRITES


def test_the_mode_cannot_be_set_on_a_thread_from_another_workspace(
    owner: TestClient, identity_client: Callable[..., TestClient]
) -> None:
    stranger = identity_client(name="Stranger", workspace_name="Stranger workspace")
    conversation = new_conversation(owner)

    assert set_mode(stranger, conversation["id"], AUTO_WRITES).status_code == 404
    assert set_mode(owner, "no-such-conversation", AUTO_WRITES).status_code == 404
    assert owner.get("/api/conversations").json()[0]["approval_mode"] == ASK_WRITES


def test_the_verdict_records_which_mode_decided(owner: TestClient, db: Any) -> None:
    """`Verdict.by_mode` is what `decided_by` is stamped from, asserted directly
    so a change to the stamping cannot quietly change the attribution."""
    identity = identity_of(owner)
    write_spec = Probe(WRITE, read_only=False).spec()
    read_spec = Probe(READ, read_only=True).spec()

    bypassed = evaluate_policy(
        db,
        workspace_id=identity.workspace_id,
        user_id=identity.user_id,
        spec=write_spec,
        scope=CHAT_SCOPE,
        mode=AUTO_WRITES,
    )
    assert (bypassed.policy, bypassed.by_mode) == ("allow", AUTO_WRITES)

    # A read-only tool under the bypass was already allowed; the mode did nothing.
    untouched = evaluate_policy(
        db,
        workspace_id=identity.workspace_id,
        user_id=identity.user_id,
        spec=read_spec,
        scope=CHAT_SCOPE,
        mode=AUTO_WRITES,
    )
    assert (untouched.policy, untouched.by_mode) == ("allow", "")

    # A park is decided by whoever answers the card, so nothing is claimed here.
    parked = evaluate_policy(
        db,
        workspace_id=identity.workspace_id,
        user_id=identity.user_id,
        spec=read_spec,
        scope=CHAT_SCOPE,
        mode=ASK_ALL,
    )
    assert (parked.policy, parked.by_mode) == ("ask", "")


def test_the_conversation_can_read_back_what_the_bypass_approved(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Property 3, as the *chat* can see it.

    A trail that only exists in `audit_events` is a trail a chat user never
    reads, so `GET /api/agent-tool-calls` names the mode that decided a call.
    The mode, and never `decided_by` itself: the column holds a user id in the
    ordinary case, and no route on this API puts one on the wire.
    """
    write = Probe(WRITE, read_only=False)
    read = Probe(READ, read_only=True)
    install(monkeypatch, write, read)
    identity = identity_of(owner)
    conversation = new_conversation(owner)
    set_mode(owner, conversation["id"], AUTO_WRITES)

    bypassed = make_run(db, identity, conversation["id"])
    run_agent_turn(db, bypassed, evidence=[], model_step=calls_then_answers(WRITE))
    unattended = make_run(db, identity, conversation["id"])
    run_agent_turn(db, unattended, evidence=[], model_step=calls_then_answers(READ))

    reported = {
        row["name"]: row for row in owner.get("/api/agent-tool-calls").json()
    }
    assert reported[WRITE]["approved_by_mode"] == AUTO_WRITES
    # The read-only tool was never going to park, so the mode did not decide it,
    # and a blank here is what a stamped row is distinguishable from.
    assert reported[READ]["approved_by_mode"] == ""
    body = owner.get("/api/agent-tool-calls").text
    assert identity.user_id not in body
    assert "decided_by" not in body


def test_the_stream_says_a_call_was_bypassed_while_it_is_happening(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Property 3 again, but at the moment it is worth knowing.

    `GET /api/agent-tool-calls` is a refetch, and the client only makes it once
    the run settles. So a trail that lives only there is blank for the whole of
    the turn in which the writes are actually going through — the only window in
    which turning the bypass off could still stop anything — and a run that
    parks on a later approval or on the budget ceiling never leaves that window
    at all. The events are what the chat has during the turn, so they carry it.
    """
    write = Probe(WRITE, read_only=False)
    read = Probe(READ, read_only=True)
    install(monkeypatch, write, read)
    conversation = new_conversation(owner)
    set_mode(owner, conversation["id"], AUTO_WRITES)

    identity = identity_of(owner)
    bypassed = make_run(db, identity, conversation["id"])
    run_agent_turn(db, bypassed, evidence=[], model_step=calls_then_answers(WRITE))
    unattended = make_run(db, identity, conversation["id"])
    run_agent_turn(db, unattended, evidence=[], model_step=calls_then_answers(READ))

    stamped: Dict[Tuple[str, str], str] = {}
    for event in db.scalars(
        select(RunEvent)
        .where(RunEvent.run_id.in_([bypassed.id, unattended.id]))
        .order_by(RunEvent.sequence)
    ):
        if event.event_type not in ("tool.started", "tool.completed"):
            continue
        payload = json.loads(event.payload_json)
        stamped[(payload["tool_name"], event.event_type)] = payload["approved_by_mode"]

    # Both events, because a client that reloaded mid-run has only the ones it
    # received and the completed row is the one it keeps.
    assert stamped[(WRITE, "tool.started")] == AUTO_WRITES
    assert stamped[(WRITE, "tool.completed")] == AUTO_WRITES
    # And a read the mode never had to decide is blank, so "unreviewed" counts
    # the calls the bypass actually let through rather than every call in a
    # bypassed thread.
    assert stamped[(READ, "tool.started")] == ""
    assert stamped[(READ, "tool.completed")] == ""
