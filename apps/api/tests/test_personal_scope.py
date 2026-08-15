"""Two people in one workspace: whose memory, and whose permission.

ADR 0010. Every test here uses a *single* workspace with two members, which is
the axis `test_tenant_isolation.py` cannot reach: a query correctly filtered on
`workspace_id` passes every cross-tenant check in this suite and still hands one
member their colleague's private memory.
"""
from __future__ import annotations

import os
from typing import Tuple

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, create_identity, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.models import SHARED_OWNER, Conversation, Membership, MemoryItem, ToolPolicy, User
from app.services.agent_loop import CHAT_SCOPE, WORKFLOW_SCOPE, resolve_policy
from app.services.llm_tools import ToolSpec
from app.services.memory import apply_extracted_memories, recall


def _app():
    from app.main import app as fastapi_app

    return fastapi_app


def _client(identity: Identity) -> TestClient:
    return authenticate(TestClient(app=_app(), base_url=TEST_BASE_URL), identity)


@pytest.fixture
def pair() -> Tuple[Identity, Identity]:
    """An owner and a second member of the same workspace."""
    owner = create_identity(name="Owner", workspace_name="Shared workspace")
    db = SessionLocal()
    try:
        user = User(email=f"{os.urandom(6).hex()}@example.com", name="Member")
        db.add(user)
        db.flush()
        db.add(
            Membership(
                workspace_id=owner.workspace_id, user_id=user.id, role="member"
            )
        )
        user_id = user.id
        db.commit()
    finally:
        db.close()
    token, csrf = issue_session(user_id)
    member = Identity(
        user_id=user_id,
        workspace_id=owner.workspace_id,
        token=token,
        csrf_token=csrf,
    )
    return owner, member


def _thread(db, identity: Identity, *, shared: bool) -> str:
    row = Conversation(
        workspace_id=identity.workspace_id,
        created_by=identity.user_id,
        title="thread",
        shared=shared,
    )
    db.add(row)
    db.flush()
    return row.id


def _learn(
    db,
    identity: Identity,
    conversation_id: str,
    content: str,
    key: str,
    *,
    owner_id: str,
) -> None:
    apply_extracted_memories(
        db,
        workspace_id=identity.workspace_id,
        conversation_id=conversation_id,
        run_id="",
        extracted=[{"kind": "fact", "content": content, "normalized_key": key}],
        message_ids=[],
        settings=_settings(),
        owner_id=owner_id,
    )
    db.flush()


def _settings():
    from app.config import get_settings

    return get_settings()


def _recalled(db, identity: Identity, conversation_id: str, query: str) -> list[str]:
    return [
        item.content
        for item in recall(
            db,
            workspace_id=identity.workspace_id,
            conversation_id=conversation_id,
            query=query,
            viewer_id=identity.user_id,
        ).items
    ]


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------


def test_a_personal_threads_memory_never_reaches_another_members_recall(pair):
    """The leak this ADR closes.

    A personal thread is visible only to its creator — the API already refuses
    to let a member so much as decide a tool call parked on one. Until now the
    memory extracted from it was written with `workspace_id` and no owner, so
    the *contents* of that thread were injected into every colleague's next turn.
    """
    owner, member = pair
    db = SessionLocal()
    try:
        mine = _thread(db, owner, shared=False)
        theirs = _thread(db, member, shared=False)
        _learn(
            db, owner, mine, "Kestrel Audit runs every Tuesday.",
            "kestrel|cadence", owner_id=owner.user_id,
        )
        db.commit()

        assert any("Kestrel Audit" in text for text in _recalled(db, owner, mine, "Kestrel Audit"))
        assert _recalled(db, member, theirs, "Kestrel Audit") == []
    finally:
        db.close()


def test_a_shared_threads_memory_reaches_everyone(pair):
    """The other half, and the reason "make it all personal" was not the answer.

    A workspace whose members cannot pool what they learn is not a workspace,
    so sharing a thread has to keep sharing what the agent learns in it.
    """
    owner, member = pair
    db = SessionLocal()
    try:
        together = _thread(db, owner, shared=True)
        theirs = _thread(db, member, shared=False)
        _learn(
            db, owner, together, "Kestrel Audit runs every Tuesday.",
            "kestrel|cadence", owner_id=SHARED_OWNER,
        )
        db.commit()

        assert any("Kestrel Audit" in t for t in _recalled(db, owner, together, "Kestrel Audit"))
        assert any("Kestrel Audit" in t for t in _recalled(db, member, theirs, "Kestrel Audit"))
    finally:
        db.close()


