"""Within-workspace visibility of a run's *activity*: events, approvals, cancel,
pending edits.

The multiplayer feature gates the conversation list/messages/stream/send on
`conversations.resolve_visible`, but a run's parked approvals, event stream,
cancel and pending edits resolve by `workspace_id` alone — so a PERSONAL chat
thread's activity used to leak to, and be actionable by, other members of the
same workspace. This file proves the gate that closed that:

  (i)   a personal thread's parked agent-tool-call is NOT listed / decidable /
        streamable / cancelable by another member (404 / absent);
  (ii)  once the thread is SHARED, that member CAN;
  (iii) a WORKFLOW-backed run's parked approval REMAINS visible to another
        member — automation is the Activity queue everyone reviews, and must not
        regress;
  (iv)  a member of ANOTHER workspace is still refused everywhere (the
        cross-workspace `workspace_id` filter, which never comes off).

Cross-workspace isolation as a whole is pinned by tests/isolation.py; this is
the member-vs-member half the run/tool-call surfaces were missing.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from conftest import TEST_BASE_URL, Identity, create_identity, issue_session
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    AgentToolCall,
    Membership,
    Run,
    ToolPolicy,
    User,
    Workflow,
    WorkflowRun,
)
from app.services.agent_loop import run_agent_turn


class FakeResponse:
    def __init__(self, output=None, output_text=""):
        self.output = output or []
        self.output_text = output_text


def _client_for(identity: Identity) -> TestClient:
    settings = get_settings()
    client = TestClient(app, base_url=TEST_BASE_URL)
    client.cookies.set(settings.session_cookie_name, identity.token)
    client.headers[settings.csrf_header_name] = identity.csrf_token
    return client


def _member(workspace_id: str, *, name: str, role: str = "member") -> tuple[TestClient, str]:
    """A fresh user placed in `workspace_id`, and a client authenticated as them."""
    db = SessionLocal()
    try:
        user = User(email=f"{os.urandom(6).hex()}@example.com", name=name)
        db.add(user)
        db.flush()
        db.add(Membership(workspace_id=workspace_id, user_id=user.id, role=role))
        db.commit()
        user_id = user.id
    finally:
        db.close()
    token, csrf_token = issue_session(user_id)
    settings = get_settings()
    client = TestClient(app, base_url=TEST_BASE_URL)
    client.cookies.set(settings.session_cookie_name, token)
    client.headers[settings.csrf_header_name] = csrf_token
    return client, user_id


def _key() -> dict[str, str]:
    return {"Idempotency-Key": "vis-" + os.urandom(8).hex()}


@pytest.fixture(autouse=True)
def _no_resume(monkeypatch):
    """A decision schedules `resume_run` in the background; here it is a no-op so
    a 200 proves the route accepted the decision without running a model turn."""
    monkeypatch.setattr("app.api.tools.resume_run", lambda *args, **kwargs: None)
    yield


def _park_agent_call(
    client: TestClient,
    *,
    tool: str = "list_datasets",
    arguments: str = "{}",
    workflow_backed: bool = False,
) -> tuple[str, str, str]:
    """Create a personal conversation, park a run on an approval for `tool`.

    Returns (conversation_id, run_id, call_id). When `workflow_backed`, a
    WorkflowRun is attached to the run so it reads as workspace automation even
    though its backing conversation is personal — which is exactly the case rule
    (a) must keep visible.
    """
    bootstrap = client.get("/api/bootstrap").json()
    workspace_id = bootstrap["identity"]["workspace_id"]
    user_id = bootstrap["identity"]["user_id"]
    agent_id = bootstrap["default_agent_id"]
    conversation = client.post(
        "/api/conversations", headers=_key(), json={"title": "Personal"}
    ).json()

    db = SessionLocal()
    try:
        run = Run(
            workspace_id=workspace_id,
            conversation_id=conversation["id"],
            agent_id=agent_id,
            created_by=user_id,
            status="running",
            prompt="do the thing",
        )
        db.add(run)
        # Force the park regardless of the tool's default: a read-only tool would
        # otherwise run unattended and never reach an approval.
        db.add(ToolPolicy(workspace_id=workspace_id, tool_name=tool, policy="ask"))
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
                                name=tool,
                                call_id="call-1",
                                arguments=arguments,
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
        call_id = call.id
        if workflow_backed:
            workflow = Workflow(
                workspace_id=workspace_id,
                created_by=user_id,
                name="nightly",
                graph_json="{}",
            )
            db.add(workflow)
            db.flush()
            db.add(
                WorkflowRun(
                    workspace_id=workspace_id,
                    workflow_id=workflow.id,
                    created_by=user_id,
                    graph_json="{}",
                    run_id=run_id,
                    trigger="schedule",
                    status="waiting_for_approval",
                )
            )
            db.commit()
        return conversation["id"], run_id, call_id
    finally:
        db.close()


def _finish_run(run_id: str) -> None:
    """Drive a run to a terminal state so its event stream ends promptly.

    `GET /runs/{id}/events` holds the connection open while a run is live; a
    terminal run's generator drains the backlog and returns, which is what lets
    a test assert the endpoint accepted the request without hanging on an
    infinite SSE. The visibility gate runs before the stream and is unaffected by
    status.
    """
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run is not None
        run.status = "completed"
        db.commit()
    finally:
        db.close()


def _listed(client: TestClient, path: str, call_id: str) -> bool:
    return call_id in [row["id"] for row in client.get(path).json()]


def _decide(client: TestClient, call_id: str) -> int:
    return client.post(
        f"/api/agent-tool-calls/{call_id}/decision",
        headers=_key(),
        json={"decision": "approved", "remember": False},
    ).status_code


def test_personal_thread_activity_is_hidden_from_other_members():
    owner = create_identity(name="Owner A", workspace_name="Vis WS")
    client_a = _client_for(owner)
    client_b, _ = _member(owner.workspace_id, name="Member B")
    _conv_id, run_id, call_id = _park_agent_call(client_a)

    # The creator sees and can act; another member of the same workspace cannot.
    assert _listed(client_a, "/api/agent-tool-calls", call_id)
    assert not _listed(client_b, "/api/agent-tool-calls", call_id)

    assert _decide(client_b, call_id) == 404
    assert client_b.get(f"/api/runs/{run_id}/events").status_code == 404
    assert (
        client_b.post(f"/api/runs/{run_id}/cancel", headers=_key()).status_code == 404
    )
    # Steering injects a prompt into the run, so it sits behind the same gate:
    # a personal thread's run answers 404 to another member, not 409/202.
    assert (
        client_b.post(
            f"/api/runs/{run_id}/steer",
            json={"content": "steered by B"},
            headers=_key(),
        ).status_code
        == 404
    )


def test_sharing_the_thread_lets_other_members_act():
    owner = create_identity(name="Owner A", workspace_name="Vis WS")
    client_a = _client_for(owner)
    client_b, _ = _member(owner.workspace_id, name="Member B")
    conv_id, run_id, call_id = _park_agent_call(client_a)

    assert (
        client_a.put(
            f"/api/conversations/{conv_id}/share", json={"shared": True}
        ).status_code
        == 200
    )

    # B now sees the parked call and may decide it.
    assert _listed(client_b, "/api/agent-tool-calls", call_id)
    assert _decide(client_b, call_id) == 200


def test_shared_thread_events_are_streamable_by_members():
    owner = create_identity(name="Owner A", workspace_name="Vis WS")
    client_a = _client_for(owner)
    client_b, _ = _member(owner.workspace_id, name="Member B")
    conv_id, run_id, _call_id = _park_agent_call(client_a)

    # Personal: the run's events 404 for another member (refused before the stream).
    assert client_b.get(f"/api/runs/{run_id}/events").status_code == 404

    client_a.put(f"/api/conversations/{conv_id}/share", json={"shared": True})
    _finish_run(run_id)
    assert client_b.get(f"/api/runs/{run_id}/events").status_code == 200


def test_shared_thread_cancel_is_allowed_for_members():
    owner = create_identity(name="Owner A", workspace_name="Vis WS")
    client_a = _client_for(owner)
    client_b, _ = _member(owner.workspace_id, name="Member B")
    conv_id, run_id, _call_id = _park_agent_call(client_a)
    client_a.put(f"/api/conversations/{conv_id}/share", json={"shared": True})

    cancelled = client_b.post(f"/api/runs/{run_id}/cancel", headers=_key())
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_workflow_backed_activity_stays_visible_to_members():
    """No automation regression: a workflow-backed run's parked approval is the
    Activity queue every member reviews, so it stays visible even though its
    backing conversation is a personal thread."""
    owner = create_identity(name="Owner A", workspace_name="Vis WS")
    client_a = _client_for(owner)
    client_b, _ = _member(owner.workspace_id, name="Member B")
    _conv_id, run_id, call_id = _park_agent_call(client_a, workflow_backed=True)

    # Listable and decidable by a member who cannot see the backing personal thread.
    assert _listed(client_b, "/api/agent-tool-calls", call_id)
    assert _decide(client_b, call_id) == 200
    # And streamable once terminal (the gate runs before the stream regardless).
    _finish_run(run_id)
    assert client_b.get(f"/api/runs/{run_id}/events").status_code == 200


def test_pending_document_edits_respect_thread_visibility():
    owner = create_identity(name="Owner A", workspace_name="Vis WS")
    client_a = _client_for(owner)
    client_b, _ = _member(owner.workspace_id, name="Member B")
    conv_id, _run_id, call_id = _park_agent_call(
        client_a,
        tool="create_document",
        arguments='{"title": "A private brief", "content": "secret body"}',
    )

    # Personal: the proposed create lists for its creator, not for another member.
    assert _listed(client_a, "/api/documents-pending", call_id)
    assert not _listed(client_b, "/api/documents-pending", call_id)

    # Shared: it lists for the member too.
    client_a.put(f"/api/conversations/{conv_id}/share", json={"shared": True})
    assert _listed(client_b, "/api/documents-pending", call_id)


def test_cross_workspace_activity_is_refused_even_when_shared():
    """The load-bearing property: sharing relaxes visibility ONLY within the
    workspace. A member of a DIFFERENT workspace is refused on every surface
    before and after the share — the `workspace_id` filter never comes off."""
    owner = create_identity(name="Owner A", workspace_name="WS A")
    client_a = _client_for(owner)
    outsider = create_identity(name="Outsider C", workspace_name="WS C")
    client_c = _client_for(outsider)
    conv_id, run_id, call_id = _park_agent_call(client_a)
    client_a.put(f"/api/conversations/{conv_id}/share", json={"shared": True})

    assert not _listed(client_c, "/api/agent-tool-calls", call_id)
    assert not _listed(client_c, "/api/documents-pending", call_id)
    assert _decide(client_c, call_id) == 404
    assert client_c.get(f"/api/runs/{run_id}/events").status_code == 404
    assert (
        client_c.post(f"/api/runs/{run_id}/cancel", headers=_key()).status_code == 404
    )
