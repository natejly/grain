"""The HTTP surface: compile, store, run, and read back.

ADR 0007 shipped a compiler with nothing in front of it — "no endpoint creates,
compiles, runs or lists a workflow yet, which is why tests/isolation.py gained no
cases". These are those endpoints, and the properties they have to hold:

- a graph a model hallucinated is refused with *every* finding, not the first,
  because the caller's next move is a repair prompt or a person reading a list;
- storing a workflow grants nothing, so the write tool it names still parks;
- a run is a background task, and its detail says what each node did;
- and a foreign id is indistinguishable from a missing one, which the sweep in
  tests/isolation.py checks route by route.
"""
from __future__ import annotations

from typing import Any, Dict

import pytest
from conftest import Identity, create_identity
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_workflow_executor import (
    Probe,
    authorize,
    graph,
    install,
    manual_node,
    tool_node,
)

from app.database import SessionLocal
from app.main import app
from app.models import ToolPolicy, WorkflowRun
from app.services.workflows import executor

TEST_BASE_URL = "https://testserver"


@pytest.fixture
def identity() -> Identity:
    return create_identity(name="API owner", workspace_name="API workspace")


@pytest.fixture
def client(identity: Identity) -> Any:
    with TestClient(app, base_url=TEST_BASE_URL) as test_client:
        authorize(test_client, identity)
        yield test_client


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def probe_graph() -> Dict[str, Any]:
    return graph(
        [
            tool_node("gather", "probe_read", {"text": "open PRs"}),
            tool_node("send", "probe_write", {"text": "{{ gather.output }}"}),
        ],
        [{"from": "gather", "to": "send"}],
        name="Probe automation",
    )


# --------------------------------------------------------------------------
# Compiling
# --------------------------------------------------------------------------


