"""Conversation export: the whole transcript as one downloadable file.

The route rides the exact rails the reading UI rides — `resolve_visible` for
who may ask, `_message_out` + `_sender_names` for what comes back — so these
tests pin the two things that could still drift: the rendered formats (the
Markdown document and the JSON envelope) and the visibility doctrine on this
new door (a colleague's personal thread is indistinguishable from absent).

Cross-tenant 404s are the isolation sweep's job (`tests/isolation.py` carries
the export RouteCase); here one smoke assert keeps the claim honest without
duplicating the sweep.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import timedelta

from conftest import (
    DEV_SEED_USER_ID,
    DEV_SEED_WORKSPACE_ID,
    TEST_BASE_URL,
    create_identity,
    issue_session,
)
from fastapi.testclient import TestClient

from app.clock import utcnow
from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import Membership, Message, User


def key() -> dict[str, str]:
    return {"Idempotency-Key": "export-" + uuid.uuid4().hex}


def _member(workspace_id: str, *, name: str) -> tuple[TestClient, str]:
    """A fresh user placed in `workspace_id`, and a client authenticated as
    them — test_conversation_sharing's `_member` pattern."""
    db = SessionLocal()
    try:
        user = User(email=f"{os.urandom(6).hex()}@example.com", name=name)
        db.add(user)
        db.flush()
        db.add(Membership(workspace_id=workspace_id, user_id=user.id, role="member"))
        db.commit()
        user_id = user.id
    finally:
        db.close()
    token, csrf_token = issue_session(user_id)
    settings = get_settings()
    client = TestClient(app, base_url=TEST_BASE_URL)
    client.cookies.set(settings.session_cookie_name, token)
    client.headers[settings.csrf_header_name] = csrf_token
    return client, user_id


def _client_for(identity) -> TestClient:
    settings = get_settings()
    client = TestClient(app, base_url=TEST_BASE_URL)
    client.cookies.set(settings.session_cookie_name, identity.token)
    client.headers[settings.csrf_header_name] = identity.csrf_token
    return client


def _make_conversation(client: TestClient, title: str = "Export fixture") -> str:
    created = client.post("/api/conversations", headers=key(), json={"title": title})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _plant_messages(
    conversation_id: str,
    *,
    workspace_id: str = DEV_SEED_WORKSPACE_ID,
    sender_id: str = DEV_SEED_USER_ID,
) -> str:
    """A user prompt, an assistant answer with a citation, and a "/btw" aside
    (role user, run_id "") — planted directly, isolation.build_tenant style,
    so no run is needed. Returns the sender's display name."""
    base = utcnow()
    db = SessionLocal()
    try:
        sender = db.get(User, sender_id)
        assert sender is not None
        sender_name = sender.name
        db.add_all(
            [
                Message(
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    run_id="run-export-1",
                    role="user",
                    content="What did the deals file say?",
                    created_by=sender_id,
                    created_at=base,
                ),
                Message(
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    run_id="run-export-1",
                    role="assistant",
                    content="The north territory leads [1].",
                    citations_json=json.dumps(
                        [
                            {
                                "chunk_id": "chunk-1",
                                "source_id": "source-1",
                                "filename": "deals.csv",
                                "ordinal": 0,
                                "excerpt": "North,30",
                                "score": 0.9,
                            }
                        ]
                    ),
                    created_by=sender_id,
                    created_at=base + timedelta(seconds=1),
                ),
                Message(
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    run_id="",
                    role="user",
                    content="btw the Q3 numbers are provisional",
                    created_by=sender_id,
                    created_at=base + timedelta(seconds=2),
                ),
            ]
        )
        db.commit()
    finally:
        db.close()
    return sender_name