def test_my_correction_does_not_retire_your_fact(pair):
    """Supersession stays inside a scope.

    One claim key, two owners, two live rows — which is what widening the unique
    key to include `owner_id` buys. Before it, `_retire` marked the other
    person's row superseded *and rewrote its `normalized_key`*, so their value
    was not merely outranked, it became permanently unrecallable.
    """
    owner, member = pair
    db = SessionLocal()
    try:
        mine = _thread(db, owner, shared=False)
        theirs = _thread(db, member, shared=False)
        _learn(
            db, owner, mine, "Standup is on Tuesday.", "standup|day",
            owner_id=owner.user_id,
        )
        _learn(
            db, member, theirs, "Standup is on Wednesday.", "standup|day",
            owner_id=member.user_id,
        )
        db.commit()

        rows = db.scalars(
            select(MemoryItem).where(
                MemoryItem.workspace_id == owner.workspace_id,
                MemoryItem.normalized_key == "standup|day",
            )
        ).all()
        assert {row.owner_id: row.status for row in rows} == {
            owner.user_id: "active",
            member.user_id: "active",
        }
        assert _recalled(db, owner, mine, "When is standup?") == ["Standup is on Tuesday."]
        assert _recalled(db, member, theirs, "When is standup?") == [
            "Standup is on Wednesday."
        ]
    finally:
        db.close()


def test_my_value_for_a_claim_shadows_the_workspaces(pair):
    """"Both, and mine wins" — and *only* mine, not both at once.

    A scoring bonus would have satisfied "mine wins" while still injecting the
    workspace's contradicting value alongside it, which is the STALE-SERVED
    failure evaluate_memory.py measures. It does not stop being that failure
    because the two rows disagree across people rather than across time.
    """
    owner, member = pair
    db = SessionLocal()
    try:
        together = _thread(db, owner, shared=True)
        mine = _thread(db, owner, shared=False)
        theirs = _thread(db, member, shared=False)
        _learn(
            db, owner, together, "The API deploys on Railway.", "api|deploy_host",
            owner_id=SHARED_OWNER,
        )
        _learn(
            db, owner, mine, "The API deploys on Fly.", "api|deploy_host",
            owner_id=owner.user_id,
        )
        db.commit()

        assert _recalled(db, owner, mine, "Where does the API deploy?") == [
            "The API deploys on Fly."
        ]
        # Nobody else's view moved: the shared value is still the workspace's.
        assert _recalled(db, member, theirs, "Where does the API deploy?") == [
            "The API deploys on Railway."
        ]
    finally:
        db.close()


def test_the_memory_list_and_delete_stop_at_the_callers_own(pair):
    owner, member = pair
    db = SessionLocal()
    try:
        theirs = _thread(db, member, shared=False)
        _learn(
            db, member, theirs, "Peregrine keys rotate in April.", "peregrine|rotation",
            owner_id=member.user_id,
        )
        db.commit()
        their_memory = db.scalar(
            select(MemoryItem.id).where(MemoryItem.owner_id == member.user_id)
        )
    finally:
        db.close()

    owner_client = _client(owner)
    listed = owner_client.get("/api/memory").json()
    assert not any(row["id"] == their_memory for row in listed)
    # Not "you may not", but "there is no such memory": naming it must not
    # confirm it exists, exactly as a foreign workspace's id does not.
    forget = owner_client.delete(
        f"/api/memory/{their_memory}",
        headers={"Idempotency-Key": "forget-" + os.urandom(6).hex()},
    )
    assert forget.status_code == 404

    member_client = _client(member)
    theirs_listed = member_client.get("/api/memory").json()
    row = next(item for item in theirs_listed if item["id"] == their_memory)
    assert row["shared"] is False


# --------------------------------------------------------------------------
# Tool policies
# --------------------------------------------------------------------------


def _write_tool() -> ToolSpec:
    return ToolSpec(
        name="send_email",
        description="",
        parameters={},
        executor=lambda db, context, args: None,
        read_only=False,
    )


def _verdict(db, identity: Identity, scope: str = CHAT_SCOPE) -> str:
    return resolve_policy(
        db,
        workspace_id=identity.workspace_id,
        user_id=identity.user_id,
        spec=_write_tool(),
        scope=scope,
    )


