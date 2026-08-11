from __future__ import annotations

import json
import uuid


def key() -> dict[str, str]:
    return {"Idempotency-Key": "expansion-" + uuid.uuid4().hex}


def upload(client, filename: str, content: str, media_type: str) -> dict:
    response = client.post(
        "/api/sources",
        headers=key(),
        files={"file": (filename, content.encode(), media_type)},
    )
    assert response.status_code == 202, response.text
    source = response.json()
    refreshed = next(
        item for item in client.get("/api/sources").json() if item["id"] == source["id"]
    )
    assert refreshed["status"] == "ready", refreshed
    return refreshed


def test_graph_projection_is_rebuildable_and_has_provenance(client):
    source = upload(
        client,
        "graph-fixture.md",
        (
            "Project Northstar is owned by Maya Chen at Atlas Labs. "
            "Maya Chen coordinates Project Northstar with Atlas Labs.\n\n"
            "Project Northstar targets faster onboarding for Acme Support."
        ),
        "text/markdown",
    )
    response = client.get("/api/graph")
    assert response.status_code == 200
    graph = response.json()
    assert graph["status"] == "ready"
    assert graph["version"]
    assert graph["entities"]
    assert any(source["id"] in entity["source_ids"] for entity in graph["entities"])
    assert graph["edges"]

    first_version = graph["version"]
    rebuild = client.post("/api/graph/rebuild", headers=key())
    assert rebuild.status_code == 202
    rebuilt = client.get("/api/graph").json()
    assert rebuilt["status"] == "ready"
    assert rebuilt["version"] == first_version


