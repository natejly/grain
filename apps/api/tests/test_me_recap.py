"""GET /api/me/recap: the member's own month, deterministically counted.

The properties worth pinning are all scoping properties. The counts are
member-scoped inside one workspace (a teammate's threads never inflate your
recap), the window is UTC month-to-date (a backdated row drops out),
`top_spaces` refuses the "" sentinel (the global shelf is not a space) and
resolves names workspace-scoped (a deleted space's id yields no foreign
name), and `memories_learned` counts only what the member themself learned
(their rows plus their runs' shared extractions), with liveness through
`services.memory._active` — a superseded row leaves the count exactly as it
leaves recall.
"""
from __future__ import annotations

import os
from datetime import timedelta

from conftest import TEST_BASE_URL, Identity, create_identity, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.clock import utcnow
from app.database import SessionLocal
from app.main import app
from app.models import Agent, Conversation, Membership, MemoryItem, Run, Space, User


def _client_for(identity: Identity) -> TestClient:
    from app.config import get_settings

    settings = get_settings()
    client = TestClient(app, base_url=TEST_BASE_URL)
    client.cookies.set(settings.session_cookie_name, identity.token)
    client.headers[settings.csrf_header_name] = identity.csrf_token
    return client


def _member(workspace_id: str, *, name: str) -> tuple[TestClient, str]:
    """A second user in the SAME workspace, so member-scoping is what the
    assertions exercise rather than the workspace filter."""
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
    from app.config import get_settings

    settings = get_settings()
    client = TestClient(app, base_url=TEST_BASE_URL)
    client.cookies.set(settings.session_cookie_name, token)
    client.headers[settings.csrf_header_name] = csrf_token
    return client, user_id


def _agent_of(workspace_id: str) -> str:
    db = SessionLocal()
    try:
        agent = db.scalar(select(Agent).where(Agent.workspace_id == workspace_id))
        assert agent is not None
        return agent.id
    finally:
        db.close()


def _add_thread(
    workspace_id: str,
    user_id: str,
    *,
    space_id: str = "",
    days_ago: int = 0,
) -> str:
    db = SessionLocal()
    try:
        conversation = Conversation(
            workspace_id=workspace_id,
            created_by=user_id,
            title="Recap probe",
            space_id=space_id,
            created_at=utcnow() - timedelta(days=days_ago),
        )
        db.add(conversation)
        db.commit()
        return conversation.id
    finally:
        db.close()


def _add_run(
    workspace_id: str,
    user_id: str,
    conversation_id: str,
    agent_id: str,
    *,
    days_ago: int = 0,
) -> str:
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
            created_by=user_id,
            prompt="recap probe",
            created_at=utcnow() - timedelta(days=days_ago),
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _add_memory(
    workspace_id: str,
    owner_id: str,
    *,
    key: str,
    status: str = "active",
    days_ago: int = 0,
    run_id: str = "",
) -> None:
    db = SessionLocal()
    try:
        db.add(
            MemoryItem(
                workspace_id=workspace_id,
                owner_id=owner_id,
                content=f"recap memory {key}",
                normalized_key=key,
                status=status,
                created_at=utcnow() - timedelta(days=days_ago),
                run_id=run_id,
            )
        )
        db.commit()
    finally:
        db.close()


def _add_space(workspace_id: str, name: str) -> str:
    db = SessionLocal()
    try:
        space = Space(workspace_id=workspace_id, name=name)
        db.add(space)
        db.commit()
        return space.id
    finally:
        db.close()


def test_recap_counts_are_member_scoped_and_shaped() -> None:
    """Tenant A's threads, runs and memories never appear in B's recap, even
    inside the same workspace — and the response carries the exact MeRecapOut
    keys the client types against."""
    owner = create_identity(name="Recap owner", workspace_name="Recap workspace")
    client_a = _client_for(owner)
    client_b, user_b = _member(owner.workspace_id, name="Recap teammate")
    agent_id = _agent_of(owner.workspace_id)

    thread_a = _add_thread(owner.workspace_id, owner.user_id)
    _add_run(owner.workspace_id, owner.user_id, thread_a, agent_id)
    _add_memory(owner.workspace_id, owner.user_id, key="a-only-fact")

    response_a = client_a.get("/api/me/recap")
    assert response_a.status_code == 200, response_a.text
    recap_a = response_a.json()
    assert set(recap_a) == {
        "since",
        "threads_started",
        "runs_started",
        "memories_learned",
        "top_spaces",
        "top_agents",
    }
    assert recap_a["threads_started"] == 1
    assert recap_a["runs_started"] == 1
    assert recap_a["memories_learned"] == 1
    assert recap_a["top_agents"] == [
        {"id": agent_id, "name": "Research partner", "count": 1}
    ]

    # The teammate's recap starts at zero: same workspace, different member.
    recap_b = client_b.get("/api/me/recap").json()
    assert recap_b["threads_started"] == 0
    assert recap_b["runs_started"] == 0
    assert recap_b["memories_learned"] == 0
    assert recap_b["top_agents"] == []
    # ... and their own activity counts only for them.
    thread_b = _add_thread(owner.workspace_id, user_b)
    _add_run(owner.workspace_id, user_b, thread_b, agent_id)
    recap_b2 = client_b.get("/api/me/recap").json()
    assert recap_b2["threads_started"] == 1
    assert recap_b2["runs_started"] == 1
    recap_a2 = client_a.get("/api/me/recap").json()
    assert recap_a2["threads_started"] == 1  # unchanged by B's activity


