"""The unified attention feed: `GET /api/inbox`.

What these pin is the contract the old surfaces broke: the waiting set has no
scan bound (a parked approval cannot be pushed off the list by newer,
already-decided calls), budget holds are member-visible facts rather than an
owner-only aside, decided and cancelled proposals are not offered as work, and
none of it leaks across a workspace boundary.
"""
from __future__ import annotations

import os
from datetime import timedelta

import pytest
from conftest import ask_before_writes
from test_agent_approvals import _park_run

from app.database import SessionLocal
from app.models import AgentToolCall, Conversation, Run, ToolPolicy
from app.services.agent_loop import PAUSED_FOR_BUDGET


@pytest.fixture(autouse=True)
def _clean_policies():
    """`_park_run` seeds an "ask" ToolPolicy per call; the unique key on
    (workspace, owner, tool, scope) refuses the second test's copy unless the
    first test's row is gone — same hygiene test_agent_approvals runs."""
    yield
    db = SessionLocal()
    try:
        db.query(ToolPolicy).delete()
        db.commit()
    finally:
        db.close()


def _identity(client) -> dict:
    return client.get("/api/bootstrap").json()["identity"]


def _approval_ids(client) -> list[str]:
    response = client.get("/api/inbox")
    assert response.status_code == 200
    return [row["id"] for row in response.json()["approvals"]]


def test_a_parked_chat_approval_lists_with_its_thread(client):
    run_id, call_id = _park_run(client)
    inbox = client.get("/api/inbox").json()
    rows = [row for row in inbox["approvals"] if row["id"] == call_id]
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == run_id
    assert row["origin"] == "chat"
    assert row["conversation_id"] != ""
    assert row["conversation_title"] == "Approvals"


def test_the_waiting_set_has_no_scan_bound(client):
    """A parked approval must survive any number of newer decided calls.

    This is the regression the endpoint exists to close: the old listing took
    the last fifty calls of ANY status and filtered client-side, so fifty
    recent searches made a genuinely parked run invisible everywhere.
    """
    run_id, call_id = _park_run(client)
    identity = _identity(client)
    db = SessionLocal()
    try:
        parked = db.get(AgentToolCall, call_id)
        # Milliseconds, not seconds: the rows must sort *above* the parked call
        # to push it out of the legacy window, but a whole minute of future-
        # dated rows would also outrank the next test's freshly parked call in
        # this shared database — which is a different test failing for this
        # test's reasons.
        for index in range(60):
            db.add(
                AgentToolCall(
                    workspace_id=identity["workspace_id"],
                    run_id=run_id,
                    name="search_sources",
                    status="succeeded",
                    created_at=parked.created_at + timedelta(milliseconds=index + 1),
                )
            )
        db.commit()
    finally:
        db.close()
    try:
        # The legacy window really does lose it — the lie the feed corrects.
        legacy = client.get("/api/agent-tool-calls").json()
        assert call_id not in [row["id"] for row in legacy]
        assert call_id in _approval_ids(client)
    finally:
        db = SessionLocal()
        try:
            db.query(AgentToolCall).filter(
                AgentToolCall.run_id == run_id,
                AgentToolCall.status == "succeeded",
            ).delete()
            db.commit()
        finally:
            db.close()


def test_a_decided_call_is_not_work(client):
    run_id, call_id = _park_run(client)
    db = SessionLocal()
    try:
        call = db.get(AgentToolCall, call_id)
        call.status = "denied"
        run = db.get(Run, run_id)
        run.status = "failed"
        db.commit()
    finally:
        db.close()
    assert call_id not in _approval_ids(client)


def test_a_cancelled_runs_leftover_proposal_is_not_work(client):
    """The proposal row survives a cancel; a decision on it would 409, so the
    feed must not offer it as a button."""
    run_id, call_id = _park_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        run.status = "cancelled"
        db.commit()
    finally:
        db.close()
    assert call_id not in _approval_ids(client)


def test_a_budget_hold_is_a_member_visible_fact(client):
    """A run the ceiling parked has no AgentToolCall, so it must surface as its
    own category — and to members, not only owners: a teammate who cannot see
    that the ceiling stopped the digest cannot know to ask for it to be raised.
    """
    identity = _identity(client)
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "inbox-hold-" + os.urandom(6).hex()},
        json={"title": "Held by budget"},
    ).json()
    ask_before_writes(client, conversation["id"])
    client.put(
        f"/api/conversations/{conversation['id']}/share",
        headers={"Idempotency-Key": "inbox-share-" + os.urandom(6).hex()},
        json={"shared": True},
    )
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=identity["workspace_id"],
            conversation_id=conversation["id"],
            agent_id=client.get("/api/bootstrap").json()["default_agent_id"],
            created_by=identity["user_id"],
            status="waiting_for_approval",
            paused_reason=PAUSED_FOR_BUDGET,
            prompt="Summarise everything",
        )
        db.add(run)
        db.commit()
        run_id = run.id
    finally:
        db.close()
    inbox = client.get("/api/inbox").json()
    held = [row for row in inbox["budget_holds"] if row["run_id"] == run_id]
    assert len(held) == 1
    assert held[0]["origin"] == "chat"
    # And it is not double-reported as an approval: there is nothing to decide.
    assert run_id not in {row["run_id"] for row in inbox["approvals"]}


