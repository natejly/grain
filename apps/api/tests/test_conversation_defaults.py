"""Per-thread composer defaults: agent, model, effort.

Three columns on the Conversation, set by `PATCH /conversations/{id}/defaults`,
"" meaning "the deployment's own default". They exist so the composer can seed
its pickers when a thread reopens — per-pane session state used to evaporate.

The property worth defending is the one the column comments promise: these are
*seeds*, never policy. The run path must not read them, so what reached the
provider stays answerable from the Run row alone — a thread default that
silently steered a turn would be a fourth resolution layer nobody asked for.
"""
from __future__ import annotations

import os

import pytest
from conftest import Identity, create_identity
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import Run

TEST_BASE_URL = "http://testserver.local"


def _client_for(identity: Identity) -> TestClient:
    client = TestClient(app, base_url=TEST_BASE_URL)
    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, identity.token)
    client.headers[settings.csrf_header_name] = identity.csrf_token
    return client


def _key() -> dict[str, str]:
    return {"Idempotency-Key": "defaults-test-" + os.urandom(8).hex()}


@pytest.fixture
def tenant() -> TestClient:
    return _client_for(create_identity(name="Defaults tester"))


def _thread(client: TestClient) -> str:
    created = client.post(
        "/api/conversations", headers=_key(), json={"title": "Defaults thread"}
    )
    assert created.status_code == 201
    body = created.json()
    # A fresh thread carries no remembered choices.
    assert body["default_agent_id"] == ""
    assert body["default_model"] == ""
    assert body["default_effort"] == ""
    return body["id"]


def test_defaults_patch_names_only_what_it_changes(tenant):
    conversation_id = _thread(tenant)

    first = tenant.patch(
        f"/api/conversations/{conversation_id}/defaults",
        json={"default_model": "gpt-test", "default_effort": "high"},
    )
    assert first.status_code == 200
    assert first.json()["default_model"] == "gpt-test"
    assert first.json()["default_effort"] == "high"
    assert first.json()["default_agent_id"] == ""

    # A later PATCH of one field leaves the others standing: None means
    # untouched, and "" is a real value meaning "back to the default".
    second = tenant.patch(
        f"/api/conversations/{conversation_id}/defaults",
        json={"default_effort": ""},
    )
    assert second.status_code == 200
    assert second.json()["default_model"] == "gpt-test"
    assert second.json()["default_effort"] == ""

    # And the list echoes the same row, so a reopened thread can seed from it.
    listed = {c["id"]: c for c in tenant.get("/api/conversations").json()}
    assert listed[conversation_id]["default_model"] == "gpt-test"


def test_defaults_follow_thread_visibility(tenant):
    conversation_id = _thread(tenant)
    outsider = _client_for(create_identity(name="Outsider", workspace_name="Elsewhere"))
    # Invisible means unsettable — and 404, never 403, so the id's existence is
    # not confirmed to someone it is hidden from.
    assert (
        outsider.patch(
            f"/api/conversations/{conversation_id}/defaults",
            json={"default_model": "smuggled"},
        ).status_code
        == 404
    )


def test_an_oversized_value_is_refused(tenant):
    conversation_id = _thread(tenant)
    assert (
        tenant.patch(
            f"/api/conversations/{conversation_id}/defaults",
            json={"default_model": "x" * 121},
        ).status_code
        == 422
    )


def test_a_thread_default_never_reaches_the_run(tenant):
    """The contract the column comments promise: seeds, not policy.

    A turn sent with no controls must produce a Run whose requested_model is
    "" even when the thread holds a default — the composer applies defaults by
    *sending* them, so the Run row alone keeps answering "what was asked for".
    """
    conversation_id = _thread(tenant)
    tenant.patch(
        f"/api/conversations/{conversation_id}/defaults",
        json={"default_model": "thread-favorite", "default_effort": "high"},
    )
    sent = tenant.post(
        f"/api/conversations/{conversation_id}/messages",
        headers=_key(),
        json={"content": "hello there"},
    )
    assert sent.status_code == 202
    run_id = sent.json()["run"]["id"]
    db = SessionLocal()
    try:
        run = db.execute(select(Run).where(Run.id == run_id)).scalar_one()
        assert run.requested_model == ""
        assert run.requested_effort == ""
    finally:
        db.close()
