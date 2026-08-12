"""The HTTP surface over workspace-defined sandbox tools (0036).

Two things are proved. The ordinary one: CRUD works and the create-time
invariants hold — a slug cannot shadow a builtin, an argv placeholder cannot
reference a parameter the schema never declared, and an egress entry must be a
bare hostname. The tenancy one: a custom tool is workspace data, so a stranger
neither sees another tenant's tools in a listing nor can reach one by id (the
strict isolation suite drives every route cross-tenant; this file keeps a
focused CRUD-scoping assertion next to the happy paths).

`SANDBOX_ENABLED` is turned on for the module because the reserved-name check is
computed live from the registry — `run_python` only exists to collide with when
there is an execution provider behind it.

The last test is ORM↔migration parity: `create_all` builds development and test
schemas, alembic builds production, and a column that exists in only one is a
bug that appears the first time the two are asked to be the same table.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, create_identity
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from app.config import get_settings
from app.database import SessionLocal, engine
from app.main import app
from app.models import SandboxTool

API_ROOT = Path(__file__).resolve().parents[1]
MISSING = "00000000-0000-4000-8000-0000000000ff"


@pytest.fixture(autouse=True)
def _sandbox_enabled():
    """The reserved-name check reads the live registry, and the builtins only
    populate it when a provider is configured — so enable the fake one."""
    from app.services.sandbox.provider import reset_provider_cache

    settings = get_settings()
    was_enabled, was_provider = settings.sandbox_enabled, settings.sandbox_provider
    settings.sandbox_enabled = True
    settings.sandbox_provider = "fake"
    reset_provider_cache()
    yield
    settings.sandbox_enabled, settings.sandbox_provider = was_enabled, was_provider
    reset_provider_cache()


@pytest.fixture(autouse=True)
def _clean_tools():
    """A custom tool is a workspace row; one test's leftovers are another's name
    collision, so the table is emptied around each test."""
    yield
    db = SessionLocal()
    try:
        db.query(SandboxTool).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture
def stranger():
    """A second workspace, so "not yours" can be told apart from "not there"."""
    identity = create_identity(name="Tool stranger", workspace_name="Other tools workspace")
    other = TestClient(app, base_url=TEST_BASE_URL)
    return authenticate(
        other,
        Identity(
            user_id=identity.user_id,
            workspace_id=identity.workspace_id,
            token=identity.token,
            csrf_token=identity.csrf_token,
        ),
    )


def _payload(**overrides) -> dict:
    body = {
        "name": "fetch_url",
        "description": "Fetch a URL",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}},
        "argv": ["curl", "-s", "{{url}}"],
        "egress_hosts": ["api.example.com"],
        "approval": "inherit",
        "enabled": True,
    }
    body.update(overrides)
    return body


def _create(caller, **overrides) -> dict:
    response = caller.post("/api/sandbox-tools", json=_payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()


# --- CRUD, scoped to the workspace ----------------------------------------


def test_create_round_trips_every_field(client):
    body = _create(client)
    assert body["name"] == "fetch_url"
    assert body["argv"] == ["curl", "-s", "{{url}}"]
    assert body["egress_hosts"] == ["api.example.com"]
    assert body["approval"] == "inherit"
    assert body["enabled"] is True
    assert body["input_schema"]["properties"] == {"url": {"type": "string"}}
    assert "id" in body


def test_list_returns_only_the_callers_tools(client, stranger):
    mine = _create(client, name="mine_tool")
    theirs = _create(stranger, name="their_tool")
    names = {row["name"] for row in client.get("/api/sandbox-tools").json()}
    ids = {row["id"] for row in client.get("/api/sandbox-tools").json()}
    assert "mine_tool" in names
    assert theirs["id"] not in ids
    assert mine["id"] in ids


def test_update_edits_the_row(client):
    tool = _create(client)
    patched = client.patch(
        f"/api/sandbox-tools/{tool['id']}",
        json={"description": "now with feeling", "approval": "always", "enabled": False},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["description"] == "now with feeling"
    assert body["approval"] == "always"
    assert body["enabled"] is False


def test_delete_removes_the_row(client):
    tool = _create(client)
    assert client.delete(f"/api/sandbox-tools/{tool['id']}").status_code == 204
    remaining = {row["id"] for row in client.get("/api/sandbox-tools").json()}
    assert tool["id"] not in remaining


def test_a_name_is_unique_within_a_workspace_but_free_across_them(client, stranger):
    _create(client, name="dup")
    clash = client.post("/api/sandbox-tools", json=_payload(name="dup"))
    assert clash.status_code == 409, clash.text
    # The same slug in another workspace is a different tool entirely.
    assert stranger.post("/api/sandbox-tools", json=_payload(name="dup")).status_code == 201


def test_a_foreign_tool_id_is_indistinguishable_from_a_missing_one(client, stranger):
    """CRUD is workspace-scoped: a stranger's tool id must 404 exactly as an id
    that never existed, so the status cannot be used to confirm the id is real."""
    theirs = _create(stranger, name="secret_tool")
    for method, suffix, body in (
        ("PATCH", "", {"description": "x"}),
        ("DELETE", "", None),
    ):
        foreign = client.request(
            method, f"/api/sandbox-tools/{theirs['id']}{suffix}", json=body
        )
        absent = client.request(
            method, f"/api/sandbox-tools/{MISSING}{suffix}", json=body
        )
        assert foreign.status_code == 404, foreign.text
        assert foreign.status_code == absent.status_code
    # And the victim's tool is untouched.
    assert stranger.get("/api/sandbox-tools").json()[0]["description"] == "Fetch a URL"


# --- create-time invariants -----------------------------------------------


def test_a_custom_name_cannot_shadow_a_builtin(client):
    """`build_registry` composes the custom family last, so a custom `run_python`
    would silently replace the real one. The route refuses it, and the reserved
    set is read from the live registry so it cannot drift."""
    refused = client.post("/api/sandbox-tools", json=_payload(name="run_python"))
    assert refused.status_code == 422
    assert "built-in" in refused.text


def test_an_argv_placeholder_must_reference_a_declared_parameter(client):
    refused = client.post(
        "/api/sandbox-tools",
        json=_payload(
            input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
            argv=["curl", "{{host}}"],  # {{host}} is not a declared property
        ),
    )
    assert refused.status_code == 422
    assert "host" in refused.text


def test_egress_hosts_must_be_bare_hostnames(client):
    for bad in (["https://api.example.com"], ["api.example.com:443"], ["a b.com"], ["a/b"]):
        refused = client.post("/api/sandbox-tools", json=_payload(egress_hosts=bad))
        assert refused.status_code == 422, (bad, refused.text)


def test_a_bad_slug_is_refused(client):
    for bad in ("Fetch URL", "fetch url", "UPPER", "-lead", "trail-"):
        refused = client.post("/api/sandbox-tools", json=_payload(name=bad))
        assert refused.status_code == 422, (bad, refused.text)


def test_input_schema_must_be_an_object_schema(client):
    refused = client.post(
        "/api/sandbox-tools",
        json=_payload(input_schema={"type": "string"}, argv=["echo", "hi"]),
    )
    assert refused.status_code == 422


# --- ORM <-> migration parity ---------------------------------------------


def test_the_migration_chain_builds_the_table_the_orm_declares() -> None:
    """`alembic upgrade head` from an empty database must match `create_all`.

    The whole chain on a scratch database, because a migration that only works on
    top of a schema someone already had fails on the first new deployment. Then
    `sandbox_tools` is compared column-for-column and index-for-index against the
    ORM: production gets the alembic schema and development the metadata one, and
    a difference between them is a bug that only ever appears in production.
    """
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
        assert "sandbox_tools" in migrated.get_table_names()
        assert {c["name"] for c in migrated.get_columns("sandbox_tools")} == {
            c["name"] for c in declared.get_columns("sandbox_tools")
        }
        assert {i["name"] for i in migrated.get_indexes("sandbox_tools")} >= {
            i["name"] for i in declared.get_indexes("sandbox_tools")
        }
        # The uniqueness that makes a slug the tool the model calls survives the
        # migration too — a duplicate name in one workspace must be refusable.
        migrated_unique = {
            tuple(c["column_names"])
            for c in migrated.get_unique_constraints("sandbox_tools")
        }
        assert ("workspace_id", "name") in migrated_unique
