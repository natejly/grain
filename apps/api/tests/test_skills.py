"""Shared skills: authored once, shared under a gate, invoked per turn.

The feature makes three promises, and each is proven here rather than asserted in
a comment:

- Visibility is own-or-shared, always same-workspace. A member sees their own and
  the workspace's shared skills, and a peer's private skill is a 404 — the same
  answer a nonexistent id gets, so membership of the row never leaks.
- Sharing is gated to owner/admin. A member may author a private skill freely,
  but flipping `shared` True (at create or edit) is refused 403, not silently
  downgraded — the ceiling is legible.
- Content is versioned. An edit that changes the body appends a snapshot and bumps
  the version; a no-op edit and a `shared` toggle spend no version; a restore is
  append-only. Prior versions stay listable and restorable.

Plus the reason the entity exists: a skill invoked for one turn reaches that
turn's assembled instructions with its args substituted, and — the invariant that
keeps today's callers byte-identical — a send with no `skill_id` injects nothing.
"""
from __future__ import annotations

import uuid
from typing import Any, Callable, Dict

import pytest
from conftest import (
    TEST_BASE_URL,
    Identity,
    authenticate,
    issue_session,
)
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import Membership, Run, Skill, SkillVersion, User, new_id
from app.services import skills as skills_service
from app.services.agent_loop import resolve_directives

# --------------------------------------------------------------------------
# Fixtures — an owner and two peers inside ONE workspace, plus a foreign tenant.


@pytest.fixture
def owner(identity_client: Callable[..., TestClient]) -> TestClient:
    """The workspace owner; skill names are unique per workspace so it gets its
    own."""
    return identity_client(name="Skill owner", workspace_name="Skills workspace")


def _member(workspace_id: str, *, role: str = "member", name: str = "Member") -> TestClient:
    """Another person in the same workspace, at a chosen role."""
    db = SessionLocal()
    try:
        user = User(email=f"{uuid.uuid4().hex}@example.com", name=name)
        db.add(user)
        db.flush()
        db.add(Membership(workspace_id=workspace_id, user_id=user.id, role=role))
        db.commit()
        user_id = user.id
    finally:
        db.close()
    token, csrf = issue_session(user_id)
    client = authenticate(
        TestClient(app, base_url=TEST_BASE_URL),
        Identity(user_id=user_id, workspace_id=workspace_id, token=token, csrf_token=csrf),
    )
    client.identity = Identity(  # type: ignore[attr-defined]
        user_id=user_id, workspace_id=workspace_id, token=token, csrf_token=csrf
    )
    return client


def ws_of(client: TestClient) -> str:
    return client.identity.workspace_id  # type: ignore[attr-defined,no-any-return]


