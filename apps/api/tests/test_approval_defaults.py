"""Which approval mode a brand-new thread starts in.

This is the one claim `tests/conftest.py` cannot make. The suite pins
`DEFAULT_APPROVAL_MODE=ask_writes` for its whole run, because almost everything
it tests — assignment, the inbox, digests, per-thread visibility, document
hunks — needs a call to park before there is anything to look at. That pin makes
the shipped default invisible to every other test in the tree, so it is asserted
here, against `Settings` itself, where it cannot be quietly satisfied by the
environment the rest of the suite runs in.

Two separate facts, and they are separate on purpose:

  1. The *shipped* default is `auto_writes` — a new thread runs tool calls
     without stopping to ask. That is a product decision, and a regression in it
     is a silent change in how much a person is asked to approve.
  2. A conversation *reads* the setting when it is created, rather than baking a
     literal in. That is what makes (1) a deployment's answer instead of ours,
     and it is what lets the suite pin the strict baseline at all.

A test that only checked (2) would pass just as happily with the default flipped
back, and one that only checked (1) would pass on a model that ignored it.
"""
from __future__ import annotations

import os

import pytest
from conftest import Identity, create_identity
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.main import app
from app.models import Conversation

TEST_BASE_URL = "http://testserver.local"


def _client_for(identity: Identity) -> TestClient:
    client = TestClient(app, base_url=TEST_BASE_URL)
    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, identity.token)
    client.headers[settings.csrf_header_name] = identity.csrf_token
    return client


def _key() -> dict[str, str]:
    return {"Idempotency-Key": "approval-default-" + os.urandom(8).hex()}


@pytest.fixture
def tenant() -> TestClient:
    return _client_for(create_identity(name="Approval default tester"))


def test_the_shipped_default_lets_tool_calls_run():
    """The product promise, read off the field's DECLARED default.

    Deliberately not `Settings().default_approval_mode`: every settings source
    outranks the declaration, so an instantiated Settings reports `ask_writes`
    here (conftest pins it) and would report whatever a developer's repo-root
    `.env` says on their machine. The question this test asks is narrower and is
    the one that actually ships — what a deployment gets when it configures
    nothing at all — and only the declaration answers it.
    """
    declared = Settings.model_fields["default_approval_mode"].default
    assert declared == "auto_writes"


def test_a_new_thread_is_created_in_the_configured_mode(tenant):
    """The column reads the setting rather than a literal of its own.

    Asserted against the suite's pinned value, so this fails both ways: if the
    model stops reading the setting it reports `auto_writes` here, and if the
    pin stops being applied every parking test in the tree goes quiet at once.
    """
    configured = get_settings().default_approval_mode
    assert configured == "ask_writes", "conftest pins the suite's baseline"

    created = tenant.post(
        "/api/conversations", headers=_key(), json={"title": "Fresh thread"}
    )
    assert created.status_code == 201
    conversation_id = created.json()["id"]

    # Read from the row and not only from the response: the API serialises
    # whatever the column holds, so a default applied at the wrong layer would
    # still round-trip through the payload and look right.
    db = SessionLocal()
    try:
        stored = db.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        ).scalar_one()
        assert stored.approval_mode == configured
    finally:
        db.close()


def test_an_explicit_mode_still_wins_over_the_default(tenant):
    """The default seeds a thread; it never re-decides one.

    The mode is per conversation precisely so it can answer what is being done
    right now, which is worth nothing if the deployment default reasserts itself
    later. Set it, and it stays set.
    """
    created = tenant.post(
        "/api/conversations", headers=_key(), json={"title": "Bypassed thread"}
    )
    conversation_id = created.json()["id"]

    switched = tenant.put(
        f"/api/conversations/{conversation_id}/approval-mode",
        headers=_key(),
        json={"mode": "auto_writes"},
    )
    assert switched.status_code == 200

    listed = tenant.get("/api/conversations").json()
    row = next(item for item in listed if item["id"] == conversation_id)
    assert row["approval_mode"] == "auto_writes"
