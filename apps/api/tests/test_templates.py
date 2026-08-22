"""Space templates, workflow templates, and duplicate-dashboard.

Every test here is about the same three promises. A template is a *snapshot* —
instantiating one copies content forward through the ordinary create path, so
nothing a template makes could not have been made by hand. A workflow template
can never smuggle a schedule — the copy is a draft with an empty cron and a
None claim, whatever the graph's trigger says. And a foreign id anywhere in
the request is a uniform 404, indistinguishable from a missing one.

Schema tests at the bottom follow test_workflow_schema.py: the migration chain
on a scratch database must build exactly what the ORM declares.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app.database import SessionLocal, engine
from app.models import SpaceTemplate, Workflow, WorkflowTemplate

TEMPLATE_TABLES = ("space_templates", "workflow_templates")
API_ROOT = Path(__file__).resolve().parents[1]

CSV = "territory,revenue\nNorth,10\nSouth,20\nNorth,30\n"

SPEC = {
    "visualization": "bar",
    "query": {
        "group_by": "territory",
        "metrics": [{"field": "revenue", "operation": "sum", "label": "total"}],
        "limit": 10,
    },
    "x_field": "territory",
    "y_fields": ["total"],
}

GRAPH = {
    "name": "Weekly digest",
    "description": "Summarise the sources.",
    "trigger": {"kind": "manual", "cron": "", "timezone": "UTC"},
    "nodes": [
        {
            "id": "gather",
            "kind": "tool",
            "tool": "search_sources",
            "arguments": {"query": "digest"},
            "description": "Find the passages.",
        }
    ],
    "edges": [],
}


def key() -> dict[str, str]:
    return {"Idempotency-Key": "tmpl-" + uuid.uuid4().hex}


def unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


@pytest.fixture
def workspace(client):
    identity = client.get("/api/bootstrap").json()["identity"]
    return identity["workspace_id"], identity["user_id"]


def make_space(client, *, instructions: str = "") -> dict:
    response = client.post(
        "/api/spaces",
        headers=key(),
        json={"name": unique("Clients"), "instructions": instructions},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_workflow_row(workspace, *, graph: dict | None = None) -> str:
    """A stored automation, written directly: compilation is another file's
    subject, and the template routes only ever read `graph_json` verbatim."""
    workspace_id, user_id = workspace
    db = SessionLocal()
    try:
        workflow = Workflow(
            workspace_id=workspace_id,
            created_by=user_id,
            name=unique("Digest"),
            source_prompt="summarise the sources weekly",
            graph_json=json.dumps(graph if graph is not None else GRAPH),
            status="draft",
        )
        db.add(workflow)
        db.commit()
        return workflow.id
    finally:
        db.close()


def make_dashboard(client) -> dict:
    upload = client.post(
        "/api/sources",
        headers=key(),
        files={"file": ("deals.csv", CSV.encode(), "text/csv")},
    )
    assert upload.status_code == 202, upload.text
    dataset = client.post(
        "/api/datasets",
        headers=key(),
        json={
            "name": unique("Deals"),
            "description": "",
            "source_id": upload.json()["id"],
        },
    )
    assert dataset.status_code == 201, dataset.text
    response = client.post(
        "/api/dashboards",
        headers=key(),
        json={
            "name": unique("Revenue"),
            "description": "Sum of revenue",
            "dataset_id": dataset.json()["id"],
            "spec": SPEC,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------
# Space templates


def test_a_space_template_snapshots_the_spaces_instructions(client):
    space = make_space(client, instructions="Always answer in French.")
    response = client.post(
        "/api/space-templates",
        headers=key(),
        json={
            "name": unique("Client playbook"),
            "description": "How we set up client spaces",
            "from_space_id": space["id"],
            # Ignored on the snapshot path: the space's own instructions win.
            "instructions": "not this",
        },
    )
    assert response.status_code == 201, response.text
    template = response.json()
    assert template["instructions"] == "Always answer in French."

    listed = client.get("/api/space-templates").json()
    assert template["id"] in {item["id"] for item in listed}

    # A snapshot, not a link: editing the space afterwards moves nothing.
    client.patch(f"/api/spaces/{space['id']}", json={"instructions": "changed"})
    unchanged = next(
        item
        for item in client.get("/api/space-templates").json()
        if item["id"] == template["id"]
    )
    assert unchanged["instructions"] == "Always answer in French."


def test_instantiating_a_space_template_creates_a_space_with_its_instructions(
    client,
):
    space = make_space(client, instructions="Cite every claim.")
    created = client.post(
        "/api/space-templates",
        headers=key(),
        json={"name": unique("Research playbook"), "from_space_id": space["id"]},
    ).json()

    name = unique("Acme research")
    response = client.post(
        f"/api/space-templates/{created['id']}/instantiate",
        headers=key(),
        json={"name": name},
    )
    assert response.status_code == 201, response.text
    made = response.json()
    assert made["name"] == name
    assert made["instructions"] == "Cite every claim."
    assert made["id"] != space["id"]
    # It went through the ordinary create path, so the rail lists it.
    assert made["id"] in {item["id"] for item in client.get("/api/spaces").json()}


def test_a_foreign_from_space_id_is_a_404_not_a_snapshot(client, identity_client):
    other = identity_client()
    foreign_space = make_space(other, instructions="their secret playbook")
    response = client.post(
        "/api/space-templates",
        headers=key(),
        json={"name": unique("Stolen"), "from_space_id": foreign_space["id"]},
    )
    assert response.status_code == 404, response.text
    assert "their secret playbook" not in response.text


def test_an_unknown_agent_id_in_a_space_template_is_a_404(client, identity_client):
    other = identity_client()
    foreign_agent = other.get("/api/bootstrap").json()["default_agent_id"]
    response = client.post(
        "/api/space-templates",
        headers=key(),
        json={"name": unique("Agents"), "agent_ids": [foreign_agent]},
    )
    assert response.status_code == 404, response.text


def test_a_taken_space_template_name_is_a_409(client):
    name = unique("Playbook")
    first = client.post(
        "/api/space-templates", headers=key(), json={"name": name}
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/api/space-templates", headers=key(), json={"name": name}
    )
    assert second.status_code == 409, second.text


def test_deleting_a_space_template_leaves_the_spaces_it_made_alone(client):
    template = client.post(
        "/api/space-templates",
        headers=key(),
        json={"name": unique("Ephemeral"), "instructions": "keep calm"},
    ).json()
    made = client.post(
        f"/api/space-templates/{template['id']}/instantiate",
        headers=key(),
        json={"name": unique("Made")},
    ).json()
    deleted = client.delete(f"/api/space-templates/{template['id']}")
    assert deleted.status_code == 204, deleted.text
    assert template["id"] not in {
        item["id"] for item in client.get("/api/space-templates").json()
    }
    still_there = client.get(f"/api/spaces/{made['id']}")
    assert still_there.status_code == 200
    assert still_there.json()["instructions"] == "keep calm"


# --------------------------------------------------------------------------
# Workflow templates


def test_a_workflow_template_snapshots_graph_and_prompt(client, workspace):
    workflow_id = make_workflow_row(workspace)
    response = client.post(
        "/api/workflow-templates",
        headers=key(),
        json={"name": unique("Digest shape"), "from_workflow_id": workflow_id},
    )
    assert response.status_code == 201, response.text
    template = response.json()
    assert template["source_prompt"] == "summarise the sources weekly"
    assert [node["id"] for node in template["graph"]["nodes"]] == ["gather"]
    assert template["id"] in {
        item["id"] for item in client.get("/api/workflow-templates").json()
    }


def test_a_foreign_from_workflow_id_is_a_404(client, identity_client):
    other = identity_client()
    other_identity = other.get("/api/bootstrap").json()["identity"]
    foreign_workflow = make_workflow_row(
        (other_identity["workspace_id"], other_identity["user_id"])
    )
    response = client.post(
        "/api/workflow-templates",
        headers=key(),
        json={"name": unique("Stolen"), "from_workflow_id": foreign_workflow},
    )
    assert response.status_code == 404, response.text


def test_instantiating_a_workflow_template_can_never_self_fire(client, workspace):
    """The sharpest promise in this file: however scheduled the source workflow
    was, the copy is a draft with no cron and no dispatch claim — a person has
    to review and activate it before the ticker can ever see it."""
    workspace_id, user_id = workspace
    scheduled = dict(GRAPH, trigger={"kind": "schedule", "cron": "0 9 * * 1", "timezone": "UTC"})
    workflow_id = make_workflow_row(workspace, graph=scheduled)
    db = SessionLocal()
    try:
        source = db.get(Workflow, workflow_id)
        source.trigger_kind = "schedule"
        source.schedule_cron = "0 9 * * 1"
        source.status = "active"
        db.commit()
    finally:
        db.close()

    template = client.post(
        "/api/workflow-templates",
        headers=key(),
        json={"name": unique("Scheduled shape"), "from_workflow_id": workflow_id},
    ).json()
    response = client.post(
        f"/api/workflow-templates/{template['id']}/instantiate",
        headers=key(),
        json={"name": unique("Copied digest")},
    )
    assert response.status_code == 201, response.text
    made = response.json()
    assert made["status"] == "draft"
    assert made["trigger_kind"] == "manual"
    assert made["schedule_cron"] == ""
    assert made["last_dispatched_at"] is None
    # The graph itself is the verbatim snapshot — drift stays visible.
    assert made["graph"]["trigger"]["cron"] == "0 9 * * 1"

    db = SessionLocal()
    try:
        row = db.get(Workflow, made["id"])
        assert row is not None
        assert row.workspace_id == workspace_id
        assert row.created_by == user_id
        assert row.schedule_cron == ""
        assert row.last_dispatched_at is None
    finally:
        db.close()


def test_instantiating_a_template_revalidates_the_stored_graph(client, workspace):
    """A snapshot saved under an older schema fails at the click, legibly —
    not at 3am inside the executor."""
    workspace_id, user_id = workspace
    db = SessionLocal()
    try:
        broken = WorkflowTemplate(
            workspace_id=workspace_id,
            created_by=user_id,
            name=unique("Broken"),
            graph_json=json.dumps({"nodes": "not-a-list"}),
        )
        db.add(broken)
        db.commit()
        template_id = broken.id
    finally:
        db.close()
    response = client.post(
        f"/api/workflow-templates/{template_id}/instantiate",
        headers=key(),
        json={"name": ""},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["errors"], response.text


def test_a_taken_workflow_template_name_is_a_409(client, workspace):
    workflow_id = make_workflow_row(workspace)
    name = unique("Digest shape")
    first = client.post(
        "/api/workflow-templates",
        headers=key(),
        json={"name": name, "from_workflow_id": workflow_id},
    )
    assert first.status_code == 201, first.text
    second = client.post(
        "/api/workflow-templates",
        headers=key(),
        json={"name": name, "from_workflow_id": workflow_id},
    )
    assert second.status_code == 409, second.text


# --------------------------------------------------------------------------
# Duplicate dashboard


def test_duplicating_a_dashboard_copies_the_definition_under_a_new_name(client):
    dashboard = make_dashboard(client)
    response = client.post(
        f"/api/dashboards/{dashboard['id']}/duplicate", headers=key()
    )
    assert response.status_code == 201, response.text
    copy = response.json()
    assert copy["id"] != dashboard["id"]
    assert copy["name"] == f"{dashboard['name']} copy"
    assert copy["spec"] == dashboard["spec"]
    assert copy["dataset_id"] == dashboard["dataset_id"]
    assert copy["template_id"] == dashboard["template_id"]
    assert copy["bindings"] == dashboard["bindings"]
    # The copy is a real dashboard: it runs.
    run = client.post(f"/api/dashboards/{copy['id']}/run")
    assert run.status_code == 200, run.text

    # A second duplicate wants the same "<name> copy" claim: 409, not a third row.
    again = client.post(
        f"/api/dashboards/{dashboard['id']}/duplicate", headers=key()
    )
    assert again.status_code == 409, again.text


def test_duplicating_a_foreign_dashboard_is_a_404(client, identity_client):
    other = identity_client()
    foreign = make_dashboard(other)
    response = client.post(
        f"/api/dashboards/{foreign['id']}/duplicate", headers=key()
    )
    assert response.status_code == 404, response.text


def test_duplicate_replays_return_the_same_copy(client):
    dashboard = make_dashboard(client)
    idem = key()
    first = client.post(f"/api/dashboards/{dashboard['id']}/duplicate", headers=idem)
    assert first.status_code == 201, first.text
    replay = client.post(f"/api/dashboards/{dashboard['id']}/duplicate", headers=idem)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]


# --------------------------------------------------------------------------
# Schema: the migration and the ORM must agree (test_workflow_schema pattern)


def test_every_template_table_is_workspace_scoped():
    for model in (SpaceTemplate, WorkflowTemplate):
        columns = model.__table__.columns
        assert "workspace_id" in columns, model.__tablename__
        assert not columns["workspace_id"].nullable, model.__tablename__


def test_the_migration_chain_builds_the_template_tables_the_orm_declares():
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
        assert set(TEMPLATE_TABLES) <= set(migrated.get_table_names())
        declared = inspect(engine)
        for table in TEMPLATE_TABLES:
            assert {column["name"] for column in migrated.get_columns(table)} == {
                column["name"] for column in declared.get_columns(table)
            }, table
            assert {index["name"] for index in migrated.get_indexes(table)} >= {
                index["name"] for index in declared.get_indexes(table)
            }, table
