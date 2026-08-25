"""API tokens: the machine door, and the two hooks it opens.

The claims worth pinning:

- the raw secret exists in exactly one response — the mint's 201; the replay
  and the list can only prove it existed, never repeat it;
- a token is a delegation of one member's access in one workspace: it reaches
  its own workspace's threads and workflows and nobody else's (the cookie
  sweep cannot probe bearer auth, so these targeted tests are the isolation
  story for /api/hooks);
- revocation is immediate and uniform — a revoked, garbled, or absent token
  is the same 401;
- a webhook-triggered workflow runs at WORKFLOW policy scope by construction:
  a standing chat "always allow" does not authorise the write it reaches, so
  the run parks exactly as a scheduled one would;
- a posted message is an inert note: `run_id=""`, no agent turn.
"""
from __future__ import annotations

import uuid
from typing import Any

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, create_identity
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_workflow_executor import Probe, grant, graph, install, nodes_of, store, tool_node

from app.database import SessionLocal
from app.main import app
from app.models import Message, Run, WorkflowRun
from app.services.agent_loop import WORKFLOW_SCOPE, policy_scope_for_run


def key() -> dict[str, str]:
    return {"Idempotency-Key": "tok-" + uuid.uuid4().hex}


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def tenant() -> tuple[TestClient, Identity]:
    """A fresh owner in a fresh workspace, so minted tokens and tool policies
    cannot bleed into the shared dev workspace other modules clean."""
    identity = create_identity(name="Token owner", workspace_name="Token workspace")
    client = authenticate(TestClient(app, base_url=TEST_BASE_URL), identity)
    return client, identity


