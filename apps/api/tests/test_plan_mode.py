"""Plan mode: the thread researches, proposes, and only acts once the plan is
approved.

Plan mode is the fourth `ApprovalMode`, and it is not another point on the
ask/allow ladder — it changes what the turn *is*. The properties that matter:

1. a planning turn is never offered a write-capable tool — the registry is
   narrowed, so the model plans around what it can see instead of burning
   iterations on refusals — and `evaluate_policy` denies a write anyway as the
   second, independent lock;
2. the exit is an approval card whose content is the plan itself: a standing
   allow cannot pre-answer it, "always allow" is not remembered for it, and
   approving it restores `ask_writes` *before* the resume so the same turn can
   implement under the full registry;
3. a denial changes nothing — the thread stays in plan mode and the model
   revises;
4. plan mode composes with the rest of the mode system the way the other modes
   do: ignored at workflow scope, but — alone among the modes — it outranks the
   development bypass, because a developer who switched a thread into plan mode
   is exercising plan mode itself.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import pytest
from conftest import Identity, ask_before_writes
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from app.config import Settings
from app.database import SessionLocal
from app.models import Agent, AgentToolCall, Conversation, Membership, Run, ToolPolicy
from app.services import agent_loop
from app.services.agent_loop import (
    ASK_WRITES,
    AUTO_WRITES,
    CHAT_SCOPE,
    PLAN,
    PLAN_MODE_INSTRUCTIONS,
    WORKFLOW_SCOPE,
    approval_mode_for_run,
    evaluate_policy,
    resume_agent_turn,
    run_agent_turn,
)
from app.services.llm_tools import (
    EXIT_PLAN_MODE,
    ToolContext,
    ToolResult,
    ToolSpec,
    exit_plan_mode_spec,
)

# --------------------------------------------------------------------------
# Harness (the shape test_approval_mode.py uses, for the same reasons)
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, output: Optional[List[Any]] = None, output_text: str = ""):
        self.output = output or []
        self.output_text = output_text


class Probe:
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


def _call(name: str, arguments: str = "{}", call_id: str = "probe-1") -> Any:
    return SimpleNamespace(
        type="function_call", name=name, call_id=call_id, arguments=arguments
    )


def seeing_steps(
    seen: List[Dict[str, Any]], first_output: Optional[List[Any]] = None
) -> Callable[[List[Any], List[Dict[str, Any]], str], Iterable[Tuple[str, Any]]]:
    """A model that records what each step was offered, calls `first_output`
    once if given, then answers."""
    steps = {"n": 0}

    def step(
        input_items: List[Any], tools: List[Dict[str, Any]], instructions: str
    ) -> Iterable[Tuple[str, Any]]:
        steps["n"] += 1
        seen.append(
            {
                "tools": sorted(
                    tool["name"] for tool in tools if tool.get("type") == "function"
                ),
                "instructions": instructions,
            }
        )
        if steps["n"] == 1 and first_output is not None:
            return [("completed", FakeResponse(output=first_output))]
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


def new_conversation(client: TestClient, title: str = "Planning") -> Dict[str, Any]:
    response = client.post(
        "/api/conversations",
        headers=_key(),
        json={"title": title},
    )
    assert response.status_code == 201, response.text
    payload: Dict[str, Any] = response.json()
    # Plan mode is defined against the mode a thread was in before it, and this
    # module's setups queue a call under "asks before writes" and then switch.
    # Pinned here rather than left to the default, which is agentic since 0064.
    ask_before_writes(client, payload["id"])
    payload["approval_mode"] = "ask_writes"
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


def _key() -> Dict[str, str]:
    return {"Idempotency-Key": "plan-" + os.urandom(6).hex()}


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        model_provider="openai",
        openai_api_key=SecretStr("test-key"),
        **overrides,
    )


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def owner(identity_client: Callable[..., TestClient]) -> TestClient:
    return identity_client(name="Plan owner", workspace_name="Plan workspace")


def identity_of(client: TestClient) -> Identity:
    return client.identity  # type: ignore[attr-defined,no-any-return]


WRITE = "probe_write"
READ = "probe_read"


# --------------------------------------------------------------------------
# Property 1: a planning turn cannot write
# --------------------------------------------------------------------------


def test_plan_offers_only_reads_and_the_exit(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry lock: writes are absent, not present-and-denied, and the
    exit gesture is spliced in. The instructions carry the plan block."""
    install(monkeypatch, Probe(WRITE, read_only=False), Probe(READ, read_only=True))
    conversation = new_conversation(owner)
    assert set_mode(owner, conversation["id"], PLAN).status_code == 200
    run = make_run(db, identity_of(owner), conversation["id"])

    seen: List[Dict[str, Any]] = []
    assert run_agent_turn(db, run, evidence=[], model_step=seeing_steps(seen)) is not None
    assert seen[0]["tools"] == [EXIT_PLAN_MODE, READ]
    assert PLAN_MODE_INSTRUCTIONS in seen[0]["instructions"]


