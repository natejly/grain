"""The per-agent scorecard: `GET /api/admin/agents`.

Everything the scorecard shows is derived from rows other subsystems write —
runs by status, `agent_tool_calls` by status and `decided_by`, `screen.flagged`
run events, and the `model_usage` ledger's own `agent_id` column — so these
tests plant those rows directly and assert the arithmetic against what was
planted, following `test_admin_observability.py`'s pattern: each test builds a
brand-new workspace so the counts are exactly its own rows.

KNOWN APP BUG (documented as strict xfails below): every GET of this endpoint
raises `sqlalchemy.exc.InvalidRequestError`. The `mode_approved` aggregate at
`apps/api/app/api/admin.py:1788` is written as::

    select(Run.agent_id, func.count()).join(Run, AgentToolCall.run_id == Run.id)

`func.count()` carries no table, so the statement's only FROM is `Run` — and
joining `Run` to itself with an ON clause that mentions a table not in the FROM
list is the exact shape SQLAlchemy refuses ("Don't know how to join to Run").
The sibling aggregates compile because their column lists put the joined table
in the FROM list; this one needs `.select_from(AgentToolCall)` (or an
`AgentToolCall` column) like `call_counts` above it effectively has. Until that
line is fixed the endpoint 500s unconditionally, so the tests that read it are
marked `xfail(strict=True)`: they encode the intended arithmetic and will start
failing as XPASS the moment the query is repaired, demanding the marks be
removed.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, create_identity, issue_session
from fastapi.testclient import TestClient

from app.clock import utcnow
from app.database import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AgentToolCall,
    Conversation,
    Membership,
    ModelUsage,
    Run,
    RunEvent,
    User,
)

AGENTS = "/api/admin/agents"


@dataclass
class Workspace:
    """A fresh workspace, its owner's client, and the ids seeding needs."""

    identity: Identity
    client: TestClient
    agent_id: str
    conversation_id: str

    @property
    def workspace_id(self) -> str:
        return self.identity.workspace_id

    @property
    def user_id(self) -> str:
        return self.identity.user_id


def _new_workspace(name: str) -> Workspace:
    """A brand-new workspace with one owner, one seeded agent, one conversation."""
    identity = create_identity(name=f"{name} owner", workspace_name=f"{name} workspace")
    client = authenticate(TestClient(app, base_url=TEST_BASE_URL), identity)
    db = SessionLocal()
    try:
        agent = db.query(Agent).filter(Agent.workspace_id == identity.workspace_id).first()
        assert agent is not None, "create_identity should have made an agent"
        conversation = Conversation(
            workspace_id=identity.workspace_id,
            created_by=identity.user_id,
            title=f"{name} thread",
        )
        db.add(conversation)
        db.commit()
        return Workspace(
            identity=identity,
            client=client,
            agent_id=agent.id,
            conversation_id=conversation.id,
        )
    finally:
        db.close()


def _add_run(ws: Workspace, *, status: str, created_at: Optional[datetime] = None) -> str:
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=ws.workspace_id,
            conversation_id=ws.conversation_id,
            agent_id=ws.agent_id,
            created_by=ws.user_id,
            status=status,
            prompt="prompt",
            created_at=created_at or utcnow(),
            updated_at=created_at or utcnow(),
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _add_tool_call(
    ws: Workspace, run_id: str, *, status: str, decided_by: Optional[str] = None
) -> None:
    db = SessionLocal()
    try:
        db.add(
            AgentToolCall(
                workspace_id=ws.workspace_id,
                run_id=run_id,
                name="list_datasets",
                status=status,
                decided_by=decided_by,
            )
        )
        db.commit()
    finally:
        db.close()


def _flag_run(ws: Workspace, run_id: str) -> None:
    db = SessionLocal()
    try:
        db.add(
            RunEvent(
                workspace_id=ws.workspace_id,
                run_id=run_id,
                sequence=0,
                event_type="screen.flagged",
                payload_json="{}",
            )
        )
        db.commit()
    finally:
        db.close()


def _add_usage(
    ws: Workspace, *, agent_id: str, total_tokens: int, cost_usd: Optional[float]
) -> None:
    db = SessionLocal()
    try:
        db.add(
            ModelUsage(
                workspace_id=ws.workspace_id,
                agent_id=agent_id,
                operation="chat",
                model="scripted",
                total_tokens=total_tokens,
                cost_usd=cost_usd,
            )
        )
        db.commit()
    finally:
        db.close()