def mint(client: TestClient, name: str = "CI") -> dict:
    response = client.post("/api/api-tokens", headers=key(), json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def machine(secret: str) -> TestClient:
    """A client that holds only the bearer secret — no cookie, no CSRF."""
    client = TestClient(app, base_url=TEST_BASE_URL)
    client.headers["Authorization"] = f"Bearer {secret}"
    return client


def make_conversation(client: TestClient, title: str = "Hook target") -> str:
    response = client.post("/api/conversations", headers=key(), json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()["id"]


# --------------------------------------------------------------------------
# Minting and the raw-exactly-once rule
# --------------------------------------------------------------------------


def test_the_secret_appears_once_and_the_list_never_repeats_it(tenant):
    client, _ = tenant
    idempotency = key()
    first = client.post("/api/api-tokens", headers=idempotency, json={"name": "CI"})
    assert first.status_code == 201, first.text
    secret = first.json()["secret"]
    assert secret.startswith("grain_")
    # The replay proves the create happened; it cannot repeat what was never
    # stored — the server holds only the hash.
    again = client.post("/api/api-tokens", headers=idempotency, json={"name": "CI"})
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["secret"] == ""
    listed = client.get("/api/api-tokens")
    assert listed.status_code == 200
    assert secret not in listed.text
    assert all("secret" not in row for row in listed.json())
    assert all("token_hash" not in row for row in listed.json())


def make_member(workspace_id: str) -> str:
    """A second, non-owner member of an existing workspace. Returns their id."""
    from app.models import Membership, User

    db = SessionLocal()
    try:
        member = User(email=f"member-{uuid.uuid4().hex[:10]}@example.com", name="M")
        db.add(member)
        db.flush()
        db.add(
            Membership(workspace_id=workspace_id, user_id=member.id, role="member")
        )
        db.commit()
        return member.id
    finally:
        db.close()


def test_token_management_is_an_owners_surface(tenant):
    """Handing a machine standing access is the require_owner class of act."""
    client, identity = tenant
    from conftest import issue_session

    member_id = make_member(identity.workspace_id)
    token, csrf = issue_session(member_id)
    member_client = authenticate(
        TestClient(app, base_url=TEST_BASE_URL),
        Identity(
            user_id=member_id,
            workspace_id=identity.workspace_id,
            token=token,
            csrf_token=csrf,
        ),
    )
    assert (
        member_client.post(
            "/api/api-tokens", headers=key(), json={"name": "sneak"}
        ).status_code
        == 403
    )
    assert member_client.get("/api/api-tokens").status_code == 403


# --------------------------------------------------------------------------
# The door: open, revoked, garbage
# --------------------------------------------------------------------------


def test_a_live_token_posts_an_inert_note_and_a_revoked_one_is_401(tenant, db):
    client, _ = tenant
    conversation_id = make_conversation(client)
    minted = mint(client)
    bearer = machine(minted["secret"])

    posted = bearer.post(
        f"/api/hooks/conversations/{conversation_id}/messages",
        json={"content": "Build 412 finished."},
    )
    assert posted.status_code == 201, posted.text
    assert posted.json()["run_id"] == ""
    message = db.scalar(select(Message).where(Message.id == posted.json()["id"]))
    assert message is not None
    assert message.run_id == ""
    assert message.content == "Build 412 finished."
    # No agent turn was started on the note's account.
    runs = db.scalars(
        select(Run).where(Run.conversation_id == conversation_id)
    ).all()
    assert runs == []

    revoked = client.delete(f"/api/api-tokens/{minted['id']}")
    assert revoked.status_code == 204, revoked.text
    dead = bearer.post(
        f"/api/hooks/conversations/{conversation_id}/messages",
        json={"content": "still there?"},
    )
    assert dead.status_code == 401, dead.text


def test_missing_and_garbled_bearers_are_uniformly_401(tenant):
    client, _ = tenant
    conversation_id = make_conversation(client)
    path = f"/api/hooks/conversations/{conversation_id}/messages"
    body = {"content": "knock knock"}
    bare = TestClient(app, base_url=TEST_BASE_URL)
    for headers in (
        {},
        {"Authorization": "Bearer nope"},
        {"Authorization": f"Bearer grain_{uuid.uuid4().hex}"},
    ):
        response = bare.post(path, json=body, headers=headers)
        assert response.status_code == 401, (headers, response.text)
        assert response.json()["detail"] == "Not authenticated"


# --------------------------------------------------------------------------
# The isolation story the cookie sweep cannot tell
# --------------------------------------------------------------------------


def test_a_token_reaches_only_its_own_workspace(tenant, db):
    client_a, identity_a = tenant
    identity_b = create_identity(name="Other owner", workspace_name="Other workspace")
    client_b = authenticate(TestClient(app, base_url=TEST_BASE_URL), identity_b)

    conversation_b = make_conversation(client_b, title="B's private thread")
    workflow_b = store(db, identity_b, graph([tool_node("noop", "probe_read")]))
    bearer_a = machine(mint(client_a)["secret"])

    foreign_post = bearer_a.post(
        f"/api/hooks/conversations/{conversation_b}/messages",
        json={"content": "cross-tenant probe"},
    )
    assert foreign_post.status_code == 404, foreign_post.text

    foreign_trigger = bearer_a.post(
        f"/api/hooks/workflows/{workflow_b.id}/trigger", json={"payload": {}}
    )
    assert foreign_trigger.status_code == 404, foreign_trigger.text
    assert (
        db.scalars(
            select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_b.id)
        ).all()
        == []
    )


def test_a_token_cannot_reach_a_colleagues_personal_thread(tenant, db):
    """The roommate axis: workspace-correct is not enough — resolve_visible
    runs for the minting member, so a colleague's unshared thread 404s."""
    client, identity = tenant
    from app.models import Conversation

    colleague_id = make_member(identity.workspace_id)
    private = Conversation(
        workspace_id=identity.workspace_id,
        created_by=colleague_id,
        title="Colleague's private notes",
        shared=False,
    )
    db.add(private)
    db.commit()
    bearer = machine(mint(client)["secret"])
    refused = bearer.post(
        f"/api/hooks/conversations/{private.id}/messages",
        json={"content": "peek"},
    )
    assert refused.status_code == 404, refused.text


# --------------------------------------------------------------------------
# The webhook trigger runs at workflow scope
# --------------------------------------------------------------------------


def test_a_webhook_trigger_starts_a_workflow_scope_run(tenant, db, monkeypatch):
    """The unattended-policy invariant, end to end: the caller's standing CHAT
    allow exists and the write still parks, because the run is workflow-scoped
    by construction (it is a WorkflowRun)."""
    client, identity = tenant
    writer = Probe("probe_write", read_only=False)
    install(monkeypatch, writer)
    grant(db, identity, "probe_write", "allow", scope="chat")

    workflow = store(
        db, identity, graph([tool_node("send", "probe_write", {"text": "hi"})])
    )
    bearer = machine(mint(client)["secret"])
    triggered = bearer.post(
        f"/api/hooks/workflows/{workflow.id}/trigger", json={"payload": {}}
    )
    assert triggered.status_code == 202, triggered.text

    workflow_run = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.id == triggered.json()["workflow_run_id"]
        )
    )
    assert workflow_run is not None
    assert workflow_run.trigger == "webhook"
    # The background task ran inside the TestClient request; the chat grant
    # did not authorise the unattended write.
    db.refresh(workflow_run)
    assert workflow_run.status == "waiting_for_approval", workflow_run.error
    assert writer.calls == []
    assert nodes_of(db, workflow_run)["send"].policy == "ask"
    backing = db.scalar(select(Run).where(Run.id == workflow_run.run_id))
    assert backing is not None
    assert policy_scope_for_run(db, backing) == WORKFLOW_SCOPE


def test_the_trigger_validates_inputs_and_refuses_a_disabled_workflow(
    tenant, db, monkeypatch
):
    client, identity = tenant
    reader = Probe("probe_read")
    install(monkeypatch, reader)
    document = graph([tool_node("read", "probe_read")])
    document["inputs"] = [
        {"name": "region", "type": "string", "required": True, "default": None}
    ]
    workflow = store(db, identity, document)
    bearer = machine(mint(client)["secret"])

    missing = bearer.post(
        f"/api/hooks/workflows/{workflow.id}/trigger", json={"payload": {}}
    )
    assert missing.status_code == 422, missing.text
    assert (
        db.scalars(
            select(WorkflowRun).where(WorkflowRun.workflow_id == workflow.id)
        ).all()
        == []
    )

    supplied = bearer.post(
        f"/api/hooks/workflows/{workflow.id}/trigger",
        json={"payload": {"region": "north"}},
    )
    assert supplied.status_code == 202, supplied.text

    disabled = store(
        db, identity, graph([tool_node("read", "probe_read")]), status="disabled"
    )
    refused = bearer.post(
        f"/api/hooks/workflows/{disabled.id}/trigger", json={"payload": {}}
    )
    assert refused.status_code == 409, refused.text
