"""The `guardian` approval mode: reviewer-triaged writes, fail-closed.

The reviewer itself (services/guardian.py) has its own unit suite; these tests
wire a fake reviewer into the loop and prove the mode's five guards — chat
scope only, never force_ask, never exit_plan_mode, per-turn cap, defer parks —
plus the attribution trail a bypassed write must leave.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List

from app.database import SessionLocal
from app.models import AgentToolCall, AuditEvent, Conversation, Document, Run, ToolPolicy
from app.services import guardian
from app.services.agent_loop import run_agent_turn


class FakeResponse:
    def __init__(self, output=None, output_text=""):
        self.output = output or []
        self.output_text = output_text


def _completed(output=None, output_text=""):
    return [("completed", FakeResponse(output=output, output_text=output_text))]


def _function_call(name: str, arguments: Dict[str, Any], call_id: str = "call-1"):
    return SimpleNamespace(
        type="function_call",
        name=name,
        call_id=call_id,
        arguments=json.dumps(arguments),
    )


def _make_run(client, prompt: str) -> str:
    identity = client.get("/api/bootstrap").json()["identity"]
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "guardian-conv-" + prompt[:16]},
        json={"title": "Guardian"},
    ).json()
    db = SessionLocal()
    try:
        conv = db.get(Conversation, conversation["id"])
        conv.approval_mode = "guardian"
        run = Run(
            workspace_id=identity["workspace_id"],
            conversation_id=conversation["id"],
            agent_id=client.get("/api/bootstrap").json()["default_agent_id"],
            created_by=identity["user_id"],
            status="running",
            prompt=prompt,
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _cleanup(run_id: str) -> None:
    db = SessionLocal()
    try:
        db.query(ToolPolicy).delete()
        titles = ("Guardian doc", "G1", "G2", "G3", "G4", "G5", "G6")
        db.query(Document).filter(Document.title.in_(titles)).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


def _write_step(calls: List[SimpleNamespace], answer: str = "Wrote it."):
    def model_step(input_items, tools, instructions):
        if not any(
            isinstance(item, dict) and item.get("type") == "function_call_output"
            for item in input_items
        ):
            return _completed(output=calls)
        return _completed(output_text=answer)

    return model_step


def test_a_guardian_approval_executes_the_write_with_honest_attribution(
    client, monkeypatch
):
    run_id = _make_run(client, "Guardian: write the doc.")
    reviews: List[str] = []

    def fake_review(*, name, arguments_json, preview, prompt, settings):
        reviews.append(name)
        return guardian.GuardianVerdict(True, "routine and clearly asked for")

    monkeypatch.setattr(guardian, "review", fake_review)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        step = _write_step(
            [_function_call("create_document", {"title": "Guardian doc", "content": "x"})]
        )
        result = run_agent_turn(db, run, evidence=[], model_step=step)
        assert result is not None and result.answer == "Wrote it."
        assert reviews == ["create_document"]
        record = db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).one()
        assert record.status == "succeeded"
        assert record.decided_by == "mode:guardian"
        assert record.approved_by_mode == "guardian"
        audit = (
            db.query(AuditEvent)
            .filter(
                AuditEvent.action == "agent_tool.guardian_approved",
                AuditEvent.resource_id == run_id,
            )
            .one()
        )
        assert json.loads(audit.detail_json)["tool"] == "create_document"
    finally:
        _cleanup(run_id)
        db.close()


def test_a_guardian_defer_parks_exactly_like_ask_writes(client, monkeypatch):
    run_id = _make_run(client, "Guardian: risky write.")
    monkeypatch.setattr(
        guardian,
        "review",
        lambda **kwargs: guardian.GuardianVerdict(False, "surprising"),
    )
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        step = _write_step(
            [_function_call("create_document", {"title": "Guardian doc", "content": "x"})]
        )
        assert run_agent_turn(db, run, evidence=[], model_step=step) is None
        db.refresh(run)
        assert run.status == "waiting_for_approval"
        record = db.query(AgentToolCall).filter(AgentToolCall.run_id == run_id).one()
        assert record.status == "proposed"
        assert record.decided_by is None or record.decided_by == ""
    finally:
        _cleanup(run_id)
        db.close()


def test_the_guardian_is_never_consulted_for_a_force_ask_tool(client, monkeypatch):
    run_id = _make_run(client, "Guardian: ask the user something.")

    def must_not_be_called(**kwargs):
        raise AssertionError("guardian.review must not see a force_ask tool")

    monkeypatch.setattr(guardian, "review", must_not_be_called)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        step = _write_step([_function_call("ask_user", {"question": "Which env?"})])
        assert run_agent_turn(db, run, evidence=[], model_step=step) is None
        db.refresh(run)
        assert run.status == "waiting_for_approval"
    finally:
        _cleanup(run_id)
        db.close()


def test_a_deny_policy_still_denies_without_consulting_the_guardian(
    client, monkeypatch
):
    run_id = _make_run(client, "Guardian: denied tool.")

    def must_not_be_called(**kwargs):
        raise AssertionError("guardian.review must not see a denied tool")

    monkeypatch.setattr(guardian, "review", must_not_be_called)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        db.add(
            ToolPolicy(
                workspace_id=run.workspace_id,
                tool_name="create_document",
                policy="deny",
            )
        )
        db.commit()
        seen: List[str] = []

        def model_step(input_items, tools, instructions):
            outputs = [
                item
                for item in input_items
                if isinstance(item, dict) and item.get("type") == "function_call_output"
            ]
            if not outputs:
                return _completed(
                    output=[
                        _function_call(
                            "create_document", {"title": "Guardian doc", "content": "x"}
                        )
                    ]
                )
            seen.extend(str(item.get("output")) for item in outputs)
            return _completed(output_text="Understood, not doing that.")

        result = run_agent_turn(db, run, evidence=[], model_step=model_step)
        assert result is not None
        assert any("denied this tool call" in output for output in seen)
    finally:
        _cleanup(run_id)
        db.close()


def test_the_per_turn_cap_sends_the_sixth_write_to_a_person(client, monkeypatch):
    assert guardian.MAX_GUARDIAN_APPROVALS_PER_TURN == 5
    run_id = _make_run(client, "Guardian: six writes.")
    reviews: List[str] = []

    def fake_review(*, name, arguments_json, preview, prompt, settings):
        reviews.append(json.loads(arguments_json)["title"])
        return guardian.GuardianVerdict(True, "fine")

    monkeypatch.setattr(guardian, "review", fake_review)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        calls = [
            _function_call(
                "create_document", {"title": f"G{n}", "content": "x"}, f"g-{n}"
            )
            for n in range(1, 7)
        ]
        step = _write_step(calls)
        # The sixth call parks, so the turn does not finish.
        assert run_agent_turn(db, run, evidence=[], model_step=step) is None
        assert reviews == ["G1", "G2", "G3", "G4", "G5"]
        rows = (
            db.query(AgentToolCall)
            .filter(AgentToolCall.run_id == run_id)
            .order_by(AgentToolCall.created_at)
            .all()
        )
        assert [row.status for row in rows] == ["succeeded"] * 5 + ["proposed"]
        db.refresh(run)
        # The cap survives a park/resume: the persisted state already counts 5.
        assert '"guardian_approvals": 5' in (run.agent_state_json or "")
    finally:
        _cleanup(run_id)
        db.close()


def test_the_approval_mode_endpoint_accepts_guardian(client):
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "guardian-mode-endpoint"},
        json={"title": "Guardian endpoint"},
    ).json()
    response = client.put(
        f"/api/conversations/{conversation['id']}/approval-mode",
        headers={"Idempotency-Key": "guardian-mode-endpoint-put"},
        json={"mode": "guardian"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["approval_mode"] == "guardian"


def test_an_explicit_ask_policy_row_parks_despite_an_approving_guardian(
    client, monkeypatch
):
    """The reviewer softens only the tool's DEFAULT prudence. A row somebody
    wrote — "ask me about this tool" — is a stated instruction, and the
    provenance flag `Verdict.guardian_may_review` keeps the reviewer from ever
    seeing it."""

    def must_not_be_called(**kwargs):
        raise AssertionError("guardian.review must not see a policy-row ask")

    monkeypatch.setattr(guardian, "review", must_not_be_called)
    run_id = _make_run(client, "Guardian: row-asked tool.")
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        db.add(
            ToolPolicy(
                workspace_id=run.workspace_id,
                tool_name="create_document",
                policy="ask",
            )
        )
        db.commit()
        step = _write_step(
            [_function_call("create_document", {"title": "Guardian doc", "content": "x"})]
        )
        assert run_agent_turn(db, run, evidence=[], model_step=step) is None
        db.refresh(run)
        assert run.status == "waiting_for_approval"
    finally:
        _cleanup(run_id)
        db.close()


def test_an_org_opinion_takes_the_call_out_of_the_guardians_reach(
    client, monkeypatch
):
    """An org `ask` and the tool's default `ask` are the same string with
    entirely different authority. The ceiling's provenance is checked where
    the rows are — `evaluate_policy` — so the reviewer never sees a call an
    organization said a human must."""
    from app.services import agent_loop

    def must_not_be_called(**kwargs):
        raise AssertionError("guardian.review must not see an org-mandated ask")

    monkeypatch.setattr(guardian, "review", must_not_be_called)
    monkeypatch.setattr(agent_loop, "_org_ceiling", lambda *args, **kwargs: "ask")
    run_id = _make_run(client, "Guardian: org-asked tool.")
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        step = _write_step(
            [_function_call("create_document", {"title": "Guardian doc", "content": "x"})]
        )
        assert run_agent_turn(db, run, evidence=[], model_step=step) is None
        db.refresh(run)
        assert run.status == "waiting_for_approval"
    finally:
        _cleanup(run_id)
        db.close()


def test_an_unabsorbed_steer_defers_the_guardian_to_the_human(client, monkeypatch):
    """A steer the model step has not yet read means the user is actively
    re-instructing; the reviewer stands down and the park lets the person
    apply their own latest words."""
    from app.models import RunEvent
    from app.services.agent_loop import STEER_REQUESTED

    reviews: List[str] = []

    def fake_review(**kwargs):
        reviews.append(kwargs["name"])
        return guardian.GuardianVerdict(True, "fine")

    monkeypatch.setattr(guardian, "review", fake_review)
    run_id = _make_run(client, "Guardian: steered mid-write.")
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)

        def model_step(input_items, tools, instructions):
            if not any(
                isinstance(item, dict) and item.get("type") == "function_call_output"
                for item in input_items
            ):
                # The steer lands while this response streams: it is in the
                # events but not yet absorbed when the call is triaged.
                session = SessionLocal()
                try:
                    session.add(
                        RunEvent(
                            workspace_id=run.workspace_id,
                            run_id=run_id,
                            sequence=999,
                            event_type=STEER_REQUESTED,
                            payload_json=json.dumps({"content": "wait, hold off"}),
                        )
                    )
                    session.commit()
                finally:
                    session.close()
                return _completed(
                    output=[
                        _function_call(
                            "create_document", {"title": "Guardian doc", "content": "x"}
                        )
                    ]
                )
            return _completed(output_text="done")

        assert run_agent_turn(db, run, evidence=[], model_step=model_step) is None
        db.refresh(run)
        assert run.status == "waiting_for_approval"
        assert reviews == []
    finally:
        _cleanup(run_id)
        db.close()


def test_absorbed_steers_reach_the_reviewers_prompt(client, monkeypatch):
    """A steer the turn already absorbed is part of what the user is asking
    for, so the reviewer reads it beside the prompt."""
    from app.models import RunEvent
    from app.services.agent_loop import STEER_REQUESTED

    seen_prompts: List[str] = []

    def fake_review(**kwargs):
        seen_prompts.append(kwargs["prompt"])
        return guardian.GuardianVerdict(True, "fine")

    monkeypatch.setattr(guardian, "review", fake_review)
    run_id = _make_run(client, "Guardian: steer then write.")
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        db.add(
            RunEvent(
                workspace_id=run.workspace_id,
                run_id=run_id,
                sequence=1,
                event_type=STEER_REQUESTED,
                payload_json=json.dumps({"content": "use the Q3 numbers only"}),
            )
        )
        db.commit()
        step = _write_step(
            [_function_call("create_document", {"title": "Guardian doc", "content": "x"})]
        )
        result = run_agent_turn(db, run, evidence=[], model_step=step)
        assert result is not None
        assert len(seen_prompts) == 1
        assert "Guardian: steer then write." in seen_prompts[0]
        assert "use the Q3 numbers only" in seen_prompts[0]
    finally:
        _cleanup(run_id)
        db.close()