def test_plan_runs_a_read_without_parking(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Research stays fluent: plan mode is not ask_all."""
    read = Probe(READ, read_only=True)
    install(monkeypatch, read)
    conversation = new_conversation(owner)
    set_mode(owner, conversation["id"], PLAN)
    run = make_run(db, identity_of(owner), conversation["id"])

    seen: List[Dict[str, Any]] = []
    result = run_agent_turn(
        db, run, evidence=[], model_step=seeing_steps(seen, first_output=[_call(READ)])
    )
    assert result is not None
    assert read.calls == [{}]
    assert only_call(db, run.id).status == "succeeded"


def test_plan_refuses_a_write_at_the_policy_too(owner: TestClient, db: Any) -> None:
    """The second lock, asserted at the decision point: even when a write spec
    somehow reaches `evaluate_policy` under plan mode, the verdict is deny —
    and it is not dressed up as a mode-approved anything."""
    identity = identity_of(owner)
    write_spec = Probe(WRITE, read_only=False).spec()
    read_spec = Probe(READ, read_only=True).spec()

    denied = evaluate_policy(
        db,
        workspace_id=identity.workspace_id,
        user_id=identity.user_id,
        spec=write_spec,
        scope=CHAT_SCOPE,
        mode=PLAN,
    )
    assert (denied.policy, denied.by_mode) == ("deny", "")
    assert (
        evaluate_policy(
            db,
            workspace_id=identity.workspace_id,
            user_id=identity.user_id,
            spec=read_spec,
            scope=CHAT_SCOPE,
            mode=PLAN,
        ).policy
        == "allow"
    )
    assert (
        evaluate_policy(
            db,
            workspace_id=identity.workspace_id,
            user_id=identity.user_id,
            spec=exit_plan_mode_spec(),
            scope=CHAT_SCOPE,
            mode=PLAN,
        ).policy
        == "ask"
    )


def test_a_write_queued_before_the_switch_never_runs(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one road a write call has into a plan-mode turn: queued under an
    earlier mode, with the conversation switched to plan before the resume.
    Nothing executes — not the pending sibling, and not even the call a human
    approved before the switch. That is the same safety-switch rule the other
    modes already obey ("switching back mid-turn is felt by the very next
    call"): entering plan mode means "stop acting", and a switch that waited
    for decisions already in flight would not be a switch. Which lock catches
    each call is deliberately not pinned; that no write executes is the
    property."""
    write = Probe(WRITE, read_only=False)
    install(monkeypatch, write)
    identity = identity_of(owner)
    conversation = new_conversation(owner)
    run = make_run(db, identity, conversation["id"])

    def two_writes(
        input_items: List[Any], tools: List[Dict[str, Any]], instructions: str
    ) -> Iterable[Tuple[str, Any]]:
        return [
            (
                "completed",
                FakeResponse(
                    output=[_call(WRITE, call_id="w-1"), _call(WRITE, call_id="w-2")]
                ),
            )
        ]

    assert run_agent_turn(db, run, evidence=[], model_step=two_writes) is None
    parked = only_call(db, run.id)
    assert parked.status == "proposed"

    assert set_mode(owner, conversation["id"], PLAN).status_code == 200
    db.expire_all()
    run_row = db.get(Run, run.id)
    assert run_row is not None
    seen: List[Dict[str, Any]] = []
    result = resume_agent_turn(
        db,
        run_row,
        tool_call_id=parked.id,
        decision="approved",
        model_step=seeing_steps(seen),
    )
    assert result is not None
    assert write.calls == [], "no write may run once the thread is planning"


# --------------------------------------------------------------------------
# Property 2: the exit is the plan review
# --------------------------------------------------------------------------


PLAN_TEXT = "1. Rename the column.\n2. Backfill.\n3. Drop the alias."


def _park_on_exit(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> Tuple[Dict[str, Any], Run, AgentToolCall]:
    install(monkeypatch, Probe(WRITE, read_only=False), Probe(READ, read_only=True))
    conversation = new_conversation(owner)
    assert set_mode(owner, conversation["id"], PLAN).status_code == 200
    run = make_run(db, identity_of(owner), conversation["id"])
    outcome = run_agent_turn(
        db,
        run,
        evidence=[],
        model_step=seeing_steps(
            [], first_output=[_call(EXIT_PLAN_MODE, json.dumps({"plan": PLAN_TEXT}))]
        ),
    )
    assert outcome is None, "the exit gesture must park"
    call = only_call(db, run.id)
    assert call.status == "proposed"
    return conversation, run, call


def test_the_exit_parks_with_the_plan_as_the_card(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the reviewer reads on the approval card is the plan itself."""
    _conversation, _run, call = _park_on_exit(owner, db, monkeypatch)
    assert call.name == EXIT_PLAN_MODE
    assert call.proposal_preview == PLAN_TEXT


def test_a_standing_allow_cannot_pre_answer_the_plan_review(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approving the card IS approving the plan, so no remembered grant may
    skip it."""
    identity = identity_of(owner)
    db.add(
        ToolPolicy(
            workspace_id=identity.workspace_id,
            tool_name=EXIT_PLAN_MODE,
            policy="allow",
            scope=CHAT_SCOPE,
            created_by=identity.user_id,
        )
    )
    db.commit()
    _conversation, _run, call = _park_on_exit(owner, db, monkeypatch)
    assert call.status == "proposed"


def test_approving_the_plan_lifts_the_mode_and_the_turn_implements(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole handshake. The decision endpoint restores the approver's own
    default before the resume — audited, attributed to the exit — so the resumed
    turn re-enters under the full registry with the plan block gone, and "always
    allow" on the card is deliberately not remembered as a policy row.

    "Their default" and not a fixed `ask_writes`: leaving plan mode lands where
    this member's threads live, which since 0064 is `auto_writes` unless they
    turned Safe mode on. What has not changed is that it never restores
    *whatever preceded plan* — see
    `test_leaving_plan_mode_lands_on_safe_mode_for_a_member_who_asked_for_it`
    for the other side of it."""
    conversation, run, call = _park_on_exit(owner, db, monkeypatch)
    identity = identity_of(owner)

    scheduled: List[Any] = []
    monkeypatch.setattr(
        "app.api.tools.resume_run", lambda *args, **kwargs: scheduled.append(args)
    )
    response = owner.post(
        f"/api/agent-tool-calls/{call.id}/decision",
        headers=_key(),
        json={"decision": "approved", "remember": True},
    )
    assert response.status_code == 200, response.text
    assert scheduled, "the endpoint must still schedule the resume"

    db.expire_all()
    stored = db.get(Conversation, conversation["id"])
    assert stored is not None and stored.approval_mode == AUTO_WRITES
    lifted = [
        row
        for row in owner.get("/api/audit-events").json()
        if row["action"] == "conversation.approval_mode_set"
        and row["resource_id"] == conversation["id"]
        and row["detail"].get("via") == EXIT_PLAN_MODE
    ]
    assert [(row["detail"]["from"], row["detail"]["to"]) for row in lifted] == [
        (PLAN, AUTO_WRITES)
    ]
    assert (
        db.scalar(
            select(ToolPolicy).where(
                ToolPolicy.workspace_id == identity.workspace_id,
                ToolPolicy.tool_name == EXIT_PLAN_MODE,
            )
        )
        is None
    ), "remember must not create a standing grant for the exit gesture"

    # Now the resume the endpoint scheduled, run for real: the parked call
    # executes even though the full registry no longer carries its spec, and the
    # step after it is offered the write tools again, without the plan block.
    run_row = db.get(Run, run.id)
    assert run_row is not None
    seen: List[Dict[str, Any]] = []
    result = resume_agent_turn(
        db,
        run_row,
        tool_call_id=call.id,
        decision="approved",
        model_step=seeing_steps(seen),
    )
    assert result is not None
    assert WRITE in seen[0]["tools"]
    assert PLAN_MODE_INSTRUCTIONS not in seen[0]["instructions"]
    db.expire_all()
    executed = only_call(db, run.id)
    assert executed.status == "succeeded"
    assert "approved" in executed.result_preview


def test_leaving_plan_mode_lands_on_safe_mode_for_a_member_who_asked_for_it(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the exit: a Safe-mode member lands back on `ask_writes`.

    Both halves of the rule are load-bearing and they pull opposite ways. A
    member who never asked to be asked must not find themselves being asked
    because they once used plan mode — that is the test above. A member who DID
    ask must not have plan mode be the thing that quietly turns it off. One
    lookup answers both, which is why the exit reads the member's default
    rather than either constant.
    """
    identity = identity_of(owner)
    membership = db.scalar(
        select(Membership).where(
            Membership.workspace_id == identity.workspace_id,
            Membership.user_id == identity.user_id,
        )
    )
    assert membership is not None
    membership.safe_mode = True
    db.commit()
    try:
        conversation, _run, call = _park_on_exit(owner, db, monkeypatch)
        monkeypatch.setattr(
            "app.api.tools.resume_run", lambda *args, **kwargs: None
        )
        response = owner.post(
            f"/api/agent-tool-calls/{call.id}/decision",
            headers=_key(),
            json={"decision": "approved"},
        )
        assert response.status_code == 200, response.text
        db.expire_all()
        stored = db.get(Conversation, conversation["id"])
        assert stored is not None and stored.approval_mode == ASK_WRITES
    finally:
        # The owner client is session-scoped; leaving it opted in would make
        # every later test in this process run as a different kind of member.
        db.expire_all()
        restored = db.get(Membership, membership.id)
        assert restored is not None
        restored.safe_mode = False
        db.commit()


def test_denying_the_plan_stays_in_plan_mode(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Property 3: a denial changes nothing — the resumed turn is still a
    planning turn, and the model is told to revise rather than the run dying."""
    conversation, run, call = _park_on_exit(owner, db, monkeypatch)

    db.expire_all()
    run_row = db.get(Run, run.id)
    assert run_row is not None
    seen: List[Dict[str, Any]] = []
    result = resume_agent_turn(
        db,
        run_row,
        tool_call_id=call.id,
        decision="denied",
        model_step=seeing_steps(seen),
    )
    assert result is not None
    assert seen[0]["tools"] == [EXIT_PLAN_MODE, READ]
    assert PLAN_MODE_INSTRUCTIONS in seen[0]["instructions"]
    db.expire_all()
    assert only_call(db, run.id).status == "denied"
    stored = db.get(Conversation, conversation["id"])
    assert stored is not None and stored.approval_mode == PLAN


# --------------------------------------------------------------------------
# Property 4: composition with the rest of the mode system
# --------------------------------------------------------------------------


def test_plan_is_read_back_for_a_chat_run(owner: TestClient, db: Any) -> None:
    conversation = new_conversation(owner)
    set_mode(owner, conversation["id"], PLAN)
    run = make_run(db, identity_of(owner), conversation["id"])
    assert approval_mode_for_run(db, run, scope=CHAT_SCOPE) == PLAN


def test_plan_is_ignored_at_workflow_scope(owner: TestClient, db: Any) -> None:
    """Same lock as every other mode: nothing set while typing governs an
    unattended run."""
    conversation = new_conversation(owner)
    set_mode(owner, conversation["id"], PLAN)
    run = make_run(db, identity_of(owner), conversation["id"])
    assert approval_mode_for_run(db, run, scope=WORKFLOW_SCOPE) == ASK_WRITES


def test_plan_outranks_the_development_bypass(
    owner: TestClient, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alone among the modes. The bypass skips approval friction; a thread
    switched into plan mode is exercising plan mode itself, and a flag that
    quietly turned it back into auto_writes would make the feature untestable
    in the only environment the flag can be on."""
    monkeypatch.setattr(
        agent_loop,
        "get_settings",
        lambda: _settings(app_env="development", dev_unrestricted_agent=True),
    )
    identity = identity_of(owner)
    planned = new_conversation(owner, "Planned")
    set_mode(owner, planned["id"], PLAN)
    assert (
        approval_mode_for_run(db, make_run(db, identity, planned["id"]), scope=CHAT_SCOPE)
        == PLAN
    )
    # The control: any other thread still resolves to the bypass.
    ordinary = new_conversation(owner, "Ordinary")
    assert (
        approval_mode_for_run(
            db, make_run(db, identity, ordinary["id"]), scope=CHAT_SCOPE
        )
        == AUTO_WRITES
    )