def test_always_allow_authorises_the_clicker_and_nobody_else(pair):
    """The security consequence ADR 0010 exists for.

    "Always allow send_email" removes the approval park, and the approval park is
    the only containment prompt injection has to get past. One member clicking it
    used to remove it for everyone.
    """
    owner, member = pair
    assert (
        _client(owner)
        .put("/api/tool-policies", json={"tool_name": "send_email", "policy": "allow"})
        .status_code
        == 200
    )
    db = SessionLocal()
    try:
        assert _verdict(db, owner) == "allow"
        assert _verdict(db, member) == "ask"
    finally:
        db.close()


def test_a_shared_grant_reaches_every_member_but_only_an_owner_may_write_one(pair):
    owner, member = pair
    refused = _client(member).put(
        "/api/tool-policies",
        json={"tool_name": "send_email", "policy": "allow", "shared": True},
    )
    assert refused.status_code == 403

    assert (
        _client(owner)
        .put(
            "/api/tool-policies",
            json={"tool_name": "send_email", "policy": "allow", "shared": True},
        )
        .status_code
        == 200
    )
    db = SessionLocal()
    try:
        assert _verdict(db, member) == "allow"
    finally:
        db.close()


def test_a_shared_deny_survives_a_personal_allow(pair):
    """A prohibition is not a grant, along the owner axis too.

    Without this, exempting yourself from a workspace-wide refusal is one PUT
    away and the escalation this ADR closes reopens sideways.
    """
    owner, member = pair
    _client(owner).put(
        "/api/tool-policies",
        json={"tool_name": "send_email", "policy": "deny", "shared": True},
    )
    _client(member).put(
        "/api/tool-policies", json={"tool_name": "send_email", "policy": "allow"}
    )
    db = SessionLocal()
    try:
        assert _verdict(db, member) == "deny"
    finally:
        db.close()


def test_a_personal_deny_tightens_a_shared_allow(pair):
    """The free direction: I may always be more cautious than my workspace."""
    owner, member = pair
    _client(owner).put(
        "/api/tool-policies",
        json={"tool_name": "send_email", "policy": "allow", "shared": True},
    )
    _client(member).put(
        "/api/tool-policies", json={"tool_name": "send_email", "policy": "deny"}
    )
    db = SessionLocal()
    try:
        assert _verdict(db, member) == "deny"
        assert _verdict(db, owner) == "allow"
    finally:
        db.close()


def test_the_two_axes_are_independent(pair):
    """`scope` says when, `owner_id` says to whom, and neither collapses into
    the other — a personal *workflow* grant is a coherent thing to hold."""
    owner, member = pair
    _client(member).put(
        "/api/tool-policies",
        json={"tool_name": "send_email", "policy": "allow", "scope": "workflow"},
    )
    db = SessionLocal()
    try:
        assert _verdict(db, member, WORKFLOW_SCOPE) == "allow"
        # Not in chat, and not for anybody else in either scope.
        assert _verdict(db, member, CHAT_SCOPE) == "ask"
        assert _verdict(db, owner, WORKFLOW_SCOPE) == "ask"
    finally:
        db.close()


def test_listing_and_revoking_a_grant_stop_at_the_callers_own(pair):
    owner, member = pair
    _client(member).put(
        "/api/tool-policies", json={"tool_name": "send_email", "policy": "allow"}
    )
    owner_client = _client(owner)
    assert not [
        row
        for row in owner_client.get("/api/tool-policies").json()
        if row["tool_name"] == "send_email"
    ]
    # The owner cannot revoke it either — it is not theirs to revoke, and there
    # is no shared row of that name to take back.
    assert (
        owner_client.delete(
            "/api/tool-policies/send_email", params={"scope": "chat"}
        ).status_code
        == 404
    )
    db = SessionLocal()
    try:
        assert _verdict(db, member) == "allow"
    finally:
        db.close()


def test_an_existing_workspace_wide_grant_still_applies_to_everyone(pair):
    """What migration 0040 leaves behind for `memory_items`, in policy form.

    A row with no owner is the workspace's, which is what every row written
    before this existed meant — and what the migration preserves for any grant
    it could not attribute.
    """
    owner, member = pair
    db = SessionLocal()
    try:
        db.add(
            ToolPolicy(
                workspace_id=owner.workspace_id,
                owner_id=SHARED_OWNER,
                tool_name="send_email",
                policy="allow",
                scope=CHAT_SCOPE,
            )
        )
        db.commit()
        assert _verdict(db, owner) == "allow"
        assert _verdict(db, member) == "allow"
    finally:
        db.close()
