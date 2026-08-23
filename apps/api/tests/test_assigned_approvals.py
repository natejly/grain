"""Routing a parked approval to one member: `assigned_to`.

Assignment is routing, not decision — the call stays `proposed`, the run stays
parked, and the compare-and-set decision claim is untouched. What these tests
pin down: the named member (and nobody else) may answer while the row names
one, '' hands the call back to anyone, a foreign user id is refused with the
same 404 a missing user would get, the same run-visibility gate that guards
the decision guards the assignment (another member's personal thread parks are
unreachable), a decided call refuses routing with a 409, the Inbox carries the
assignment so the client can partition the queue, and the migrated schema
matches the ORM. The cross-workspace DENY case for the assign route lives in
the isolation sweep (tests/isolation.py); here we prove the within-workspace
halves.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from conftest import TEST_BASE_URL, create_identity, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from test_agent_approvals import _park_run

from app.config import get_settings
from app.database import SessionLocal, engine
from app.main import app
from app.models import Conversation, Membership, Run, ToolPolicy, User

API_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_resume(monkeypatch):
    """Capture the resume hand-off instead of running a model-backed turn, and
    clean up `_park_run`'s ask-policy row — the unique key on (workspace,
    owner, tool, scope) refuses the next test's copy otherwise."""
    calls = []
    monkeypatch.setattr(
        "app.api.tools.resume_run",
        lambda run_id, tool_call_id, decision, amendment=None, inputs=None: calls.append(
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


def _member(workspace_id: str, *, name: str) -> tuple[TestClient, str]:
    """A fresh plain member of `workspace_id`, and a client signed in as them."""
    db = SessionLocal()
    try:
        user = User(email=f"{os.urandom(6).hex()}@example.com", name=name)
        db.add(user)
        db.flush()
        db.add(Membership(workspace_id=workspace_id, user_id=user.id, role="member"))
        db.commit()
        user_id = user.id
    finally:
        db.close()
    token, csrf_token = issue_session(user_id)
    settings = get_settings()
    member_client = TestClient(app, base_url=TEST_BASE_URL)
    member_client.cookies.set(settings.session_cookie_name, token)
    member_client.headers[settings.csrf_header_name] = csrf_token
    return member_client, user_id


def _share_thread(run_id: str) -> None:
    """Flip the parked run's thread to shared, so a colleague can see it."""
    db = SessionLocal()
    try:
        run = db.query(Run).filter(Run.id == run_id).one()
        thread = db.query(Conversation).filter(Conversation.id == run.conversation_id).one()
        thread.shared = True
        db.commit()
    finally:
        db.close()


def _assign(client, call_id: str, user_id: str):
    return client.post(f"/api/agent-tool-calls/{call_id}/assign", json={"user_id": user_id})


def _decide(client, call_id: str, decision: str = "approved"):
    return client.post(
        f"/api/agent-tool-calls/{call_id}/decision",
        headers={"Idempotency-Key": "assign-test-" + os.urandom(8).hex()},
        json={"decision": decision, "remember": False},
    )


def _identity(client) -> dict:
    return client.get("/api/bootstrap").json()["identity"]


def test_the_assignee_and_only_the_assignee_decides(client, _no_resume):
    """While the row names a member, another reviewer's decision is a 409 —
    routing narrows who may answer without touching the decision machinery."""
    run_id, call_id = _park_run(client)
    _share_thread(run_id)
    workspace_id = _identity(client)["workspace_id"]
    client_b, user_b = _member(workspace_id, name="Assignee B")

    assigned = _assign(client, call_id, user_b)
    assert assigned.status_code == 200
    assert assigned.json()["assigned_to"] == user_b
    assert assigned.json()["status"] == "proposed"

    refused = _decide(client, call_id)
    assert refused.status_code == 409
    assert "assigned" in refused.json()["detail"]

    decided = _decide(client_b, call_id)
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"
    assert _no_resume, "the assignee's approval must schedule the resume"


def test_unassigning_hands_the_call_back_to_anyone(client, _no_resume):
    run_id, call_id = _park_run(client)
    _share_thread(run_id)
    workspace_id = _identity(client)["workspace_id"]
    _client_b, user_b = _member(workspace_id, name="Briefly named")

    assert _assign(client, call_id, user_b).status_code == 200
    cleared = _assign(client, call_id, "")
    assert cleared.status_code == 200
    assert cleared.json()["assigned_to"] == ""

    decided = _decide(client, call_id)
    assert decided.status_code == 200


def test_assignment_refuses_a_user_who_is_not_a_member(client, _no_resume):
    """A foreign workspace's user id answers the same 404 a made-up id would —
    the refusal must confirm neither that the user exists nor where."""
    _run_id, call_id = _park_run(client)
    outsider = create_identity(name="Outsider", workspace_name="Elsewhere")

    for probe in (outsider.user_id, "no-such-user"):
        refused = _assign(client, call_id, probe)
        assert refused.status_code == 404

    # The probe changed nothing: the call still waits on anyone.
    inbox = client.get("/api/inbox").json()
    row = next(row for row in inbox["approvals"] if row["id"] == call_id)
    assert row["assigned_to"] == ""


def test_another_members_personal_thread_park_cannot_be_routed(client, _no_resume):
    """Assignment respects run visibility: a colleague who cannot see the
    thread cannot assign (or learn about) its parked call — 404, exactly as
    the decision endpoint answers them."""
    _run_id, call_id = _park_run(client)  # owner's personal thread, not shared
    workspace_id = _identity(client)["workspace_id"]
    client_b, user_b = _member(workspace_id, name="Roommate")

    refused = _assign(client_b, call_id, user_b)
    assert refused.status_code == 404


def test_a_decided_call_refuses_routing(client, _no_resume):
    _run_id, call_id = _park_run(client)
    assert _decide(client, call_id, "denied").status_code == 200
    me = _identity(client)["user_id"]

    stale = _assign(client, call_id, me)
    assert stale.status_code == 409


def test_the_inbox_carries_the_assignment(client, _no_resume):
    """`assigned_to` rides the feed row — and an assigned-away approval still
    lists, because nothing parked is invisible; the client de-emphasizes it."""
    run_id, call_id = _park_run(client)
    _share_thread(run_id)
    workspace_id = _identity(client)["workspace_id"]
    client_b, user_b = _member(workspace_id, name="Feed reader")

    assert _assign(client, call_id, user_b).status_code == 200

    for reader in (client, client_b):
        inbox = reader.get("/api/inbox").json()
        rows = [row for row in inbox["approvals"] if row["id"] == call_id]
        assert len(rows) == 1
        assert rows[0]["assigned_to"] == user_b


def test_the_migration_chain_builds_the_column_the_orm_declares():
    """`alembic upgrade head` from an empty database must match `create_all`
    for agent_tool_calls — 0047 adds `assigned_to`, and a column that exists in
    only one of the two schemas is a bug nobody sees until deploy."""
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'chain.db'}"
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=API_ROOT,
            capture_output=True,
            text=True,
            env={
                "PATH": "/usr/bin:/bin",
                "DATABASE_URL": url,
                "APP_ENV": "test",
                "MODEL_PROVIDER": "scripted",
                "SCRIPTED_MODEL_SCRIPT": "tests/scripts/agent.json",
                "PYTHONPATH": str(API_ROOT),
            },
        )
        assert result.returncode == 0, result.stderr

        migrated = inspect(create_engine(url))
        declared = inspect(engine)
        assert {column["name"] for column in migrated.get_columns("agent_tool_calls")} == {
            column["name"] for column in declared.get_columns("agent_tool_calls")
        }
        assert "assigned_to" in {
            column["name"] for column in migrated.get_columns("agent_tool_calls")
        }
        assert {index["name"] for index in migrated.get_indexes("agent_tool_calls")} >= {
            index["name"] for index in declared.get_indexes("agent_tool_calls")
        }
