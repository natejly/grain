"""A space's default agent: chosen on the space, born onto its threads.

The contract under test is deliberately narrow — the column is a SEED. It is
copied onto `Conversation.default_agent_id` when a thread is created in the
space, and only the composer reads either column; the run path still receives
its agent explicitly per turn. So these tests assert what the wire shows
(SpaceOut, ConversationOut) and what degrades (retired or disabled agents),
and `test_space_directives.py` remains the whole story of what actually
reaches a turn.

Templates close the loop at both ends: saving a space snapshots its agent
into `agent_ids_json` (the column that had been stored-and-unused since
0053), and instantiating finally applies the first surviving id.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from conftest import TEST_BASE_URL, authenticate, create_identity
from fastapi.testclient import TestClient

from app.main import app


def _key() -> Dict[str, str]:
    return {"Idempotency-Key": "space-agent-" + os.urandom(8).hex()}


def _client(label: str = "Space agent owner") -> TestClient:
    identity = create_identity(name=label, workspace_name=f"{label} ws")
    client = TestClient(app, base_url=TEST_BASE_URL)
    authenticate(client, identity)
    client.identity = identity  # type: ignore[attr-defined]
    return client


def _agent(client: TestClient, name: str = "Archivist") -> Dict[str, Any]:
    response = client.post(
        "/api/agents",
        json={"name": name, "instructions": f"Answer as {name}."},
        headers=_key(),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _space(client: TestClient, name: str = "Research") -> Dict[str, Any]:
    response = client.post("/api/spaces", json={"name": name}, headers=_key())
    assert response.status_code == 201, response.text
    return response.json()


def _set_agent(client: TestClient, space_id: str, agent_id: str):
    return client.patch(
        f"/api/spaces/{space_id}", json={"default_agent_id": agent_id}
    )


def _thread(client: TestClient, space_id: str = "") -> Dict[str, Any]:
    response = client.post(
        "/api/conversations",
        json={"title": "T", "space_id": space_id},
        headers=_key(),
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------
# Choosing the space's agent


def test_a_space_picks_an_agent_and_the_wire_carries_it() -> None:
    client = _client()
    agent = _agent(client)
    space = _space(client)
    assert space["default_agent_id"] == ""

    updated = _set_agent(client, space["id"], agent["id"])
    assert updated.status_code == 200, updated.text
    assert updated.json()["default_agent_id"] == agent["id"]
    # And the list view says the same — both SpaceOut builders carry it.
    listed = client.get("/api/spaces").json()
    assert [row["default_agent_id"] for row in listed] == [agent["id"]]

    cleared = _set_agent(client, space["id"], "")
    assert cleared.status_code == 200
    assert cleared.json()["default_agent_id"] == ""


def test_patch_leaves_the_agent_alone_when_it_names_only_other_fields() -> None:
    client = _client("Leave alone")
    agent = _agent(client)
    space = _space(client)
    _set_agent(client, space["id"], agent["id"])
    renamed = client.patch(f"/api/spaces/{space['id']}", json={"name": "Renamed"})
    assert renamed.status_code == 200
    assert renamed.json()["default_agent_id"] == agent["id"]


def test_an_unknown_or_foreign_agent_is_a_404_and_stores_nothing() -> None:
    client = _client("Refuser")
    space = _space(client)
    assert _set_agent(client, space["id"], "no-such-agent").status_code == 404

    stranger = _client("Stranger")
    foreign_agent = _agent(stranger, "Not yours")
    # A foreign agent id must read as absent — the same 404 as an unknown one,
    # never a different refusal that would confirm the id exists elsewhere.
    assert _set_agent(client, space["id"], foreign_agent["id"]).status_code == 404
    assert (
        client.get(f"/api/spaces/{space['id']}").json()["default_agent_id"] == ""
    )


# --------------------------------------------------------------------------
# What a new thread is born with


def test_a_thread_created_in_the_space_is_born_preferring_its_agent() -> None:
    client = _client("Seeder")
    agent = _agent(client)
    space = _space(client)
    _set_agent(client, space["id"], agent["id"])

    in_space = _thread(client, space["id"])
    assert in_space["default_agent_id"] == agent["id"]
    # An unspaced thread keeps the workspace default ("" — the composer falls
    # back to the bootstrap's oldest enabled agent).
    assert _thread(client)["default_agent_id"] == ""


def test_a_retired_or_disabled_preference_degrades_to_the_default() -> None:
    client = _client("Degrader")
    agent = _agent(client)  # second agent; the seeded one keeps the workspace alive
    space = _space(client)
    _set_agent(client, space["id"], agent["id"])

    disable = client.patch(
        f"/api/agents/{agent['id']}", json={"enabled": False}, headers=_key()
    )
    assert disable.status_code == 200, disable.text
    # The space keeps its preference; the thread it seeds does not — a seed
    # naming a disabled agent would just bounce off _stage_turn's 400.
    assert _thread(client, space["id"])["default_agent_id"] == ""
    assert (
        client.get(f"/api/spaces/{space['id']}").json()["default_agent_id"]
        == agent["id"]
    )

    delete = client.delete(f"/api/agents/{agent['id']}", headers=_key())
    assert delete.status_code == 204, delete.text
    assert _thread(client, space["id"])["default_agent_id"] == ""


# --------------------------------------------------------------------------
# Templates round-trip the agent


def test_save_as_template_snapshots_the_agent_and_instantiate_applies_it() -> None:
    client = _client("Templater")
    agent = _agent(client)
    space = _space(client)
    _set_agent(client, space["id"], agent["id"])

    created = client.post(
        "/api/space-templates",
        json={"name": "Playbook", "from_space_id": space["id"]},
        headers=_key(),
    )
    assert created.status_code == 201, created.text
    template = created.json()
    assert template["agent_ids"] == [agent["id"]]

    instantiated = client.post(
        f"/api/space-templates/{template['id']}/instantiate",
        json={"name": "From playbook"},
        headers=_key(),
    )
    assert instantiated.status_code == 201, instantiated.text
    assert instantiated.json()["default_agent_id"] == agent["id"]
    # And a thread in the instantiated space inherits it end to end.
    thread = _thread(client, instantiated.json()["id"])
    assert thread["default_agent_id"] == agent["id"]


def test_a_template_whose_roster_is_gone_applies_no_agent() -> None:
    client = _client("Ghost roster")
    agent = _agent(client)
    space = _space(client)
    _set_agent(client, space["id"], agent["id"])
    template = client.post(
        "/api/space-templates",
        json={"name": "Ghost playbook", "from_space_id": space["id"]},
        headers=_key(),
    ).json()

    assert (
        client.delete(f"/api/agents/{agent['id']}", headers=_key()).status_code == 204
    )
    instantiated = client.post(
        f"/api/space-templates/{template['id']}/instantiate",
        json={"name": "From ghost"},
        headers=_key(),
    )
    assert instantiated.status_code == 201, instantiated.text
    assert instantiated.json()["default_agent_id"] == ""


def test_a_snapshot_of_an_agentless_space_records_no_agent() -> None:
    client = _client("Plain snapshot")
    space = _space(client)
    template = client.post(
        "/api/space-templates",
        json={"name": "Plain playbook", "from_space_id": space["id"]},
        headers=_key(),
    ).json()
    assert template["agent_ids"] == []
