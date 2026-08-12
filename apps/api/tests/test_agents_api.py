"""The agents CRUD surface, and the two invariants it owns.

An agent is a system prompt plus a provisioned tool subset, so most of what
matters here is serialization discipline (None vs [] on `allowed_tools` are
different facts) and the guards: a workspace never drops to zero enabled
agents, and an agent with run history is retired, never deleted.

Every test runs in a fresh workspace (`identity_client`), because the guards
are counting rows — the seeded demo workspace's agent population is somebody
else's furniture.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, create_identity
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Agent, AuditEvent, Conversation, Run


def key() -> Dict[str, str]:
    return {"Idempotency-Key": "agents-" + os.urandom(8).hex()}


def create(
    client: TestClient,
    *,
    name: str = "Researcher",
    instructions: str = "You answer tersely, from evidence.",
    allowed_tools: Optional[list] = None,
    include_allowed: bool = False,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {"name": name, "instructions": instructions}
    if include_allowed:
        body["allowed_tools"] = allowed_tools
    response = client.post("/api/agents", json=body, headers=headers or key())
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def owner() -> TestClient:
    identity = create_identity(name="Agent owner", workspace_name="Agent workspace")
    client = TestClient(app, base_url=TEST_BASE_URL)
    authenticate(client, identity)
    client.identity = identity  # type: ignore[attr-defined]
    return client


def seeded_agent_id(client: TestClient) -> str:
    """The one agent `create_identity` seeds (enabled, no subset)."""
    rows = client.get("/api/agents").json()
    assert len(rows) == 1
    return rows[0]["id"]


# --------------------------------------------------------------------------
# Shape and round trips
# --------------------------------------------------------------------------


def test_created_agent_round_trips_through_the_list(owner: TestClient) -> None:
    created = create(owner, name="Terse librarian")
    assert created["name"] == "Terse librarian"
    assert created["enabled"] is True
    # No allowed_tools sent at all: the agent sees the whole registry.
    assert created["allowed_tools"] is None

    listed = owner.get("/api/agents").json()
    assert [row["name"] for row in listed][-1] == "Terse librarian"
    fetched = next(row for row in listed if row["id"] == created["id"])
    assert fetched == created


def test_allowed_tools_none_and_empty_are_different_facts(owner: TestClient) -> None:
    """None = all tools; [] = a prompt-only agent with zero tools.

    Asserted through the row, not just the response: the API's None must be
    stored as "" and its [] as "[]", or a restart would forget the difference.
    """
    everything = create(owner, name="Omni", include_allowed=True, allowed_tools=None)
    nothing = create(owner, name="Talker", include_allowed=True, allowed_tools=[])
    subset = create(
        owner,
        name="Reader",
        include_allowed=True,
        allowed_tools=["search_sources", "recall_memory", "search_sources"],
    )

    assert everything["allowed_tools"] is None
    assert nothing["allowed_tools"] == []
    # Deduplicated and sorted on the way in — a set, stored legibly.
    assert subset["allowed_tools"] == ["recall_memory", "search_sources"]

    db = SessionLocal()
    try:
        stored = {
            row.name: row.allowed_tools_json
            for row in db.scalars(
                select(Agent).where(Agent.name.in_(["Omni", "Talker", "Reader"]))
            )
        }
    finally:
        db.close()
    assert stored["Omni"] == ""
    assert stored["Talker"] == "[]"
    assert stored["Reader"] == '["recall_memory", "search_sources"]'


def test_patch_edits_only_what_it_names(owner: TestClient) -> None:
    agent = create(owner, include_allowed=True, allowed_tools=["search_sources"])
    patched = owner.patch(
        f"/api/agents/{agent['id']}", json={"instructions": "Answer in French."}
    ).json()
    assert patched["instructions"] == "Answer in French."
    assert patched["name"] == agent["name"]
    assert patched["allowed_tools"] == ["search_sources"]


def test_clearing_the_subset_needs_the_explicit_flag(owner: TestClient) -> None:
    agent = create(owner, include_allowed=True, allowed_tools=["search_sources"])
    # A patch that says nothing about tools leaves the subset alone.
    untouched = owner.patch(f"/api/agents/{agent['id']}", json={"name": "Still scoped"})
    assert untouched.json()["allowed_tools"] == ["search_sources"]
    # The flag is the only way back to "all tools".
    cleared = owner.patch(
        f"/api/agents/{agent['id']}", json={"clear_allowed_tools": True}
    )
    assert cleared.json()["allowed_tools"] is None
    # And it outranks a list sent beside it.
    both = owner.patch(
        f"/api/agents/{agent['id']}",
        json={"clear_allowed_tools": True, "allowed_tools": ["recall_memory"]},
    )
    assert both.json()["allowed_tools"] is None


def test_create_replays_on_the_same_idempotency_key(owner: TestClient) -> None:
    headers = key()
    first = create(owner, name="Once", headers=headers)
    again = owner.post(
        "/api/agents",
        json={"name": "Once", "instructions": "You answer tersely, from evidence."},
        headers=headers,
    )
    assert again.status_code == 201
    assert again.json()["id"] == first["id"]
    names = [row["name"] for row in owner.get("/api/agents").json()]
    assert names.count("Once") == 1


def test_mutations_are_audited(owner: TestClient) -> None:
    agent = create(owner, name="Audited")
    owner.patch(f"/api/agents/{agent['id']}", json={"description": "now with notes"})
    delete = owner.delete(f"/api/agents/{agent['id']}")
    assert delete.status_code == 204

    db = SessionLocal()
    try:
        actions = [
            row.action
            for row in db.scalars(
                select(AuditEvent)
                .where(AuditEvent.resource_id == agent["id"])
                .order_by(AuditEvent.created_at)
            )
        ]
    finally:
        db.close()
    assert actions == ["agent.created", "agent.updated", "agent.deleted"]


# --------------------------------------------------------------------------
# The guards
# --------------------------------------------------------------------------


def test_the_last_enabled_agent_cannot_be_disabled_or_deleted(
    owner: TestClient,
) -> None:
    only = seeded_agent_id(owner)
    disabled = owner.patch(f"/api/agents/{only}", json={"enabled": False})
    assert disabled.status_code == 409
    deleted = owner.delete(f"/api/agents/{only}")
    assert deleted.status_code == 409
    # With a second enabled agent, retiring the first is allowed.
    create(owner, name="Successor")
    assert (
        owner.patch(f"/api/agents/{only}", json={"enabled": False}).status_code == 200
    )


def test_an_agent_with_run_history_is_retired_not_deleted(owner: TestClient) -> None:
    agent = create(owner, name="Veteran")
    identity: Identity = owner.identity  # type: ignore[attr-defined]
    db = SessionLocal()
    try:
        conversation = Conversation(
            workspace_id=identity.workspace_id, created_by=identity.user_id
        )
        db.add(conversation)
        db.flush()
        db.add(
            Run(
                workspace_id=identity.workspace_id,
                conversation_id=conversation.id,
                agent_id=agent["id"],
                created_by=identity.user_id,
                status="completed",
                prompt="hello",
            )
        )
        db.commit()
    finally:
        db.close()

    refused = owner.delete(f"/api/agents/{agent['id']}")
    assert refused.status_code == 409
    assert "disable" in refused.json()["detail"]
    retired = owner.patch(f"/api/agents/{agent['id']}", json={"enabled": False})
    assert retired.status_code == 200
    assert retired.json()["enabled"] is False


def test_agents_are_workspace_scoped(owner: TestClient) -> None:
    agent = create(owner, name="Private")
    stranger_identity = create_identity(name="Stranger", workspace_name="Elsewhere")
    stranger = TestClient(app, base_url=TEST_BASE_URL)
    authenticate(stranger, stranger_identity)

    assert all(row["id"] != agent["id"] for row in stranger.get("/api/agents").json())
    assert stranger.get(f"/api/agents/{agent['id']}").status_code in (404, 405)
    assert (
        stranger.patch(
            f"/api/agents/{agent['id']}", json={"name": "Mine now"}
        ).status_code
        == 404
    )
    assert stranger.delete(f"/api/agents/{agent['id']}").status_code == 404


# --------------------------------------------------------------------------
# The tool catalogue the provisioning UI reads
# --------------------------------------------------------------------------


def test_the_tool_catalogue_lists_the_registry_with_families(
    owner: TestClient,
) -> None:
    tools = owner.get("/api/tools").json()
    by_name = {row["name"]: row for row in tools}
    assert "search_sources" in by_name
    assert by_name["search_sources"]["family"] == "core"
    assert by_name["search_sources"]["read_only"] is True
    # A known write tool reports itself as one — the checklist renders the
    # "(writes)" caveat off this bit, so it must survive the trip.
    writers = [row for row in tools if not row["read_only"]]
    assert writers, "expected at least one write-capable tool in the registry"
    assert all(row["description"] for row in tools)
