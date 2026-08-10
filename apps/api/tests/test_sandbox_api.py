"""The HTTP surface over sandbox sessions.

Two things are being proved here. The first is the ordinary one: each route does
what it says, and the executions table ends up holding what ran. The second is
the one ADR 0005 actually cares about — a session id is only meaningful inside
the workspace that owns it, so every route that takes one is driven twice, once
by its owner and once by a stranger, and the stranger must get the same answer
they would get for an id that never existed.

The whole file runs on `SANDBOX_PROVIDER=fake`, which executes nothing. That is
the point of the driver seam: the routing, the tenancy filter, the quota and the
error mapping are all exercised with no key and no network.
"""
from __future__ import annotations

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, create_identity
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import SandboxExecution, SandboxSession

MISSING = "00000000-0000-4000-8000-0000000000ff"


@pytest.fixture(autouse=True)
def _fake_provider():
    """Pin the driver for this module regardless of how the process was started.

    Drivers are cached on (provider, key, template), so the cache is dropped
    around the change — otherwise a provider built earlier in the run would be
    handed back and the setting would look ignored. Dropping it afterwards
    matters too: `FakeProvider` keeps its sandboxes in the instance, so one that
    outlives this module carries them into the next test.
    """
    from app.services.sandbox.provider import reset_provider_cache

    settings = get_settings()
    # SANDBOX_ENABLED defaults to off, because every deployment predating this
    # feature lacks a provider key. These routes only exist when it is on.
    was_enabled, was_provider = settings.sandbox_enabled, settings.sandbox_provider
    settings.sandbox_enabled = True
    settings.sandbox_provider = "fake"
    reset_provider_cache()
    yield
    settings.sandbox_enabled, settings.sandbox_provider = was_enabled, was_provider
    reset_provider_cache()


@pytest.fixture(autouse=True)
def _clean_sessions():
    """Sessions are workspace-scoped rows, and the concurrency quota counts them,
    so one test's leftovers are the next test's 429."""
    yield
    db = SessionLocal()
    try:
        db.query(SandboxExecution).delete()
        db.query(SandboxSession).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture
def stranger():
    """A second workspace, so "not yours" can be told apart from "not there"."""
    identity = create_identity(name="Sandbox stranger", workspace_name="Other workspace")
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


def create_session(caller, **payload) -> dict:
    response = caller.post("/api/sandbox", json={"project_id": "", "label": "", **payload})
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------
# Happy paths


def test_create_returns_a_session_with_its_egress_policy(client):
    body = create_session(client, label="analysis")
    assert body["status"] == "running"
    assert body["label"] == "analysis"
    assert body["network_policy"] in {"open", "allowlist", "none"}
    assert body["exec_count"] == 0
    # The provider-side id is the one value that would let a leaked response
    # address a live machine, so it must not be in the payload at all.
    assert "external_id" not in body


def test_create_reuses_the_session_for_the_same_project(client):
    """Reloading the panel must not boot (and bill for) a second microVM."""
    first = create_session(client, project_id="proj-1")
    second = create_session(client, project_id="proj-1")
    assert first["id"] == second["id"]

    listed = client.get("/api/sandbox")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [first["id"]]


def test_list_shows_only_the_callers_sessions(client, stranger):
    mine = create_session(client)
    theirs = create_session(stranger)
    ids = [row["id"] for row in client.get("/api/sandbox").json()]
    assert mine["id"] in ids
    assert theirs["id"] not in ids


def test_run_executes_and_lands_in_the_history(client):
    session = create_session(client)
    response = client.post(
        f"/api/sandbox/{session['id']}/run",
        json={"source": "print('hello')", "kind": "code", "language": "python"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["execution"]["kind"] == "code"
    assert body["execution"]["source"] == "print('hello')"
    assert isinstance(body["artifacts"], list)
    # The session travels back with the result so the panel can update its
    # counters without a second request.
    assert body["session"]["exec_count"] == 1

    history = client.get(f"/api/sandbox/{session['id']}/executions")
    assert history.status_code == 200
    assert [row["id"] for row in history.json()] == [body["execution"]["id"]]


def test_run_refuses_an_empty_body_before_reaching_the_provider(client):
    session = create_session(client)
    response = client.post(f"/api/sandbox/{session['id']}/run", json={"source": ""})
    assert response.status_code == 422


def test_pause_then_kill_walks_the_session_to_its_terminal_state(client):
    session = create_session(client)
    paused = client.post(f"/api/sandbox/{session['id']}/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == "paused"

    killed = client.delete(f"/api/sandbox/{session['id']}")
    assert killed.status_code == 200, killed.text
    assert killed.json()["status"] == "killed"

    # A killed machine cannot run anything, and saying so is the service layer's
    # job — the route must surface it rather than dispatching at a dead id.
    refused = client.post(
        f"/api/sandbox/{session['id']}/run", json={"source": "print(1)"}
    )
    assert refused.status_code == 502
    assert "killed" in refused.text


def test_the_concurrency_quota_answers_429_not_502(client):
    """A limit is not a fault: the UI has to tell the user which dial to turn."""
    settings = get_settings()
    original = settings.sandbox_max_concurrent_per_workspace
    settings.sandbox_max_concurrent_per_workspace = 1
    try:
        create_session(client, project_id="proj-a")
        response = client.post("/api/sandbox", json={"project_id": "proj-b"})
    finally:
        settings.sandbox_max_concurrent_per_workspace = original
    assert response.status_code == 429, response.text
    assert "limit" in response.text


# --------------------------------------------------------------------------
# Tenancy: a foreign session id is indistinguishable from an absent one


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("GET", "/executions"),
        ("POST", "/run"),
        ("POST", "/pause"),
        ("DELETE", ""),
    ],
)
def test_a_foreign_session_id_answers_404(client, stranger, method, suffix):
    theirs = create_session(stranger)
    body = {"source": "print('stolen')"} if suffix == "/run" else None

    foreign = client.request(
        method, f"/api/sandbox/{theirs['id']}{suffix}", json=body
    )
    absent = client.request(method, f"/api/sandbox/{MISSING}{suffix}", json=body)

    assert foreign.status_code == 404, foreign.text
    # Identical to the absent case, or the status itself confirms the id exists.
    assert foreign.status_code == absent.status_code
    assert theirs["id"] not in foreign.text


def test_a_refused_run_never_reaches_the_other_tenants_machine(client, stranger):
    """The 404 above proves the answer; this proves nothing happened on the way
    there — the victim's session must be untouched and its history empty."""
    theirs = create_session(stranger)
    client.post(f"/api/sandbox/{theirs['id']}/run", json={"source": "print(1)"})
    client.post(f"/api/sandbox/{theirs['id']}/pause")
    client.delete(f"/api/sandbox/{theirs['id']}")

    after = stranger.get("/api/sandbox").json()
    assert [row["status"] for row in after] == ["running"]
    assert after[0]["exec_count"] == 0
    assert stranger.get(f"/api/sandbox/{theirs['id']}/executions").json() == []