def test_reuploading_a_corrected_file_versions_the_dataset_it_already_named(client):
    """The exact sequence the web app runs when a tabular source lands twice.

    A dataset is auto-created per ready CSV/JSON source, keyed on the file's
    base name. So a corrected re-upload arrives as a *new* source with a name
    the workspace already holds — and `POST /api/datasets` answers that with
    409, which the browser swallowed. The correction became a source nobody
    queried while every dashboard kept reading the wrong numbers.

    Versioning is what makes the second upload mean something, so this pins the
    whole path: the collision is still a 409, the version endpoint accepts the
    *new* source, and a query afterwards returns the corrected rows.
    """
    name = "Sales " + uuid.uuid4().hex[:8]
    first = upload(
        client,
        "sales.csv",
        "team,revenue\nNorth,10\n",
        "text/csv",
    )
    created = client.post(
        "/api/datasets",
        headers=key(),
        json={"name": name, "description": "", "source_id": first["id"]},
    )
    assert created.status_code == 201, created.text
    dataset = created.json()
    assert dataset["current_version"] == 1

    corrected = upload(
        client,
        "sales.csv",
        "team,revenue\nNorth,99\n",
        "text/csv",
    )
    # Why the UI cannot simply create a second dataset: the name is taken.
    collision = client.post(
        "/api/datasets",
        headers=key(),
        json={"name": name, "description": "", "source_id": corrected["id"]},
    )
    assert collision.status_code == 409, collision.text

    versioned = client.post(
        f"/api/datasets/{dataset['id']}/versions",
        headers=key(),
        json={"source_id": corrected["id"]},
    )
    assert versioned.status_code == 201, versioned.text
    after = versioned.json()
    assert after["id"] == dataset["id"]
    assert after["current_version"] == 2
    assert after["source_id"] == corrected["id"]
    assert after["content_hash"] != dataset["content_hash"]

    # The point of the whole exercise: queries read the correction.
    result = client.post(
        f"/api/datasets/{dataset['id']}/query",
        json={
            "group_by": "team",
            "metrics": [{"field": "revenue", "operation": "sum", "label": "total"}],
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["rows"] == [{"team": "North", "total": 99}]

    # And exactly one dataset carries that name, not two.
    named = [item for item in client.get("/api/datasets").json() if item["name"] == name]
    assert len(named) == 1
    assert named[0]["current_version"] == 2


def test_dataset_dashboard_and_generated_app_release_flow(client):
    source = upload(
        client,
        "revenue.csv",
        "team,month,revenue\nNorth,2026-01,10\nSouth,2026-01,20\nNorth,2026-02,15\n",
        "text/csv",
    )
    dataset_response = client.post(
        "/api/datasets",
        headers=key(),
        json={
            "name": "Revenue " + uuid.uuid4().hex[:8],
            "description": "Bounded revenue fixture",
            "source_id": source["id"],
        },
    )
    assert dataset_response.status_code == 201, dataset_response.text
    dataset = dataset_response.json()
    assert dataset["row_count"] == 3
    assert {column["name"] for column in dataset["columns"]} == {
        "team",
        "month",
        "revenue",
    }
    assert next(
        column for column in dataset["columns"] if column["name"] == "revenue"
    )["type"] == "integer"

    version_key = key()
    version_response = client.post(
        f"/api/datasets/{dataset['id']}/versions",
        headers=version_key,
        json={"source_id": source["id"]},
    )
    assert version_response.status_code == 201, version_response.text
    assert version_response.json()["current_version"] == 2
    replayed_version = client.post(
        f"/api/datasets/{dataset['id']}/versions",
        headers=version_key,
        json={"source_id": source["id"]},
    )
    assert replayed_version.status_code == 201
    assert replayed_version.json()["current_version"] == 2

    query = {
        "group_by": "team",
        "metrics": [{"field": "revenue", "operation": "sum", "label": "total"}],
        "order_by": "total",
        "order_direction": "desc",
        "limit": 10,
    }
    query_response = client.post(
        f"/api/datasets/{dataset['id']}/query",
        json=query,
    )
    assert query_response.status_code == 200, query_response.text
    result = query_response.json()
    assert result["columns"] == ["team", "total"]
    assert result["rows"] == [
        {"team": "North", "total": 25},
        {"team": "South", "total": 20},
    ]

    bad_query = client.post(
        f"/api/datasets/{dataset['id']}/query",
        json={"group_by": 'team"; DROP TABLE datasets;--'},
    )
    assert bad_query.status_code == 422

    dashboard_response = client.post(
        "/api/dashboards",
        headers=key(),
        json={
            "name": "Revenue by team " + uuid.uuid4().hex[:8],
            "description": "Revenue totals",
            "dataset_id": dataset["id"],
            "spec": {
                "visualization": "bar",
                "query": query,
                "x_field": "team",
                "y_fields": ["total"],
            },
        },
    )
    assert dashboard_response.status_code == 201, dashboard_response.text
    dashboard = dashboard_response.json()
    run = client.post(f"/api/dashboards/{dashboard['id']}/run")
    assert run.status_code == 200
    assert run.json()["result"]["rows"][0]["total"] == 25

    slug = "revenue-" + uuid.uuid4().hex[:10]
    app_response = client.post(
        "/api/apps",
        headers=key(),
        json={
            "name": "Revenue board",
            "slug": slug,
            "description": "Static release snapshot",
            "visibility": "public",
            "dashboard_ids": [dashboard["id"]],
        },
    )
    assert app_response.status_code == 201, app_response.text
    app = app_response.json()
    assert app["current_release_id"] is None
    release = app["releases"][0]
    assert release["status"] == "draft"
    assert release["manifest"]["dashboards"][0]["result"]["rows"][0]["total"] == 25

    unpublished = client.get(f"/published/apps/{slug}")
    assert unpublished.status_code == 404
    published = client.post(
        f"/api/apps/{app['id']}/releases/{release['id']}/publish",
        headers=key(),
    )
    assert published.status_code == 200
    assert published.json()["current_release_id"] == release["id"]
    public = client.get(f"/published/apps/{slug}")
    assert public.status_code == 200
    assert public.json()["manifest"]["dashboards"][0]["name"] == dashboard["name"]

    second_release = client.post(
        f"/api/apps/{app['id']}/releases",
        headers=key(),
        json={"dashboard_ids": [dashboard["id"]]},
    )
    assert second_release.status_code == 201
    release_two = second_release.json()["releases"][0]
    assert release_two["version"] == 2
    published_two = client.post(
        f"/api/apps/{app['id']}/releases/{release_two['id']}/publish",
        headers=key(),
    )
    assert published_two.status_code == 200
    assert published_two.json()["current_release_id"] == release_two["id"]
    rolled_back = client.post(
        f"/api/apps/{app['id']}/rollback/{release['id']}",
        headers=key(),
    )
    assert rolled_back.status_code == 200
    assert rolled_back.json()["current_release_id"] == release["id"]


def test_expansion_endpoints_enforce_workspace_scope(client, identity_client):
    # The isolated tenant is a real signed-in user with their own session, not a
    # pair of headers: identity comes from the session cookie now, so claiming
    # someone else's user id is no longer something a request can even express.
    isolated = identity_client(name="Isolated user", workspace_name="Isolated workspace")
    workspace_id = isolated.identity.workspace_id

    assert isolated.get("/api/graph").json()["entities"] == []
    assert isolated.get("/api/datasets").json() == []
    assert isolated.get("/api/dashboards").json() == []
    assert isolated.get("/api/apps").json() == []

    seed_graph = client.get("/api/graph")
    assert seed_graph.status_code == 200
    assert all(
        workspace_id not in json.dumps(entity)
        for entity in seed_graph.json()["entities"]
    )