def _add_member(ws: Workspace) -> TestClient:
    """A second person in the same workspace, role "member"."""
    db = SessionLocal()
    try:
        user = User(email=f"{uuid.uuid4().hex}@example.com", name="Plain member")
        db.add(user)
        db.flush()
        db.add(Membership(workspace_id=ws.workspace_id, user_id=user.id, role="member"))
        db.commit()
        user_id = user.id
    finally:
        db.close()
    token, csrf = issue_session(user_id)
    return authenticate(
        TestClient(app, base_url=TEST_BASE_URL),
        Identity(
            user_id=user_id, workspace_id=ws.workspace_id, token=token, csrf_token=csrf
        ),
    )


def _row_for(body: dict, agent_id: str) -> dict:
    matches = [row for row in body["agents"] if row["agent_id"] == agent_id]
    assert len(matches) == 1, f"expected exactly one scorecard row for {agent_id}"
    return matches[0]


def test_the_scorecard_arithmetic_counts_exactly_the_planted_rows():
    ws = _new_workspace("Scorecard")
    completed = _add_run(ws, status="completed")
    failed = _add_run(ws, status="failed")

    # Three tool calls: one a person approved implicitly (no decider recorded),
    # one denied, one that ran on a mode's say-so.
    _add_tool_call(ws, completed, status="succeeded")
    _add_tool_call(ws, completed, status="denied", decided_by=ws.user_id)
    _add_tool_call(ws, completed, status="succeeded", decided_by="mode:auto_writes")

    # The injection screen flagged one of the two runs.
    _flag_run(ws, failed)

    # Two ledger rows carry the agent: one priced, one unpriced. The agentless
    # row (an embedding, say) must move nothing.
    _add_usage(ws, agent_id=ws.agent_id, total_tokens=1000, cost_usd=0.05)
    _add_usage(ws, agent_id=ws.agent_id, total_tokens=500, cost_usd=None)
    _add_usage(ws, agent_id="", total_tokens=9999, cost_usd=3.0)

    body = ws.client.get(AGENTS).json()
    assert body["window_days"] == 30
    row = _row_for(body, ws.agent_id)
    assert row["runs"] == 2
    assert row["completed_runs"] == 1
    assert row["failed_runs"] == 1
    assert row["tool_calls"] == 3
    assert row["denied_calls"] == 1
    assert row["mode_approved_calls"] == 1
    assert row["flagged_runs"] == 1
    assert row["usage_calls"] == 2
    assert row["total_tokens"] == 1500
    assert row["cost_usd"] == pytest.approx(0.05)
    assert row["unpriced_calls"] == 1
    assert row["enabled"] is True


def test_two_flag_events_on_one_run_count_it_once():
    """flagged_runs is distinct runs, not raw events."""
    ws = _new_workspace("FlagDistinct")
    run_id = _add_run(ws, status="completed")
    db = SessionLocal()
    try:
        for sequence in (0, 1):
            db.add(
                RunEvent(
                    workspace_id=ws.workspace_id,
                    run_id=run_id,
                    sequence=sequence,
                    event_type="screen.flagged",
                    payload_json="{}",
                )
            )
        db.commit()
    finally:
        db.close()
    row = _row_for(ws.client.get(AGENTS).json(), ws.agent_id)
    assert row["flagged_runs"] == 1


def test_another_workspaces_agent_never_appears():
    ws = _new_workspace("Mine")
    other = _new_workspace("Theirs")
    _add_run(other, status="completed")
    _add_usage(other, agent_id=other.agent_id, total_tokens=777, cost_usd=1.0)

    body = ws.client.get(AGENTS).json()
    listed_ids = {row["agent_id"] for row in body["agents"]}
    assert listed_ids == {ws.agent_id}
    assert other.agent_id not in listed_ids
    # And our own untouched agent reads as all zeroes, not as their numbers.
    row = _row_for(body, ws.agent_id)
    assert row["runs"] == 0
    assert row["total_tokens"] == 0


def test_the_scorecard_refuses_a_plain_member():
    ws = _new_workspace("Gate")
    member = _add_member(ws)
    response = member.get(AGENTS)
    assert response.status_code == 403
    assert response.json()["detail"] == "Owner role required"
    assert ws.client.get(AGENTS).status_code == 200


def test_the_scorecard_refuses_an_anonymous_caller(anonymous_client):
    assert anonymous_client.get(AGENTS).status_code == 401


def test_a_run_older_than_the_window_is_excluded():
    ws = _new_workspace("Window")
    _add_run(ws, status="completed", created_at=utcnow() - timedelta(hours=1))
    _add_run(ws, status="failed", created_at=utcnow() - timedelta(days=40))

    body = ws.client.get(AGENTS, params={"days": 30}).json()
    assert body["window_days"] == 30
    row = _row_for(body, ws.agent_id)
    assert row["runs"] == 1
    assert row["completed_runs"] == 1
    assert row["failed_runs"] == 0
