"""Dashboards the agent authors, and home screens their users arrange.

The first test here is the one the whole feature exists for: before it, nothing
in the product could create a dashboard. It goes the whole way — the model's
tool writes one, and an HTTP caller runs it and gets numbers back.

The rest hold the two lines that are easy to lose: a dashboard is shared and a
*pin* is not, and every write path the agent has is the same code the routes
use.
"""
from __future__ import annotations

import uuid

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, issue_session
from fastapi.testclient import TestClient

from app.auth import DEV_SEED_USER_ID, DEV_SEED_WORKSPACE_ID
from app.database import SessionLocal
from app.main import app
from app.models import Membership, User
from app.services.llm_tools import ToolContext, build_registry

CSV = "territory,revenue,closed_on\nNorth,10,2026-01-02\nSouth,20,2026-01-03\nNorth,30,2026-02-01\n"


def key() -> dict[str, str]:
    return {"Idempotency-Key": "dash-" + uuid.uuid4().hex}


def unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


def make_dataset(client, content: str = CSV) -> dict:
    upload = client.post(
        "/api/sources",
        headers=key(),
        files={"file": ("deals.csv", content.encode(), "text/csv")},
    )
    assert upload.status_code == 202, upload.text
    source_id = upload.json()["id"]
    response = client.post(
        "/api/datasets",
        headers=key(),
        json={
            "name": unique("Deals"),
            "description": "Bounded fixture",
            "source_id": source_id,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def workspace_registry():
    """The agent's tool registry, in process, as the seeded owner."""
    db = SessionLocal()
    context = ToolContext(
        workspace_id=DEV_SEED_WORKSPACE_ID,
        user_id=DEV_SEED_USER_ID,
        conversation_id="",
    )
    try:
        yield db, context, build_registry(db, context)
    finally:
        db.close()


def second_member() -> TestClient:
    """Another person in the *same* workspace, holding a real session.

    Not `identity_client`, which builds a whole new tenant. The interesting
    question for pins is not cross-tenant — the isolation sweep answers that —
    but whether two colleagues who legitimately share every dashboard also share
    a home screen. They must not.
    """
    db = SessionLocal()
    try:
        user = User(email=f"{uuid.uuid4().hex}@example.com", name="Second member")
        db.add(user)
        db.flush()
        db.add(
            Membership(
                workspace_id=DEV_SEED_WORKSPACE_ID, user_id=user.id, role="member"
            )
        )
        db.commit()
        user_id = user.id
    finally:
        db.close()
    token, csrf_token = issue_session(user_id)
    client = TestClient(app, base_url=TEST_BASE_URL)
    return authenticate(
        client,
        Identity(
            user_id=user_id,
            workspace_id=DEV_SEED_WORKSPACE_ID,
            token=token,
            csrf_token=csrf_token,
        ),
    )


# --------------------------------------------------------------------------
# Reachability: the agent authors, the route runs


def test_the_agent_can_author_a_dashboard_and_a_caller_can_run_it(
    client, workspace_registry
):
    dataset = make_dataset(client)
    db, context, registry = workspace_registry
    name = unique("Revenue by territory")

    spec = registry["create_dashboard"]
    # A write tool, so it inherits the approval gate rather than deciding for
    # itself that drawing a chart is harmless.
    assert spec.read_only is False
    assert spec.preview is not None

    result = spec.executor(
        db,
        context,
        {
            "name": name,
            "description": "Sum of revenue",
            "dataset_id": dataset["id"],
            "visualization": "bar",
            "query": {
                "group_by": "territory",
                "metrics": [
                    {"field": "revenue", "operation": "sum", "label": "total"}
                ],
                "order_by": "total",
                "order_direction": "desc",
                "limit": 10,
            },
            "x_field": "territory",
            "y_fields": ["total"],
        },
    )
    db.commit()
    assert "Created dashboard" in result.content, result.content

    listed = client.get("/api/dashboards").json()
    authored = next(item for item in listed if item["name"] == name)
    assert authored["template_id"] == ""

    run = client.post(f"/api/dashboards/{authored['id']}/run")
    assert run.status_code == 200, run.text
    assert run.json()["result"]["rows"] == [
        {"territory": "North", "total": 40},
        {"territory": "South", "total": 20},
    ]

    client.delete(f"/api/dashboards/{authored['id']}")


def test_the_preview_describes_the_chart_rather_than_the_arguments(
    client, workspace_registry
):
    dataset = make_dataset(client)
    db, context, registry = workspace_registry
    preview = registry["create_dashboard"].preview
    assert preview is not None
    text = preview(
        db,
        context,
        {
            "name": "Big deals",
            "dataset_id": dataset["id"],
            "visualization": "bar",
            "query": {
                "group_by": "territory",
                "metrics": [{"field": "revenue", "operation": "sum", "label": "total"}],
                "filters": [{"field": "revenue", "operator": "gt", "value": 5}],
            },
            "x_field": "territory",
            "y_fields": ["total"],
        },
    )
    # What a person approves is a sentence about a chart.
    assert "Big deals" in text
    assert "a bar of total by territory" in text
    assert "revenue gt 5" in text
    assert dataset["name"] in text


def test_a_dashboard_that_could_not_execute_is_refused_before_it_is_saved(client):
    dataset = make_dataset(client)
    name = unique("Broken")
    response = client.post(
        "/api/dashboards",
        headers=key(),
        json={
            "name": name,
            "description": "",
            "dataset_id": dataset["id"],
            "spec": {
                "visualization": "bar",
                "query": {
                    "group_by": "territory",
                    "metrics": [
                        {"field": "revenue", "operation": "sum", "label": "total"}
                    ],
                },
                "x_field": "territory",
                # The query returns territory and total. Nothing returns "revenue".
                "y_fields": ["revenue"],
            },
        },
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert [item["code"] for item in detail["errors"]] == ["spec_field_unknown"]
    assert "territory, total" in detail["errors"][0]["message"]
    assert [item["name"] for item in client.get("/api/dashboards").json()].count(name) == 0


# --------------------------------------------------------------------------
# Pins


def test_pinning_lays_tiles_out_and_the_arrangement_survives(client):
    dataset = make_dataset(client)
    first = _dashboard(client, dataset, unique("First"))
    second = _dashboard(client, dataset, unique("Second"))

    # No position given: the tile lands below what is already there rather than
    # on top of it.
    assert client.put(f"/api/dashboards/{first['id']}/pin", json={}).status_code == 200
    placed = client.put(f"/api/dashboards/{second['id']}/pin", json={})
    assert placed.status_code == 200
    assert placed.json()["grid_y"] == 4

    moved = client.put(
        "/api/dashboard-pins/layout",
        json={
            "tiles": [
                {
                    "dashboard_id": second["id"],
                    "grid_x": 0,
                    "grid_y": 0,
                    "grid_w": 12,
                    "grid_h": 3,
                },
                {
                    "dashboard_id": first["id"],
                    "grid_x": 0,
                    "grid_y": 3,
                    "grid_w": 6,
                    "grid_h": 4,
                },
            ]
        },
    )
    assert moved.status_code == 200, moved.text
    # Reading order: the tile now on the top row comes back first.
    assert [tile["dashboard"]["id"] for tile in moved.json()] == [
        second["id"],
        first["id"],
    ]
    assert moved.json()[0]["grid_w"] == 12

    reloaded = client.get("/api/dashboard-pins").json()
    assert [tile["dashboard"]["id"] for tile in reloaded] == [second["id"], first["id"]]

    # A second pin of the same dashboard moves the tile; it does not add one.
    client.put(f"/api/dashboards/{first['id']}/pin", json={"grid_x": 6, "grid_y": 0})
    after = client.get("/api/dashboard-pins").json()
    assert len(after) == 2
    assert next(
        tile for tile in after if tile["dashboard"]["id"] == first["id"]
    )["grid_x"] == 6

    _cleanup(client, [first, second])


def test_a_tile_dropped_past_the_last_column_is_trimmed_to_fit(client):
    dataset = make_dataset(client)
    dashboard = _dashboard(client, dataset, unique("Wide"))
    response = client.put(
        f"/api/dashboards/{dashboard['id']}/pin",
        json={"grid_x": 9, "grid_y": 0, "grid_w": 6, "grid_h": 4},
    )
    assert response.status_code == 200, response.text
    # 9 + 6 would run off a 12-column grid; the tile stays where it was put.
    assert (response.json()["grid_x"], response.json()["grid_w"]) == (9, 3)
    _cleanup(client, [dashboard])


def test_one_persons_home_screen_is_invisible_to_another_in_the_same_workspace(client):
    dataset = make_dataset(client)
    mine = _dashboard(client, dataset, unique("Mine"))
    theirs = _dashboard(client, dataset, unique("Theirs"))
    colleague = second_member()

    assert client.put(f"/api/dashboards/{mine['id']}/pin", json={}).status_code == 200
    assert (
        colleague.put(f"/api/dashboards/{theirs['id']}/pin", json={}).status_code == 200
    )

    # Both people see both dashboards — they share a workspace.
    names = {item["id"] for item in colleague.get("/api/dashboards").json()}
    assert {mine["id"], theirs["id"]} <= names

    # Neither sees the other's arrangement.
    assert [tile["dashboard"]["id"] for tile in client.get("/api/dashboard-pins").json()] == [
        mine["id"]
    ]
    assert [
        tile["dashboard"]["id"] for tile in colleague.get("/api/dashboard-pins").json()
    ] == [theirs["id"]]

    # And neither can move or remove a tile from it. A pin the caller does not
    # hold is absent, not forbidden: the answer must not reveal that somebody
    # else pinned it.
    assert client.delete(f"/api/dashboards/{theirs['id']}/pin").status_code == 404
    stolen = client.put(
        "/api/dashboard-pins/layout",
        json={
            "tiles": [
                {
                    "dashboard_id": theirs["id"],
                    "grid_x": 0,
                    "grid_y": 9,
                    "grid_w": 3,
                    "grid_h": 3,
                }
            ]
        },
    )
    assert stolen.status_code == 404
    assert colleague.get("/api/dashboard-pins").json()[0]["grid_y"] == 0

    colleague.delete(f"/api/dashboards/{theirs['id']}/pin")
    _cleanup(client, [mine, theirs])


def test_deleting_a_dashboard_takes_it_off_every_home_screen(client):
    dataset = make_dataset(client)
    dashboard = _dashboard(client, dataset, unique("Doomed"))
    colleague = second_member()
    client.put(f"/api/dashboards/{dashboard['id']}/pin", json={})
    colleague.put(f"/api/dashboards/{dashboard['id']}/pin", json={})

    assert client.delete(f"/api/dashboards/{dashboard['id']}").status_code == 204

    # Not just the deleter's screen. A tile with nothing behind it is worse than
    # a missing one, and the person who pinned it is rarely the one who deleted.
    assert client.get("/api/dashboard-pins").json() == []
    assert colleague.get("/api/dashboard-pins").json() == []
    assert client.post(f"/api/dashboards/{dashboard['id']}/run").status_code == 404


# --------------------------------------------------------------------------
# Cross-tenant, through the tools rather than over HTTP


def test_the_dashboard_tools_cannot_see_another_tenants_rows(client, identity_client):
    dataset = make_dataset(client)
    dashboard = _dashboard(client, dataset, unique("Private"))
    outsider = identity_client(workspace_name="Elsewhere")

    db = SessionLocal()
    try:
        context = ToolContext(
            workspace_id=outsider.identity.workspace_id,
            user_id=outsider.identity.user_id,
            conversation_id="",
        )
        registry = build_registry(db, context)
        listing = registry["list_dashboards"].executor(db, context, {}).content
        assert dashboard["id"] not in listing
        assert dashboard["name"] not in listing

        # Naming the dataset directly does not reach it either.
        refused = registry["create_dashboard"].executor(
            db,
            context,
            {
                "name": "stolen",
                "dataset_id": dataset["id"],
                "query": {"limit": 5},
            },
        )
        assert refused.content == "Error: Dataset not found"
    finally:
        db.rollback()
        db.close()

    _cleanup(client, [dashboard])


def test_update_dashboard_cannot_revise_another_tenants_dashboard(client, identity_client):
    """`update_dashboard` resolves a dashboard_id the model supplies, so it must
    scope that id to the caller's workspace — an injected agent that names another
    tenant's dashboard_id has to be refused, not handed the revision. `create` is
    covered above; this is the *write-by-id* path, guarded only by the workspace
    clause in `_find_dashboard`, and it is the whole guard once unrestricted dev
    mode has dropped the per-subject scoping that would otherwise hide the tool.
    """
    dataset = make_dataset(client)
    victim = _dashboard(client, dataset, unique("Victim"))
    outsider = identity_client(workspace_name="Elsewhere")

    db = SessionLocal()
    try:
        context = ToolContext(
            workspace_id=outsider.identity.workspace_id,
            user_id=outsider.identity.user_id,
            conversation_id="",
        )
        registry = build_registry(db, context)
        refused = registry["update_dashboard"].executor(
            db, context, {"dashboard_id": victim["id"], "name": "hijacked"}
        )
        # The None branch of _update_dashboard, reachable only once the foreign
        # id has been scoped out of the caller's workspace.
        assert "no such dashboard in this workspace" in refused.content.lower()
    finally:
        db.rollback()
        db.close()

    _cleanup(client, [victim])


# --------------------------------------------------------------------------


def _dashboard(client, dataset: dict, name: str) -> dict:
    response = client.post(
        "/api/dashboards",
        headers=key(),
        json={
            "name": name,
            "description": "",
            "dataset_id": dataset["id"],
            "spec": {
                "visualization": "bar",
                "query": {
                    "group_by": "territory",
                    "metrics": [
                        {"field": "revenue", "operation": "sum", "label": "total"}
                    ],
                    "limit": 10,
                },
                "x_field": "territory",
                "y_fields": ["total"],
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _cleanup(client, dashboards: list[dict]) -> None:
    for dashboard in dashboards:
        client.delete(f"/api/dashboards/{dashboard['id']}")