def create_skill(client: TestClient, **overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "name": overrides.pop("name", "summarize"),
        "title": overrides.pop("title", "Summarize"),
        "body": overrides.pop("body", "Summarize the evidence."),
    }
    payload.update(overrides)
    headers = {"Idempotency-Key": "skill-" + uuid.uuid4().hex}
    response = client.post("/api/skills", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def ids(response_json: Any) -> set[str]:
    return {row["id"] for row in response_json}


# --------------------------------------------------------------------------
# CRUD


def test_create_lists_gets_and_deletes_a_private_skill(owner: TestClient) -> None:
    created = create_skill(owner, name="brief", title="Brief", body="Be brief.")
    assert created["version"] == 1
    assert created["shared"] is False

    listing = owner.get("/api/skills")
    assert listing.status_code == 200
    assert created["id"] in ids(listing.json())

    fetched = owner.get(f"/api/skills/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["body"] == "Be brief."

    deleted = owner.delete(f"/api/skills/{created['id']}")
    assert deleted.status_code == 204
    assert owner.get(f"/api/skills/{created['id']}").status_code == 404


def test_delete_cascades_its_versions(owner: TestClient) -> None:
    created = create_skill(owner, name="cascade")
    owner.patch(f"/api/skills/{created['id']}", json={"body": "v2 body"})
    owner.delete(f"/api/skills/{created['id']}")
    db = SessionLocal()
    try:
        remaining = db.query(SkillVersion).filter_by(skill_id=created["id"]).count()
    finally:
        db.close()
    assert remaining == 0


# --------------------------------------------------------------------------
# Visibility — own + shared, never a peer's private, never across workspaces.


def test_member_sees_own_and_shared_but_not_a_peers_private(owner: TestClient) -> None:
    shared = create_skill(owner, name="shared-skill", shared=True)
    owner_private = create_skill(owner, name="owner-private")

    alice = _member(ws_of(owner), name="Alice")
    bob = _member(ws_of(owner), name="Bob")

    alice_private = create_skill(alice, name="alice-private")

    alice_visible = ids(alice.get("/api/skills").json())
    # Alice sees the workspace's shared skill and her own private one …
    assert shared["id"] in alice_visible
    assert alice_private["id"] in alice_visible
    # … but not the owner's private skill.
    assert owner_private["id"] not in alice_visible

    # Bob, a third member, cannot see Alice's private skill either.
    bob_visible = ids(bob.get("/api/skills").json())
    assert alice_private["id"] not in bob_visible
    assert shared["id"] in bob_visible


def test_a_peers_private_skill_is_a_404_not_a_403(owner: TestClient) -> None:
    """The workspace filter makes an invisible id indistinguishable from a missing
    one — the row's existence must not leak through the status code."""
    owner_private = create_skill(owner, name="secret")
    alice = _member(ws_of(owner), name="Alice")
    assert alice.get(f"/api/skills/{owner_private['id']}").status_code == 404
    # An edit and a delete degrade to the same 404, not a 403.
    assert alice.patch(f"/api/skills/{owner_private['id']}", json={"body": "x"}).status_code == 404
    assert alice.delete(f"/api/skills/{owner_private['id']}").status_code == 404


def test_a_foreign_tenants_shared_skill_is_invisible(
    owner: TestClient, identity_client: Callable[..., TestClient]
) -> None:
    shared = create_skill(owner, name="ours", shared=True)
    stranger = identity_client(name="Stranger", workspace_name="Other workspace")
    assert shared["id"] not in ids(stranger.get("/api/skills").json())
    assert stranger.get(f"/api/skills/{shared['id']}").status_code == 404


# --------------------------------------------------------------------------
# Sharing gate — owner/admin only.


def test_a_member_cannot_author_a_shared_skill(owner: TestClient) -> None:
    member = _member(ws_of(owner), name="Member")
    response = member.post(
        "/api/skills",
        json={"name": "wanna-share", "title": "T", "body": "b", "shared": True},
        headers={"Idempotency-Key": "k-" + uuid.uuid4().hex},
    )
    assert response.status_code == 403


def test_a_member_cannot_flip_shared_on_an_edit(owner: TestClient) -> None:
    member = _member(ws_of(owner), name="Member")
    mine = create_skill(member, name="mine")
    response = member.patch(f"/api/skills/{mine['id']}", json={"shared": True})
    assert response.status_code == 403
    # The skill stayed private.
    assert member.get(f"/api/skills/{mine['id']}").json()["shared"] is False


def test_an_admin_may_share(owner: TestClient) -> None:
    admin = _member(ws_of(owner), role="admin", name="Admin")
    created = create_skill(admin, name="admin-shared", shared=True)
    assert created["shared"] is True
    assert created["can_share"] is True


def test_can_share_flag_reflects_the_gate(owner: TestClient) -> None:
    member = _member(ws_of(owner), name="Member")
    mine = create_skill(member, name="flag")
    assert member.get(f"/api/skills/{mine['id']}").json()["can_share"] is False
    owner_skill = create_skill(owner, name="owner-flag")
    assert owner.get(f"/api/skills/{owner_skill['id']}").json()["can_share"] is True


# --------------------------------------------------------------------------
# Versioning — content edits bump, metadata does not, restore is append-only.


def test_a_content_edit_appends_a_version(owner: TestClient) -> None:
    created = create_skill(owner, name="ver", body="original")
    assert created["version"] == 1

    edited = owner.patch(f"/api/skills/{created['id']}", json={"body": "revised"})
    assert edited.status_code == 200
    assert edited.json()["version"] == 2
    assert edited.json()["body"] == "revised"

    versions = owner.get(f"/api/skills/{created['id']}/versions")
    assert versions.status_code == 200
    assert sorted(v["version"] for v in versions.json()) == [1, 2]


def test_a_shared_toggle_and_a_noop_edit_spend_no_version(owner: TestClient) -> None:
    created = create_skill(owner, name="quiet", body="same")

    # Re-sending the identical body is a no-op — no new version.
    same = owner.patch(f"/api/skills/{created['id']}", json={"body": "same"})
    assert same.json()["version"] == 1

    # Toggling `shared` is metadata, not content.
    toggled = owner.patch(f"/api/skills/{created['id']}", json={"shared": True})
    assert toggled.json()["version"] == 1
    assert toggled.json()["shared"] is True

    versions = owner.get(f"/api/skills/{created['id']}/versions").json()
    assert len(versions) == 1


def test_restore_is_append_only_and_history_survives(owner: TestClient) -> None:
    """v1 'original' → edit to v2 'revised' → restoring v1 yields a NEW v3 carrying
    the original content, with v1 and v2 intact."""
    created = create_skill(owner, name="restore", body="original")
    owner.patch(f"/api/skills/{created['id']}", json={"body": "revised"})

    versions = owner.get(f"/api/skills/{created['id']}/versions").json()
    v1 = next(v for v in versions if v["version"] == 1)

    restored = owner.post(
        f"/api/skills/{created['id']}/versions/{v1['id']}/restore"
    )
    assert restored.status_code == 200
    assert restored.json()["version"] == 3
    assert restored.json()["body"] == "original"

    all_versions = owner.get(f"/api/skills/{created['id']}/versions").json()
    assert sorted(v["version"] for v in all_versions) == [1, 2, 3]


# --------------------------------------------------------------------------
# Injection — the body reaches the turn's assembled instructions, args substitute.


def _make_run(
    workspace_id: str, *, skill_id: str, skill_args_json: str = "", agent_id: str = ""
) -> Run:
    """A queued run carrying a skill invocation, as `send_message` would persist."""
    db = SessionLocal()
    try:
        run = Run(
            id=new_id(),
            workspace_id=workspace_id,
            conversation_id=new_id(),
            agent_id=agent_id,
            created_by="tester",
            status="queued",
            prompt="Go.",
            skill_id=skill_id,
            skill_args_json=skill_args_json,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run
    finally:
        db.close()


def test_the_skill_body_is_spliced_into_the_assembled_instructions(owner: TestClient) -> None:
    created = create_skill(owner, name="inject", body="ALWAYS cite your sources.")
    run = _make_run(ws_of(owner), skill_id=created["id"])
    db = SessionLocal()
    try:
        directives = resolve_directives(db, run)
    finally:
        db.close()
    assert "ALWAYS cite your sources." in directives.instructions


def test_declared_args_substitute_into_the_injected_body(owner: TestClient) -> None:
    created = create_skill(
        owner,
        name="tone",
        body="Write in a {{ tone }} tone, about {{ topic }}.",
        args=[
            {"name": "tone", "type": "string", "required": True},
            {"name": "topic", "type": "string", "required": False, "default": "anything"},
        ],
    )
    # Validate exactly as the send-message path does, then store on the run.
    db = SessionLocal()
    try:
        skill = db.get(Skill, created["id"])
        args_json = skills_service.validate_args(skill, {"tone": "formal"})
    finally:
        db.close()
    run = _make_run(ws_of(owner), skill_id=created["id"], skill_args_json=args_json)
    db = SessionLocal()
    try:
        instructions = resolve_directives(db, run).instructions
    finally:
        db.close()
    # Provided arg substitutes, and the declared default fills the omitted one.
    assert "Write in a formal tone, about anything." in instructions


def test_a_deleted_skill_degrades_to_no_injection(owner: TestClient) -> None:
    """A run outlives its skill: deleting the skill must leave the run resumable,
    with the injection simply dropping to nothing."""
    created = create_skill(owner, name="doomed", body="Injected text.")
    run = _make_run(ws_of(owner), skill_id=created["id"])
    owner.delete(f"/api/skills/{created['id']}")
    db = SessionLocal()
    try:
        instructions = resolve_directives(db, run).instructions
    finally:
        db.close()
    assert "Injected text." not in instructions


def test_absent_skill_leaves_the_instructions_unchanged(owner: TestClient) -> None:
    """The load-bearing back-compat invariant: no skill_id ⇒ byte-identical to a
    run that never heard of skills."""
    baseline = _make_run(ws_of(owner), skill_id="")
    db = SessionLocal()
    try:
        with_no_skill = resolve_directives(db, baseline).instructions
    finally:
        db.close()

    created = create_skill(owner, name="control", body="This should appear.")
    invoked = _make_run(ws_of(owner), skill_id=created["id"])
    db = SessionLocal()
    try:
        with_skill = resolve_directives(db, invoked).instructions
    finally:
        db.close()

    # The absent-skill run has nothing appended; the invoked one grows by exactly
    # the injected block on top of that same baseline.
    assert "This should appear." not in with_no_skill
    assert with_skill.startswith(with_no_skill)
    assert "This should appear." in with_skill


# --------------------------------------------------------------------------
# Injection through the send-message endpoint — validated and stored on the run.


def _conversation(client: TestClient) -> str:
    response = client.post(
        "/api/conversations",
        json={"title": "Skill turn"},
        headers={"Idempotency-Key": "conv-" + uuid.uuid4().hex},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _send(client: TestClient, conversation_id: str, **body: Any) -> Any:
    payload: Dict[str, Any] = {"content": "Do the thing."}
    payload.update(body)
    return client.post(
        f"/api/conversations/{conversation_id}/messages",
        json=payload,
        headers={"Idempotency-Key": "msg-" + uuid.uuid4().hex},
    )


def test_send_message_stores_the_invocation_on_the_run(owner: TestClient) -> None:
    created = create_skill(
        owner,
        name="send",
        body="Focus on {{ area }}.",
        args=[{"name": "area", "type": "string", "required": True}],
    )
    conv = _conversation(owner)
    response = _send(owner, conv, skill_id=created["id"], skill_args={"area": "risk"})
    assert response.status_code == 202, response.text
    run_id = response.json()["run"]["id"]
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run.skill_id == created["id"]
        assert '"area":"risk"' in run.skill_args_json
    finally:
        db.close()


def test_send_message_refuses_an_invisible_skill(owner: TestClient) -> None:
    owner_private = create_skill(owner, name="notyours")
    member = _member(ws_of(owner), name="Member")
    conv = _conversation(member)
    response = _send(member, conv, skill_id=owner_private["id"])
    assert response.status_code == 404


def test_send_message_refuses_a_missing_required_arg(owner: TestClient) -> None:
    created = create_skill(
        owner,
        name="needsarg",
        body="Use {{ required_one }}.",
        args=[{"name": "required_one", "type": "string", "required": True}],
    )
    conv = _conversation(owner)
    response = _send(owner, conv, skill_id=created["id"], skill_args={})
    assert response.status_code == 422


def test_send_message_without_a_skill_stores_no_invocation(owner: TestClient) -> None:
    conv = _conversation(owner)
    response = _send(owner, conv)
    assert response.status_code == 202
    run_id = response.json()["run"]["id"]
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        assert run.skill_id == ""
        assert run.skill_args_json == ""
    finally:
        db.close()
