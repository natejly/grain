"""The admin observability panel: does it count the right runs, and only for the
right people, and only within one workspace.

Everything the panel shows is *derived* from rows the app already writes — a run's
start/finish timestamps, its terminal status, and the first `message.delta` event
it streamed — so these tests plant those rows directly (a read model tested through
its write endpoints would fail for reasons that have nothing to do with the panel)
and then assert the arithmetic against what was planted, not against itself.

Four properties are proved.

*It counts correctly.* Nearest-rank percentiles over a hand-built latency list,
throughput bucketed into the window, the completed/failed/cancelled split and the
error rate it implies, the live-run list, and DAU/WAU/MAU over seeded sessions —
each checked against the seeded numbers.

*TTFT is what was streamed.* Only a run that produced a `message.delta` appears in
the TTFT distribution, its value is the gap from the run's origin to that first
delta, and a delta that predates the origin clamps to zero rather than going
negative.

*Only an owner sees it.* A plain member of the same workspace gets the same 403 as
every other admin route (the shared gate is swept in `test_admin.py`; this asserts
the message for this route specifically).

*Nothing crosses out.* A second workspace's runs and failures never move this
workspace's numbers.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from conftest import TEST_BASE_URL, Identity, authenticate, create_identity, issue_session
from fastapi.testclient import TestClient

from app.clock import utcnow
from app.database import SessionLocal
from app.main import app
from app.models import Agent, Conversation, Membership, Run, RunEvent, User, UserSession

OBS = "/api/admin/observability"


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
    """A brand-new workspace with one owner, one agent and one conversation.

    Each test builds its own so the counts it asserts are exactly the rows it
    planted — a module-scoped fixture would let one test's runs drift into
    another's percentiles.
    """
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


def _add_run(
    ws: Workspace,
    *,
    status: str,
    created_at: datetime,
    finished_at: datetime | None = None,
    error: str = "",
    first_delta_at: datetime | None = None,
) -> str:
    """Plant one run with the exact timestamps the panel derives its numbers from.

    `created_at` is the origin both TTFT and total latency measure from;
    `finished_at` becomes `updated_at`, which is what the panel reads as the
    completion time; `first_delta_at`, when given, writes the single
    `message.delta` event whose timestamp is TTFT.
    """
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=ws.workspace_id,
            conversation_id=ws.conversation_id,
            agent_id=ws.agent_id,
            created_by=ws.user_id,
            status=status,
            prompt="prompt",
            error=error,
            created_at=created_at,
            updated_at=finished_at or created_at,
        )
        db.add(run)
        db.flush()
        if first_delta_at is not None:
            db.add(
                RunEvent(
                    workspace_id=ws.workspace_id,
                    run_id=run.id,
                    sequence=0,
                    event_type="message.delta",
                    payload_json="{}",
                    created_at=first_delta_at,
                )
            )
        db.commit()
        return run.id
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


def _add_active_member(ws: Workspace, *, last_seen_at: datetime) -> None:
    """A workspace member whose only session was last seen at a chosen time.

    Retention counts distinct members with a recent `user_sessions.last_seen_at`,
    reached through the memberships join, so a member needs both a membership and
    a session row to register.
    """
    db = SessionLocal()
    try:
        user = User(email=f"{uuid.uuid4().hex}@example.com", name="Active member")
        db.add(user)
        db.flush()
        db.add(Membership(workspace_id=ws.workspace_id, user_id=user.id, role="member"))
        db.add(
            UserSession(
                user_id=user.id,
                token_hash=uuid.uuid4().hex,
                expires_at=utcnow() + timedelta(days=30),
                last_seen_at=last_seen_at,
            )
        )
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------
# Latency percentiles


def test_latency_percentiles_are_nearest_rank_over_the_seeded_runs():
    ws = _new_workspace("Latency")
    base = utcnow() - timedelta(hours=2)
    # Ten completed runs whose total latencies are 100..1000 ms, seeded out of
    # order so a route that forgot to sort would rank wrongly.
    for ms in [700, 100, 400, 1000, 200, 900, 300, 600, 500, 800]:
        _add_run(
            ws,
            status="completed",
            created_at=base,
            finished_at=base + timedelta(milliseconds=ms),
        )
    total = ws.client.get(OBS).json()["total"]
    assert total["samples"] == 10
    # Nearest-rank: value at ceil(p/100 * n), 1-indexed.
    assert total["p50_ms"] == 500  # ceil(.50*10)=5 -> 5th
    assert total["p90_ms"] == 900  # ceil(.90*10)=9 -> 9th
    assert total["p99_ms"] == 1000  # ceil(.99*10)=10 -> 10th
    assert total["max_ms"] == 1000


def test_empty_window_reports_no_data_not_zero_latency():
    """A workspace with no runs must read as "no samples", never as an instant
    response — None everywhere, so the UI can tell the two apart."""
    ws = _new_workspace("Empty")
    body = ws.client.get(OBS).json()
    for metric in ("ttft", "total"):
        stats = body[metric]
        assert stats["samples"] == 0
        assert stats["p50_ms"] is None
        assert stats["p90_ms"] is None
        assert stats["p99_ms"] is None
        assert stats["max_ms"] is None
    assert body["runs_in_window"] == 0
    assert body["error_rate"] == 0.0
    assert body["recent_failures"] == []
    assert body["live_runs"] == []


# --------------------------------------------------------------------------
# TTFT capture


def test_ttft_measures_only_runs_that_streamed_a_delta():
    ws = _new_workspace("Ttft")
    base = utcnow() - timedelta(hours=1)
    # Two runs that streamed: first delta 150 ms and 450 ms after the origin.
    _add_run(
        ws,
        status="completed",
        created_at=base,
        finished_at=base + timedelta(seconds=5),
        first_delta_at=base + timedelta(milliseconds=150),
    )
    _add_run(
        ws,
        status="completed",
        created_at=base,
        finished_at=base + timedelta(seconds=5),
        first_delta_at=base + timedelta(milliseconds=450),
    )
    # A run that never streamed a delta (parked on approval): counts as a run in
    # the window but contributes no TTFT sample.
    _add_run(ws, status="waiting_for_approval", created_at=base)

    # Exactly the two streamed runs contribute; the parked run does not.
    body = ws.client.get(OBS).json()
    assert body["runs_in_window"] == 3
    ttft = body["ttft"]
    assert ttft["samples"] == 2
    assert ttft["max_ms"] == 450


def test_ttft_p50_is_the_lower_of_two_samples():
    ws = _new_workspace("TtftPair")
    base = utcnow() - timedelta(hours=1)
    for ms in (150, 450):
        _add_run(
            ws,
            status="completed",
            created_at=base,
            finished_at=base + timedelta(seconds=5),
            first_delta_at=base + timedelta(milliseconds=ms),
        )
    ttft = ws.client.get(OBS).json()["ttft"]
    assert ttft["samples"] == 2
    # nearest-rank p50 over [150,450]: ceil(0.5*2)=1 -> 1st -> 150.
    assert ttft["p50_ms"] == 150
    assert ttft["p90_ms"] == 450
    assert ttft["max_ms"] == 450


def test_ttft_clamps_a_delta_that_precedes_the_origin_to_zero():
    """A first delta stamped before the run's origin must read as 0, not negative,
    so a clock skew never produces a nonsense TTFT."""
    ws = _new_workspace("Clamp")
    base = utcnow() - timedelta(hours=1)
    _add_run(
        ws,
        status="completed",
        created_at=base,
        finished_at=base + timedelta(seconds=5),
        first_delta_at=base - timedelta(milliseconds=200),
    )
    ttft = ws.client.get(OBS).json()["ttft"]
    assert ttft["samples"] == 1
    assert ttft["p50_ms"] == 0
    assert ttft["max_ms"] == 0


# --------------------------------------------------------------------------
# Throughput, error rate, live runs


def test_throughput_buckets_the_window_and_counts_every_run():
    ws = _new_workspace("Throughput")
    now = utcnow()
    # One run near the start of the 24h window, one near the end.
    _add_run(ws, status="completed", created_at=now - timedelta(hours=23, minutes=30),
             finished_at=now - timedelta(hours=23))
    _add_run(ws, status="completed", created_at=now - timedelta(minutes=1),
             finished_at=now)
    buckets = ws.client.get(OBS).json()["throughput"]
    assert len(buckets) == 24
    starts = [b["start"] for b in buckets]
    assert starts == sorted(starts), "buckets must run oldest -> newest"
    assert sum(b["count"] for b in buckets) == 2
    assert buckets[0]["count"] == 1  # the near-start run
    assert buckets[-1]["count"] == 1  # the near-end run


def test_error_rate_and_status_counts_reflect_the_terminal_split():
    ws = _new_workspace("Errors")
    base = utcnow() - timedelta(hours=1)
    _add_run(ws, status="completed", created_at=base, finished_at=base + timedelta(seconds=1))
    _add_run(ws, status="cancelled", created_at=base, finished_at=base + timedelta(seconds=1))
    _add_run(ws, status="cancelled", created_at=base, finished_at=base + timedelta(seconds=1))
    _add_run(
        ws,
        status="failed",
        created_at=base,
        finished_at=base + timedelta(seconds=1),
        error="boom: provider timed out",
    )
    # A live run: counted in the window but not terminal, so it moves neither the
    # error rate nor the total-latency samples.
    _add_run(ws, status="running", created_at=base)

    body = ws.client.get(OBS).json()
    assert body["completed"] == 1
    assert body["cancelled"] == 2
    assert body["failed"] == 1
    # failed / (completed + failed + cancelled) = 1/4.
    assert body["error_rate"] == 0.25
    assert body["runs_in_window"] == 5
    # Total latency is over terminal runs only (the four above), not the live one.
    assert body["total"]["samples"] == 4

    failures = body["recent_failures"]
    assert len(failures) == 1
    assert failures[0]["error"] == "boom: provider timed out"

    live = body["live_runs"]
    assert len(live) == 1
    assert live[0]["status"] == "running"
    assert live[0]["age_seconds"] >= 0


# --------------------------------------------------------------------------
# Retention


def test_retention_counts_distinct_members_over_each_window():
    ws = _new_workspace("Retention")
    now = utcnow()
    # The owner's own session (from create_identity) was seen just now, so it
    # counts in all three windows; add members seen at spread-out times.
    _add_active_member(ws, last_seen_at=now)  # today
    _add_active_member(ws, last_seen_at=now - timedelta(days=3))  # this week
    _add_active_member(ws, last_seen_at=now - timedelta(days=15))  # this month
    _add_active_member(ws, last_seen_at=now - timedelta(days=40))  # too old

    retention = ws.client.get(OBS).json()["retention"]
    # owner + today's member.
    assert retention["dau"] == 2
    # dau + the 3-day member.
    assert retention["wau"] == 3
    # wau + the 15-day member; the 40-day member falls outside every window.
    assert retention["mau"] == 4


# --------------------------------------------------------------------------
# Only an owner


def test_observability_refuses_a_plain_member():
    ws = _new_workspace("Gate")
    member = _add_member(ws)
    response = member.get(OBS)
    assert response.status_code == 403
    assert response.json()["detail"] == "Owner role required"
    # And the owner of the same workspace is allowed through.
    assert ws.client.get(OBS).status_code == 200


def test_observability_refuses_an_anonymous_caller(anonymous_client):
    assert anonymous_client.get(OBS).status_code == 401


# --------------------------------------------------------------------------
# Nothing crosses out


def test_another_workspaces_runs_never_move_the_numbers():
    ws = _new_workspace("Mine")
    other = _new_workspace("Theirs")
    base = utcnow() - timedelta(hours=1)

    # Our workspace: one completed, one failed.
    _add_run(ws, status="completed", created_at=base, finished_at=base + timedelta(seconds=1))
    _add_run(ws, status="failed", created_at=base, finished_at=base + timedelta(seconds=1),
             error="ours failed")
    # The other workspace: a pile of runs and failures that must not be seen here.
    for _ in range(5):
        _add_run(other, status="failed", created_at=base,
                 finished_at=base + timedelta(seconds=1), error="theirs failed")
    _add_run(other, status="running", created_at=base)

    body = ws.client.get(OBS).json()
    assert body["runs_in_window"] == 2
    assert body["completed"] == 1
    assert body["failed"] == 1
    assert body["live_runs"] == []
    assert len(body["recent_failures"]) == 1
    assert body["recent_failures"][0]["error"] == "ours failed"
    assert "theirs failed" not in ws.client.get(OBS).text


# --------------------------------------------------------------------------
# Window parameter


def test_window_hours_is_echoed_and_bounded():
    ws = _new_workspace("Window")
    body = ws.client.get(OBS, params={"hours": 48}).json()
    assert body["window_hours"] == 48
    # Bounds mirror the endpoint's Query(ge=1, le=720).
    assert ws.client.get(OBS, params={"hours": 0}).status_code == 422
    assert ws.client.get(OBS, params={"hours": 721}).status_code == 422


def test_a_run_outside_the_window_is_not_counted():
    ws = _new_workspace("Outside")
    now = utcnow()
    _add_run(ws, status="completed", created_at=now - timedelta(minutes=30),
             finished_at=now - timedelta(minutes=29))
    _add_run(ws, status="completed", created_at=now - timedelta(hours=40),
             finished_at=now - timedelta(hours=40) + timedelta(seconds=1))
    body = ws.client.get(OBS, params={"hours": 24}).json()
    assert body["runs_in_window"] == 1
    assert body["completed"] == 1