def test_recap_window_is_month_to_date() -> None:
    """A row backdated before the month's first instant is not this month's."""
    identity = create_identity(name="Window", workspace_name="Window workspace")
    client = _client_for(identity)
    agent_id = _agent_of(identity.workspace_id)

    # 40 days ago is provably last month whatever today's date is.
    old_thread = _add_thread(identity.workspace_id, identity.user_id, days_ago=40)
    _add_run(identity.workspace_id, identity.user_id, old_thread, agent_id, days_ago=40)
    _add_memory(identity.workspace_id, identity.user_id, key="old-fact", days_ago=40)

    recap = client.get("/api/me/recap").json()
    assert recap["threads_started"] == 0
    assert recap["runs_started"] == 0
    assert recap["memories_learned"] == 0
    # The window's own edge is in the payload, so the client can say "since".
    assert recap["since"].startswith(utcnow().strftime("%Y-%m-01"))


def test_top_spaces_excludes_the_sentinel_and_resolves_names_workspace_scoped() -> None:
    """"" is the global shelf, never a space — and a space id whose row is
    gone (or was never this workspace's) keeps its id with no name."""
    identity = create_identity(name="Spaces", workspace_name="Spaces workspace")
    client = _client_for(identity)
    space_id = _add_space(identity.workspace_id, "Research")

    _add_thread(identity.workspace_id, identity.user_id, space_id=space_id)
    _add_thread(identity.workspace_id, identity.user_id, space_id=space_id)
    _add_thread(identity.workspace_id, identity.user_id)  # unspaced: "" sentinel
    _add_thread(identity.workspace_id, identity.user_id, space_id="ghost-space-id")

    recap = client.get("/api/me/recap").json()
    ids = [group["id"] for group in recap["top_spaces"]]
    assert "" not in ids
    assert set(ids) == {space_id, "ghost-space-id"}
    by_id = {group["id"]: group for group in recap["top_spaces"]}
    assert by_id[space_id] == {"id": space_id, "name": "Research", "count": 2}
    # Stale id stays an id; the name is not invented and never foreign.
    assert by_id["ghost-space-id"]["name"] == ""
    # Ranked by count, most first.
    assert recap["top_spaces"][0]["id"] == space_id


def test_memories_learned_flows_through_the_active_chokepoint() -> None:
    """A superseded row created this month drops out of the count — proving
    the number rides `_active()`, not a private status predicate."""
    identity = create_identity(name="Memories", workspace_name="Memory workspace")
    client = _client_for(identity)

    _add_memory(identity.workspace_id, identity.user_id, key="kept-fact")
    _add_memory(
        identity.workspace_id,
        identity.user_id,
        key="replaced-fact",
        status="superseded",
    )
    _add_memory(
        identity.workspace_id,
        identity.user_id,
        key="forgotten-fact",
        status="deleted",
    )
    # A shared-shelf row with no learning run counts for nobody's recap —
    # recallable, but not something THIS member learned (see count_learned).
    _add_memory(identity.workspace_id, "", key="shared-fact")

    recap = client.get("/api/me/recap").json()
    assert recap["memories_learned"] == 1


def test_memories_learned_counts_only_what_the_member_learned() -> None:
    """The member-scoped promise, on the one count that used to break it:
    personal rows and shared rows the member's OWN runs extracted count;
    a teammate's shared learnings — run extractions and manual adds alike —
    never inflate the number."""
    owner = create_identity(name="Learner", workspace_name="Learned workspace")
    client_a = _client_for(owner)
    client_b, user_b = _member(owner.workspace_id, name="Busy teammate")
    agent_id = _agent_of(owner.workspace_id)

    # A's personal row, and a shared row A's own run learned: both count.
    _add_memory(owner.workspace_id, owner.user_id, key="a-personal")
    thread_a = _add_thread(owner.workspace_id, owner.user_id)
    run_a = _add_run(owner.workspace_id, owner.user_id, thread_a, agent_id)
    _add_memory(owner.workspace_id, "", key="a-run-shared", run_id=run_a)

    # The teammate's month: a shared extraction from THEIR run, and a manual
    # shared add. Neither may reach A's number.
    thread_b = _add_thread(owner.workspace_id, user_b)
    run_b = _add_run(owner.workspace_id, user_b, thread_b, agent_id)
    _add_memory(owner.workspace_id, "", key="b-run-shared", run_id=run_b)
    _add_memory(owner.workspace_id, "", key="b-manual-shared")

    assert client_a.get("/api/me/recap").json()["memories_learned"] == 2
    # B's recap sees their run's extraction; the manual shared add carries no
    # learner by schema (owner "" and no run) and counts for nobody.
    assert client_b.get("/api/me/recap").json()["memories_learned"] == 1