def test_compiling_returns_a_graph_without_storing_anything(client: Any) -> None:
    """A compiled workflow is a proposal, and a preview is not even that."""
    before = client.get("/api/workflows").json()
    response = client.post(
        "/api/workflows/compile",
        json={"source_prompt": "search the workspace and summarise what you find"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["graph"]["nodes"], body
    assert body["summary"]["node_count"] == len(body["graph"]["nodes"])
    assert client.get("/api/workflows").json() == before


def test_a_hallucinated_tool_is_refused_with_every_finding(client: Any) -> None:
    """The feature's central claim, at the edge of the system.

    422 rather than 500, and a body a repair prompt can be built from.
    """
    response = client.post(
        "/api/workflows",
        json={
            "graph": graph(
                [
                    tool_node("a", "no_such_tool"),
                    tool_node("b", "another_missing_tool"),
                ]
            )
        },
    )
    assert response.status_code == 422, response.text
    codes = {item["code"] for item in response.json()["detail"]["errors"]}
    assert codes == {"tool_unknown"}
    assert len(response.json()["detail"]["errors"]) == 2


def test_a_create_with_neither_a_prompt_nor_a_graph_is_refused(client: Any) -> None:
    assert client.post("/api/workflows", json={}).status_code == 422


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


def test_a_stored_workflow_round_trips_and_says_it_will_park(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, Probe("probe_read"), Probe("probe_write", read_only=False))
    created = client.post("/api/workflows", json={"graph": probe_graph()})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "Probe automation"
    assert body["version"] == 1
    assert body["status"] == "draft"
    # The question a scheduler needs answered before running anything unattended.
    assert body["requires_approval"] is True

    fetched = client.get(f"/api/workflows/{body['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["graph"]["nodes"][0]["tool"] == "probe_read"
    assert body["id"] in {row["id"] for row in client.get("/api/workflows").json()}


def test_storing_a_workflow_grants_nothing(
    client: Any, db: Any, identity: Identity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Naming a write tool in a graph is not permission to call it.

    No policy row appears, in either scope, and the run below still parks.
    """
    install(monkeypatch, Probe("probe_read"), Probe("probe_write", read_only=False))
    client.post("/api/workflows", json={"graph": probe_graph()})
    rows = list(
        db.scalars(
            select(ToolPolicy).where(ToolPolicy.workspace_id == identity.workspace_id)
        )
    )
    assert rows == []


def test_recompiling_bumps_the_version_and_keeps_the_ask(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`source_prompt` survives beside the graph so drift stays visible as drift."""
    install(monkeypatch, Probe("probe_read"), Probe("probe_write", read_only=False))
    created = client.post(
        "/api/workflows",
        json={"graph": probe_graph(), "source_prompt": "post the PR digest"},
    ).json()

    updated = client.patch(
        f"/api/workflows/{created['id']}",
        json={"graph": graph([tool_node("only", "probe_read")], name="Just reading")},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == 2
    assert updated.json()["source_prompt"] == "post the PR digest"
    assert updated.json()["requires_approval"] is False


def test_enabling_and_disabling_a_workflow(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, Probe("probe_read"))
    created = client.post(
        "/api/workflows",
        json={"graph": graph([tool_node("only", "probe_read")])},
    ).json()

    assert (
        client.patch(f"/api/workflows/{created['id']}", json={"status": "active"})
        .json()["status"]
        == "active"
    )
    assert (
        client.patch(f"/api/workflows/{created['id']}", json={"status": "disabled"})
        .json()["status"]
        == "disabled"
    )
    # A disabled workflow refuses to run rather than silently doing nothing.
    refused = client.post(f"/api/workflows/{created['id']}/run", json={"payload": {}})
    assert refused.status_code == 409


def test_deleting_a_workflow_takes_its_history(
    client: Any, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, Probe("probe_read"))
    created = client.post(
        "/api/workflows",
        json={"graph": graph([tool_node("only", "probe_read")])},
    ).json()
    started = client.post(f"/api/workflows/{created['id']}/run", json={"payload": {}})
    assert started.status_code == 202

    assert client.delete(f"/api/workflows/{created['id']}").status_code == 204
    assert client.get(f"/api/workflows/{created['id']}").status_code == 404
    db.expire_all()
    assert db.get(WorkflowRun, started.json()["id"]) is None


# --------------------------------------------------------------------------
# Running and reading back
# --------------------------------------------------------------------------


def test_a_manual_run_executes_and_the_detail_shows_every_node(
    client: Any, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """202, then a background task; by the time the response is delivered the
    TestClient has drained it, which is also how the real BackgroundTasks run."""
    reader = Probe("probe_read", reply="twelve open PRs")
    install(monkeypatch, reader)
    created = client.post(
        "/api/workflows",
        json={
            "graph": graph(
                [tool_node("gather", "probe_read", {"text": "{{ input.repo }}"})]
            )
        },
    ).json()

    started = client.post(
        f"/api/workflows/{created['id']}/run", json={"payload": {"repo": "acme/x"}}
    )
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]

    detail = client.get(f"/api/workflows/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["status"] == "succeeded", body["error"]
    assert body["trigger"] == "manual"
    assert body["run_id"], "a workflow run points at the chat run carrying its events"
    assert len(body["nodes"]) == 1
    node = body["nodes"][0]
    assert node["node_key"] == "gather"
    assert node["status"] == "succeeded"
    assert node["policy"] == "allow"
    assert node["output"] == "twelve open PRs"
    assert node["arguments"] == {"text": "acme/x"}
    assert reader.calls == [{"text": "acme/x"}]

    listed = client.get(f"/api/workflows/{created['id']}/runs")
    assert [row["id"] for row in listed.json()] == [run_id]


def test_a_run_that_parks_is_visible_as_parked(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = Probe("probe_write", read_only=False)
    install(monkeypatch, Probe("probe_read"), writer)
    created = client.post("/api/workflows", json={"graph": probe_graph()}).json()
    run_id = client.post(
        f"/api/workflows/{created['id']}/run", json={"payload": {}}
    ).json()["id"]

    body = client.get(f"/api/workflows/runs/{run_id}").json()
    assert body["status"] == "waiting_for_approval"
    nodes = {node["node_key"]: node for node in body["nodes"]}
    assert nodes["gather"]["status"] == "succeeded"
    assert nodes["send"]["status"] == "waiting_for_approval"
    assert nodes["send"]["agent_tool_call_id"]
    assert writer.calls == []

    # And the approval it is waiting on is an ordinary card in the inbox.
    inbox = client.get("/api/agent-tool-calls").json()
    assert nodes["send"]["agent_tool_call_id"] in {row["id"] for row in inbox}


def test_the_decision_endpoint_carries_manual_values_into_the_run(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual node parks like an approval; the decision endpoint that chat uses
    resumes it, and the values ride in `inputs` to become the node's output that a
    downstream step reads — the whole loop, over HTTP."""
    ship = Probe("probe_ship")
    install(monkeypatch, ship)
    created = client.post(
        "/api/workflows",
        json={
            "graph": graph(
                [
                    manual_node("collect", "Which version ships?", [{"name": "version"}]),
                    tool_node(
                        "ship", "probe_ship", {"text": "{{ collect.output.version }}"}
                    ),
                ],
                [{"from": "collect", "to": "ship"}],
            )
        },
    ).json()
    run_id = client.post(
        f"/api/workflows/{created['id']}/run", json={"payload": {}}
    ).json()["id"]

    parked = client.get(f"/api/workflows/runs/{run_id}").json()
    assert parked["status"] == "waiting_for_approval"
    collect = {node["node_key"]: node for node in parked["nodes"]}["collect"]
    call_id = collect["agent_tool_call_id"]
    assert call_id

    decided = client.post(
        f"/api/agent-tool-calls/{call_id}/decision",
        json={"decision": "approved", "remember": False, "inputs": {"version": "2.0.1"}},
        headers={"Idempotency-Key": "api-manual-approve-1"},
    )
    assert decided.status_code == 200, decided.text

    body = client.get(f"/api/workflows/runs/{run_id}").json()
    assert body["status"] == "succeeded", body["error"]
    nodes = {node["node_key"]: node for node in body["nodes"]}
    assert nodes["collect"]["output"] == {"version": "2.0.1"}
    assert nodes["ship"]["arguments"] == {"text": "2.0.1"}
    assert ship.calls == [{"text": "2.0.1"}]


def test_a_manual_submission_that_violates_a_field_is_refused(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The person is waiting on the request, so an invalid submission 422s in
    front of them and the run stays parked and answerable."""
    install(monkeypatch, Probe("probe_ship"))
    created = client.post(
        "/api/workflows",
        json={
            "graph": graph(
                [
                    manual_node(
                        "collect",
                        "Which version ships?",
                        [{"name": "version", "type": "string", "required": True}],
                    )
                ]
            )
        },
    ).json()
    run_id = client.post(
        f"/api/workflows/{created['id']}/run", json={"payload": {}}
    ).json()["id"]
    parked = client.get(f"/api/workflows/runs/{run_id}").json()
    call_id = parked["nodes"][0]["agent_tool_call_id"]

    refused = client.post(
        f"/api/agent-tool-calls/{call_id}/decision",
        json={"decision": "approved", "remember": False, "inputs": {}},
        headers={"Idempotency-Key": "api-manual-invalid-1"},
    )
    assert refused.status_code == 422, refused.text
    assert client.get(f"/api/workflows/runs/{run_id}").json()["status"] == (
        "waiting_for_approval"
    )


def test_a_failed_run_reports_the_node_that_failed(
    client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(
        monkeypatch,
        Probe("probe_read"),
        Probe("probe_bad", raises=lambda: RuntimeError("upstream is down")),
    )
    created = client.post(
        "/api/workflows",
        json={
            "graph": graph(
                [tool_node("a", "probe_read"), tool_node("b", "probe_bad")],
                [{"from": "a", "to": "b"}],
            )
        },
    ).json()
    run_id = client.post(
        f"/api/workflows/{created['id']}/run", json={"payload": {}}
    ).json()["id"]

    body = client.get(f"/api/workflows/runs/{run_id}").json()
    assert body["status"] == "failed"
    assert "upstream is down" in body["error"]
    nodes = {node["node_key"]: node for node in body["nodes"]}
    assert nodes["a"]["status"] == "succeeded"
    assert nodes["b"]["status"] == "failed"


def test_a_run_from_another_workspace_is_not_found(
    client: Any, db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workflow_run id reaches the node table without passing through the
    workflow, so it needs its own filter and its own 404."""
    from test_workflow_executor import begin

    stranger = create_identity(name="Stranger", workspace_name="Stranger workspace")
    install(monkeypatch, Probe("probe_read"))
    foreign = begin(db, stranger, graph([tool_node("only", "probe_read")]))
    executor.advance_run(db, foreign)

    response = client.get(f"/api/workflows/runs/{foreign.id}")
    assert response.status_code == 404
    assert foreign.id not in response.text