def test_a_budget_held_workflow_with_no_backing_run_still_lists(client):
    """A workflow the ceiling stops between nodes parks on its OWN row —
    `WorkflowRun.paused_reason` — and may have no chat Run at all. The feed's
    Run-anchored query cannot see those; the WorkflowRun sweep must."""
    from app.models import Workflow, WorkflowRun

    identity = _identity(client)
    db = SessionLocal()
    try:
        workflow = Workflow(
            workspace_id=identity["workspace_id"],
            name="Nightly digest",
            created_by=identity["user_id"],
            graph_json="{}",
        )
        db.add(workflow)
        db.flush()
        held = WorkflowRun(
            workspace_id=identity["workspace_id"],
            workflow_id=workflow.id,
            status="waiting_for_approval",
            paused_reason=PAUSED_FOR_BUDGET,
            run_id=None,
        )
        db.add(held)
        db.commit()
        held_id = held.id
    finally:
        db.close()
    inbox = client.get("/api/inbox").json()
    rows = [row for row in inbox["budget_holds"] if row["workflow_run_id"] == held_id]
    assert len(rows) == 1
    assert rows[0]["origin"] == "workflow"
    assert rows[0]["workflow_name"] == "Nightly digest"
    # And exactly once — a held workflow WITH a backing run must not be listed
    # by both the Run sweep and the WorkflowRun sweep.
    assert len(inbox["budget_holds"]) == len(
        {(row["workflow_run_id"], row["run_id"]) for row in inbox["budget_holds"]}
    )


def test_another_members_personal_thread_stays_invisible(client):
    """The feed obeys run_activity_predicate: a personal thread's park is its
    creator's business. Forged here by re-pointing a parked thread at another
    user id, which is exactly the row shape the predicate must exclude."""
    run_id, call_id = _park_run(client)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        thread = db.get(Conversation, run.conversation_id)
        thread.created_by = "someone-else"
        thread.shared = False
        db.commit()
    finally:
        db.close()
    assert call_id not in _approval_ids(client)


def test_the_feed_is_workspace_scoped(client, identity_client):
    _, call_id = _park_run(client)
    other = identity_client()
    response = other.get("/api/inbox")
    assert response.status_code == 200
    body = response.json()
    assert call_id not in [row["id"] for row in body["approvals"]]
    assert body["budget_holds"] == []
    # The mentions, alerts and anomalies sets ride the same workspace filter —
    # a fresh tenant's feed carries none of the dev workspace's notifications.
    assert body["mentions"] == []
    assert body["alerts"] == []
    assert body["anomalies"] == []


def test_a_spend_anomaly_lists_for_every_member_until_resolved(client, identity_client):
    """The fifth waiting set: broadcast like an alert ('' -target, so every
    member sees it), carrying the agent id the deep link needs, gone from the
    feed the moment anyone resolves it — and invisible to a foreign tenant."""
    from app.models import Notification

    workspace_id = _identity(client)["workspace_id"]
    db = SessionLocal()
    try:
        row = Notification(
            workspace_id=workspace_id,
            target_user_id="",
            kind="spend_anomaly",
            status="open",
            title="Scribe is at 3× its usual spend",
            body="About $6.00 in the last 24 hours, against a usual $1.50.",
            agent_id="agent-under-watch",
        )
        db.add(row)
        db.commit()
        anomaly_id = row.id
    finally:
        db.close()

    listed = client.get("/api/inbox").json()["anomalies"]
    mine = [row for row in listed if row["id"] == anomaly_id]
    assert len(mine) == 1
    assert mine[0]["agent_id"] == "agent-under-watch"
    assert "3×" in mine[0]["title"]

    # Another workspace's feed never carries it.
    other = identity_client()
    assert anomaly_id not in [row["id"] for row in other.get("/api/inbox").json()["anomalies"]]

    # One resolve clears it for the room, through the shared notification
    # resolve route — anomalies own no endpoint of their own.
    resolved = client.post(f"/api/notifications/{anomaly_id}/resolve")
    assert resolved.status_code == 200
    assert anomaly_id not in [
        row["id"] for row in client.get("/api/inbox").json()["anomalies"]
    ]