def test_markdown_export_renders_the_readable_transcript(client):
    conversation_id = _make_conversation(client, title="Q3 Deals — review!")
    sender_name = _plant_messages(conversation_id)

    response = client.get(
        f"/api/conversations/{conversation_id}/export", params={"format": "md"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/markdown")
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    # The slug: lowercased, non-alphanumerics collapsed to '-', .md extension.
    assert 'filename="q3-deals-review.md"' in disposition

    body = response.text
    assert "# Q3 Deals — review!" in body
    assert "What did the deals file say?" in body
    assert "The north territory leads [1]." in body
    # The assistant heading is the fixed label; the user heading is the name.
    assert "## Assistant ·" in body
    assert f"## {sender_name} ·" in body
    # The "/btw" aside (role user, run_id "") says what it is.
    assert f"## {sender_name} (aside) ·" in body
    # Every timestamp is marked UTC — stored datetimes are naive UTC, and an
    # unmarked stamp would read as the consumer's local wall clock. The
    # header's exported-at line carries the same 'Z'.
    assert re.search(r"_Exported \S+Z · 3 messages_", body)
    for line in body.splitlines():
        if line.startswith("## "):
            assert line.endswith("Z"), line
    # A cited answer names its sources on one line.
    assert "> Sources: [1] deals.csv" in body


def test_json_export_mirrors_the_messages_endpoint(client):
    conversation_id = _make_conversation(client)
    _plant_messages(conversation_id)

    response = client.get(
        f"/api/conversations/{conversation_id}/export", params={"format": "json"}
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-disposition"].endswith('.json"')

    payload = response.json()
    assert payload["conversation"]["id"] == conversation_id

    listed = client.get(f"/api/conversations/{conversation_id}/messages").json()
    assert len(payload["messages"]) == len(listed) == 3
    # Field-for-field the same serialization GET /messages answers with.
    for exported, read in zip(payload["messages"], listed, strict=True):
        for field in ("id", "role", "content", "sender_name", "created_at"):
            assert exported[field] == read[field]


def test_an_unknown_format_is_a_422_not_a_500(client):
    conversation_id = _make_conversation(client)
    response = client.get(
        f"/api/conversations/{conversation_id}/export", params={"format": "csv"}
    )
    assert response.status_code == 422, response.text


def test_a_colleagues_personal_thread_does_not_export(client):
    """The personal-thread doctrine on the export door: for the non-creator
    member the thread is indistinguishable from absent, while the creator's
    own export keeps working."""
    owner = create_identity(name="Exporter A", workspace_name="Export workspace")
    client_a = _client_for(owner)
    conversation_id = _make_conversation(client_a, title="A's private notes")
    _plant_messages(
        conversation_id, workspace_id=owner.workspace_id, sender_id=owner.user_id
    )

    colleague, _ = _member(owner.workspace_id, name="Colleague B")
    denied = colleague.get(f"/api/conversations/{conversation_id}/export")
    assert denied.status_code == 404, denied.text

    allowed = client_a.get(f"/api/conversations/{conversation_id}/export")
    assert allowed.status_code == 200, allowed.text
    assert "A's private notes" in allowed.text


def test_a_shared_thread_exports_for_a_non_creator_member(client, identity_client):
    owner = create_identity(name="Exporter C", workspace_name="Shared export ws")
    client_c = _client_for(owner)
    conversation_id = _make_conversation(client_c, title="Team retro")
    _plant_messages(
        conversation_id, workspace_id=owner.workspace_id, sender_id=owner.user_id
    )
    shared = client_c.put(
        f"/api/conversations/{conversation_id}/share", json={"shared": True}
    )
    assert shared.status_code == 200, shared.text

    colleague, _ = _member(owner.workspace_id, name="Colleague D")
    response = colleague.get(f"/api/conversations/{conversation_id}/export")
    assert response.status_code == 200, response.text
    assert "Team retro" in response.text

    # One cross-tenant smoke assert; the sweep owns the full verdict.
    outsider = identity_client()
    assert (
        outsider.get(f"/api/conversations/{conversation_id}/export").status_code
        == 404
    )


def test_the_json_export_shape_is_declared_in_the_contract():
    """The JSON export is a machine-readable artifact other tools will parse,
    and the OpenAPI document is the one place its `{conversation, messages}`
    shape can be discovered — the route returns a raw Response (for
    Content-Disposition and the md arm), so the schema must be declared via
    `responses`, not inferred."""
    spec = app.openapi()
    operation = spec["paths"]["/api/conversations/{conversation_id}/export"]["get"]
    assert "ConversationExportOut" in json.dumps(operation["responses"]["200"])
    schema = spec["components"]["schemas"]["ConversationExportOut"]
    assert set(schema["required"]) >= {"conversation", "messages"}
