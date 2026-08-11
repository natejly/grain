from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.models import AgentToolCall, Run, ToolPolicy
from app.services.agent_loop import run_agent_turn


class FakeResponse:
    def __init__(self, output=None, output_text=""):
        self.output = output or []
        self.output_text = output_text


def _park_run(client) -> tuple[str, str]:
    """Drive a run until it parks on an approval; return (run_id, tool_call_id)."""
    identity = client.get("/api/bootstrap").json()["identity"]
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "approval-conv-" + os.urandom(6).hex()},
        json={"title": "Approvals"},
    ).json()
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=identity["workspace_id"],
            conversation_id=conversation["id"],
            agent_id=client.get("/api/bootstrap").json()["default_agent_id"],
            created_by=identity["user_id"],
            status="running",
            prompt="List my datasets",
        )
        db.add(run)
        db.add(
            ToolPolicy(
                workspace_id=identity["workspace_id"],
                tool_name="list_datasets",
                policy="ask",
            )
        )
        db.commit()
        run_id = run.id

        def model_step(input_items, tools, instructions):
            return [
                (
                    "completed",
                    FakeResponse(
                        output=[
                            SimpleNamespace(
                                type="function_call",
                                name="list_datasets",
                                call_id="http-1",
                                arguments="{}",
                            )
                        ]
                    ),
                )
            ]

        assert run_agent_turn(db, run, evidence=[], model_step=model_step) is None
        call = (
            db.query(AgentToolCall)
            .filter(AgentToolCall.run_id == run_id, AgentToolCall.status == "proposed")
            .one()
        )
        return run_id, call.id
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _no_resume(monkeypatch):
    """Capture the resume hand-off instead of running a model-backed turn."""
    calls = []
    monkeypatch.setattr(
        "app.api.tools.resume_run",
        lambda run_id, tool_call_id, decision: calls.append(
            (run_id, tool_call_id, decision)
        ),
    )
    yield calls
    db = SessionLocal()
    try:
        db.query(ToolPolicy).delete()
        db.commit()
    finally:
        db.close()


def test_approve_resumes_the_run_and_remembers_the_policy(client, _no_resume):
    run_id, call_id = _park_run(client)
    response = client.post(
        f"/api/agent-tool-calls/{call_id}/decision",
        headers={"Idempotency-Key": "decide-" + os.urandom(6).hex()},
        json={"decision": "approved", "remember": True},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert _no_resume == [(run_id, call_id, "approved")]

    db = SessionLocal()
    try:
        policy = (
            db.query(ToolPolicy)
            .filter(ToolPolicy.tool_name == "list_datasets")
            .one()
        )
        assert policy.policy == "allow"
    finally:
        db.close()


def test_deny_without_remember_leaves_the_policy_asking(client, _no_resume):
    run_id, call_id = _park_run(client)
    response = client.post(
        f"/api/agent-tool-calls/{call_id}/decision",
        headers={"Idempotency-Key": "decide-" + os.urandom(6).hex()},
        json={"decision": "denied"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "denied"
    # Denials resume too, so the model can answer around the refusal.
    assert _no_resume == [(run_id, call_id, "denied")]

    db = SessionLocal()
    try:
        policy = (
            db.query(ToolPolicy)
            .filter(ToolPolicy.tool_name == "list_datasets")
            .one()
        )
        assert policy.policy == "ask"
    finally:
        db.close()


def test_second_decision_conflicts(client, _no_resume):
    _run_id, call_id = _park_run(client)
    first = client.post(
        f"/api/agent-tool-calls/{call_id}/decision",
        headers={"Idempotency-Key": "decide-" + os.urandom(6).hex()},
        json={"decision": "approved"},
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/agent-tool-calls/{call_id}/decision",
        headers={"Idempotency-Key": "decide-" + os.urandom(6).hex()},
        json={"decision": "denied"},
    )
    assert second.status_code == 409


def test_replayed_idempotency_key_does_not_resume_twice(client, _no_resume):
    run_id, call_id = _park_run(client)
    key = "decide-" + os.urandom(6).hex()
    body = {"decision": "approved"}
    first = client.post(
        f"/api/agent-tool-calls/{call_id}/decision", headers={"Idempotency-Key": key}, json=body
    )
    second = client.post(
        f"/api/agent-tool-calls/{call_id}/decision", headers={"Idempotency-Key": key}, json=body
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert _no_resume == [(run_id, call_id, "approved")]


def test_unknown_tool_call_is_not_found(client, _no_resume):
    response = client.post(
        "/api/agent-tool-calls/does-not-exist/decision",
        headers={"Idempotency-Key": "decide-" + os.urandom(6).hex()},
        json={"decision": "approved"},
    )
    assert response.status_code == 404


def test_tool_policy_endpoints_round_trip(client):
    response = client.put(
        "/api/tool-policies", json={"tool_name": "query_dataset", "policy": "ask"}
    )
    assert response.status_code == 200
    listed = client.get("/api/tool-policies").json()
    assert {"tool_name": "query_dataset", "policy": "ask"} in [
        {"tool_name": row["tool_name"], "policy": row["policy"]} for row in listed
    ]
    # Upsert rather than duplicate.
    client.put("/api/tool-policies", json={"tool_name": "query_dataset", "policy": "deny"})
    listed = client.get("/api/tool-policies").json()
    matches = [row for row in listed if row["tool_name"] == "query_dataset"]
    assert len(matches) == 1
    assert matches[0]["policy"] == "deny"

    db = SessionLocal()
    try:
        db.query(ToolPolicy).delete()
        db.commit()
    finally:
        db.close()


def test_agent_tool_calls_are_listable(client, _no_resume):
    run_id, call_id = _park_run(client)
    rows = client.get("/api/agent-tool-calls").json()
    match = [row for row in rows if row["id"] == call_id]
    assert match and match[0]["run_id"] == run_id
    assert json.loads(match[0]["arguments_json"]) == {}


def test_the_decision_claim_is_refused_once_another_transaction_holds_it(
    client, _no_resume
):
    """The route's gate, exercised as the loser of a race sees it.

    `decide_agent_tool_call` no longer reads `status == 'proposed'` and then
    assigns; it claims the row with `UPDATE ... WHERE status = 'proposed'` and
    lets the rowcount decide, because two reviewers who both read an open card
    both pass an `if` and both go on to schedule `resume_run`. Only the claim
    itself can be tested here — SQLite serialises the two HTTP handlers, so
    driving both through the client would prove nothing about the gate.
    """
    from app.api.tools import _claim_decision

    _run_id, call_id = _park_run(client)
    identity = client.get("/api/bootstrap").json()["identity"]
    db = SessionLocal()
    try:
        assert _claim_decision(
            db,
            AgentToolCall,
            call_id=call_id,
            workspace_id=identity["workspace_id"],
            decision="denied",
            actor_id=identity["user_id"],
        )
        db.commit()
        # The same claim from a reviewer who read the card while it was open.
        assert not _claim_decision(
            db,
            AgentToolCall,
            call_id=call_id,
            workspace_id=identity["workspace_id"],
            decision="approved",
            actor_id=identity["user_id"],
        )
        db.commit()
        assert db.get(AgentToolCall, call_id).status == "denied"
    finally:
        db.close()
