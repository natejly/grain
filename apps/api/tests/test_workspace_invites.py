"""A workspace gains a second member.

Before these routes existed, `Membership` was written in exactly two places —
the dev seed and `_create_account` — always as `role="owner"` of a workspace
created in the same transaction. So no workspace had ever held two people,
`require_owner` was true for everybody who could reach it, and every rule in the
codebase that says "or the owner" had never once been the deciding clause.

That makes this file the first place several long-standing claims are actually
tested. It is organised around what can go wrong rather than around the routes:

* the link is a credential — hashed, single-use, expiring, revocable, and never
  in a response after the one that mints it;
* two clicks are one membership, and the *database* is what decides that;
* an invitation into workspace A is worth nothing against workspace B;
* a member is not an owner, and cannot make themselves one;
* the last owner cannot be removed or demoted, because a workspace with no
  owner has no way back.
"""
from __future__ import annotations

import threading
import uuid
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, create_identity, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.clock import utcnow
from app.database import SessionLocal
from app.main import app
from app.models import AuditEvent, Membership, User, WorkspaceInvite
from app.services.auth import invites as invite_service


def client_for(identity: Identity) -> TestClient:
    client = TestClient(app, base_url=TEST_BASE_URL)
    authenticate(client, identity)
    client.headers["X-Workspace-Id"] = identity.workspace_id
    return client


def new_email() -> str:
    return f"{uuid.uuid4().hex}@example.com"


@pytest.fixture
def owner() -> Tuple[Identity, TestClient]:
    identity = create_identity(name="Ada Owner", workspace_name="Alpha workspace")
    return identity, client_for(identity)


@pytest.fixture
def outsider() -> Tuple[Identity, TestClient]:
    """Somebody with their own account and their own workspace, not in Alpha.

    The realistic invitee: everybody who signs up gets a personal workspace, so
    accepting an invitation is always *gaining a second* membership, never
    gaining a first.
    """
    identity = create_identity(name="Bo Outsider", workspace_name="Bo's workspace")
    return identity, client_for(identity)


def email_of(user_id: str) -> str:
    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        assert user is not None
        return user.email
    finally:
        db.close()


def invite(
    client: TestClient, email: str, role: str = "member"
) -> Tuple[Dict[str, Any], str]:
    """Create an invitation. Returns (invite row, raw token)."""
    response = client.post("/api/admin/invites", json={"email": email, "role": role})
    assert response.status_code == 201, response.text
    body = response.json()
    token = body["accept_url"].split("token=")[1]
    return body["invite"], token


def memberships_of(workspace_id: str) -> List[Membership]:
    db = SessionLocal()
    try:
        return list(
            db.scalars(
                select(Membership).where(Membership.workspace_id == workspace_id)
            ).all()
        )
    finally:
        db.close()


def invite_row(invite_id: str) -> WorkspaceInvite:
    db = SessionLocal()
    try:
        row = db.get(WorkspaceInvite, invite_id)
        assert row is not None
        return row
    finally:
        db.close()


def make_member(workspace_id: str, *, role: str = "member") -> Tuple[str, TestClient]:
    """Put a brand new person straight into a workspace, bypassing the invite.

    Used where the invitation is not what is under test — the role gate, the
    last-owner rule — so those tests do not silently depend on the flow they are
    meant to be independent of.
    """
    db = SessionLocal()
    try:
        user = User(email=new_email(), name="Plain member")
        db.add(user)
        db.flush()
        db.add(Membership(workspace_id=workspace_id, user_id=user.id, role=role))
        db.commit()
        user_id = user.id
    finally:
        db.close()
    token, csrf = issue_session(user_id)
    return user_id, client_for(
        Identity(
            user_id=user_id, workspace_id=workspace_id, token=token, csrf_token=csrf
        )
    )


# --------------------------------------------------------------------------
# The happy path, which is the one that never existed


def test_an_invited_outsider_becomes_a_member_of_the_workspace(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    owner_identity, owner_client = owner
    outsider_identity, outsider_client = outsider
    before = len(memberships_of(owner_identity.workspace_id))

    _row, token = invite(owner_client, email_of(outsider_identity.user_id))
    accepted = outsider_client.post("/api/auth/invites/accept", json={"token": token})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["workspace_id"] == owner_identity.workspace_id
    assert accepted.json()["role"] == "member"
    assert accepted.json()["joined"] is True

    after = memberships_of(owner_identity.workspace_id)
    assert len(after) == before + 1
    assert {m.user_id for m in after} >= {
        owner_identity.user_id,
        outsider_identity.user_id,
    }


def test_the_new_member_can_select_the_workspace_they_joined(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    """The whole point: `X-Workspace-Id` is now believed for a second workspace.

    Before invitations, `GET /api/auth/workspaces` could only ever return one
    row and the switcher in the web app had nothing to switch between.
    """
    owner_identity, owner_client = owner
    outsider_identity, outsider_client = outsider
    _row, token = invite(owner_client, email_of(outsider_identity.user_id))
    outsider_client.post("/api/auth/invites/accept", json={"token": token})

    listed = outsider_client.get("/api/auth/workspaces")
    assert listed.status_code == 200
    by_id = {row["id"]: row for row in listed.json()}
    assert owner_identity.workspace_id in by_id
    assert by_id[owner_identity.workspace_id]["role"] == "member"
    assert by_id[outsider_identity.workspace_id]["role"] == "owner"

    # And a real request scoped to it succeeds, which is the membership fence in
    # `_resolve_workspace` believing the header for the first time.
    outsider_client.headers["X-Workspace-Id"] = owner_identity.workspace_id
    me = outsider_client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["workspace_id"] == owner_identity.workspace_id
    assert me.json()["role"] == "member"
    outsider_client.headers["X-Workspace-Id"] = outsider_identity.workspace_id


def test_an_invited_owner_arrives_as_an_owner(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    owner_identity, owner_client = owner
    outsider_identity, outsider_client = outsider
    _row, token = invite(owner_client, email_of(outsider_identity.user_id), role="owner")
    accepted = outsider_client.post("/api/auth/invites/accept", json={"token": token})
    assert accepted.status_code == 200
    assert accepted.json()["role"] == "owner"


def test_the_invitation_appears_in_the_audit_log_without_its_token(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    owner_identity, owner_client = owner
    outsider_identity, outsider_client = outsider
    invited = email_of(outsider_identity.user_id)
    _row, token = invite(owner_client, invited)
    outsider_client.post("/api/auth/invites/accept", json={"token": token})

    db = SessionLocal()
    try:
        events = list(
            db.scalars(
                select(AuditEvent).where(
                    AuditEvent.workspace_id == owner_identity.workspace_id
                )
            ).all()
        )
    finally:
        db.close()
    actions = {event.action for event in events}
    assert {"invite.created", "invite.accepted"} <= actions
    for event in events:
        assert token not in event.detail_json
        assert invite_service.hash_token(token) not in event.detail_json


# --------------------------------------------------------------------------
# The link is a credential


def test_the_raw_token_is_never_stored_and_never_returned_again(
    owner: Tuple[Identity, TestClient]
) -> None:
    row, token = invite(owner[1], new_email())
    stored = invite_row(row["id"])
    assert stored.token_hash == invite_service.hash_token(token)
    assert token not in stored.token_hash

    listed = owner[1].get("/api/admin/invites")
    assert listed.status_code == 200
    assert token not in listed.text
    assert stored.token_hash not in listed.text
    assert "token" not in listed.json()[0]


def test_a_link_cannot_be_used_twice(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    _row, token = invite(owner[1], email_of(outsider[0].user_id))
    assert outsider[1].post("/api/auth/invites/accept", json={"token": token}).status_code == 200
    second = outsider[1].post("/api/auth/invites/accept", json={"token": token})
    assert second.status_code == 400
    assert "already been accepted" in second.json()["detail"]


def test_an_expired_link_is_refused(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    row, token = invite(owner[1], email_of(outsider[0].user_id))
    db = SessionLocal()
    try:
        stored = db.get(WorkspaceInvite, row["id"])
        assert stored is not None
        stored.expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()
    refused = outsider[1].post("/api/auth/invites/accept", json={"token": token})
    assert refused.status_code == 400
    assert "expired" in refused.json()["detail"]
    assert not any(
        m.user_id == outsider[0].user_id for m in memberships_of(owner[0].workspace_id)
    )


def test_a_revoked_link_stops_working(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    row, token = invite(owner[1], email_of(outsider[0].user_id))
    revoked = owner[1].delete(f"/api/admin/invites/{row['id']}")
    assert revoked.status_code == 200
    assert revoked.json()["status"] == "revoked"

    refused = outsider[1].post("/api/auth/invites/accept", json={"token": token})
    assert refused.status_code == 400
    assert "withdrawn" in refused.json()["detail"]
    assert not any(
        m.user_id == outsider[0].user_id for m in memberships_of(owner[0].workspace_id)
    )


def test_revoking_twice_is_refused_rather_than_silently_repeated(
    owner: Tuple[Identity, TestClient]
) -> None:
    row, _token = invite(owner[1], new_email())
    assert owner[1].delete(f"/api/admin/invites/{row['id']}").status_code == 200
    again = owner[1].delete(f"/api/admin/invites/{row['id']}")
    assert again.status_code == 409


def test_re_inviting_an_address_invalidates_the_previous_link(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    """Two live links to one address are two chances for the older one to leak.

    `issue_email_token` consumes its predecessors for the same reason, and this
    is also the only way to *rotate* a link an owner has stopped trusting.
    """
    invited = email_of(outsider[0].user_id)
    _first_row, first_token = invite(owner[1], invited)
    _second_row, second_token = invite(owner[1], invited)

    stale = outsider[1].post("/api/auth/invites/accept", json={"token": first_token})
    assert stale.status_code == 400
    assert "withdrawn" in stale.json()["detail"]
    assert outsider[1].post(
        "/api/auth/invites/accept", json={"token": second_token}
    ).status_code == 200


def test_an_unknown_token_is_a_404_on_both_invite_routes(
    outsider: Tuple[Identity, TestClient]
) -> None:
    anonymous = TestClient(app, base_url=TEST_BASE_URL)
    assert (
        anonymous.post("/api/auth/invites/preview", json={"token": "not-a-token"}).status_code
        == 404
    )
    assert (
        outsider[1].post("/api/auth/invites/accept", json={"token": "not-a-token"}).status_code
        == 404
    )


# --------------------------------------------------------------------------
# Who may accept


def test_a_link_cannot_be_redeemed_by_an_account_it_was_not_sent_to(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    """A forwarded invitation is not a workspace.

    The token alone would be an argument for admission, since it went to one
    mailbox; requiring the account to *hold* that address as well means a link
    quoted in a ticket or read off a screenshot buys nothing.
    """
    _row, token = invite(owner[1], new_email())
    refused = outsider[1].post("/api/auth/invites/accept", json={"token": token})
    assert refused.status_code == 403
    assert not any(
        m.user_id == outsider[0].user_id for m in memberships_of(owner[0].workspace_id)
    )
    # Still pending: a refused attempt must not burn somebody else's link.
    assert invite_row(_row["id"]).accepted_at is None


def test_accepting_without_a_session_is_refused(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    _row, token = invite(owner[1], email_of(outsider[0].user_id))
    anonymous = TestClient(app, base_url=TEST_BASE_URL)
    assert anonymous.post("/api/auth/invites/accept", json={"token": token}).status_code == 401


def test_preview_works_before_the_invitee_has_signed_in(
    owner: Tuple[Identity, TestClient]
) -> None:
    """The invitee may have no account at all, so the page has to be able to say
    what they are being asked to join before one exists."""
    invited = new_email()
    _row, token = invite(owner[1], invited)
    anonymous = TestClient(app, base_url=TEST_BASE_URL)
    preview = anonymous.post("/api/auth/invites/preview", json={"token": token})
    assert preview.status_code == 200
    body = preview.json()
    assert body["workspace_name"] == "Alpha workspace"
    assert body["email"] == invited
    assert body["role"] == "member"
    assert body["status"] == "pending"
    assert body["invited_by_name"] == "Ada Owner"
    assert "token" not in body


def test_preview_reports_why_a_dead_link_is_dead(
    owner: Tuple[Identity, TestClient]
) -> None:
    row, token = invite(owner[1], new_email())
    owner[1].delete(f"/api/admin/invites/{row['id']}")
    anonymous = TestClient(app, base_url=TEST_BASE_URL)
    preview = anonymous.post("/api/auth/invites/preview", json={"token": token})
    assert preview.status_code == 200
    assert preview.json()["status"] == "revoked"


def test_an_address_that_is_already_a_member_cannot_be_invited(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    invited = email_of(outsider[0].user_id)
    _row, token = invite(owner[1], invited)
    outsider[1].post("/api/auth/invites/accept", json={"token": token})

    again = owner[1].post("/api/admin/invites", json={"email": invited, "role": "member"})
    assert again.status_code == 409
    assert "already a member" in again.json()["detail"]


def test_accepting_when_already_a_member_burns_the_link_and_changes_nothing(
    owner: Tuple[Identity, TestClient]
) -> None:
    """The only way to reach this is a membership created between issue and
    accept — but "answered once" must still hold, and an invitation must not be
    a back door to a role change."""
    user_id, member_client = make_member(owner[0].workspace_id, role="member")
    invited_email = email_of(user_id)
    # Issued through the service rather than the route, because the route
    # refuses an address that is already in the workspace — which is the guard
    # the previous test pins. This is the state a race would leave behind.
    db = SessionLocal()
    try:
        created, token = invite_service.issue_invite(
            db,
            workspace_id=owner[0].workspace_id,
            email=invited_email,
            role="owner",
            invited_by=owner[0].user_id,
        )
        db.commit()
        invite_id = created.id
    finally:
        db.close()

    accepted = member_client.post("/api/auth/invites/accept", json={"token": token})
    assert accepted.status_code == 200
    assert accepted.json()["joined"] is False
    # Their role is untouched: promotion is PATCH /members/{id}, which audits.
    assert accepted.json()["role"] == "member"
    assert invite_row(invite_id).accepted_at is not None
    rows = [m for m in memberships_of(owner[0].workspace_id) if m.user_id == user_id]
    assert len(rows) == 1
    assert rows[0].role == "member"


# --------------------------------------------------------------------------
# Two clicks are one membership


def test_two_overlapping_acceptances_of_one_link_produce_one_membership(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    """The interleaving written out, so the test says the same thing everywhere.

    Both transactions are open before either commits, which is the shape two
    Uvicorn workers produce. The conditional UPDATE inside `accept_invite` is
    what decides — a `SELECT accepted_at IS NULL` followed by a Python-side
    assignment cannot, because both readers would see an unclaimed row.
    """
    _row, token = invite(owner[1], email_of(outsider[0].user_id))
    first = SessionLocal()
    second = SessionLocal()
    try:
        invite_one = invite_service.load_invite(first, token)
        invite_two = invite_service.load_invite(second, token)
        assert invite_one is not None and invite_two is not None
        user_one = first.get(User, outsider[0].user_id)
        user_two = second.get(User, outsider[0].user_id)
        assert user_one is not None and user_two is not None
        one = invite_service.accept_invite(first, invite=invite_one, user=user_one)
        two = invite_service.accept_invite(second, invite=invite_two, user=user_two)
    finally:
        first.close()
        second.close()

    assert one is not None, "the first acceptance should succeed"
    assert two is None, "a second overlapping acceptance of one link was accepted"
    rows = [
        m for m in memberships_of(owner[0].workspace_id) if m.user_id == outsider[0].user_id
    ]
    assert len(rows) == 1


def test_racing_http_acceptances_leave_exactly_one_membership(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    """The same claim through the real stack, released by a barrier.

    The assertion is on the final database state and never on which thread won:
    "both got a 200" is flaky, "two rows exist where the constraint says one" is
    not. `UniqueConstraint("workspace_id", "user_id")` is the backstop that
    holds even if the claim above is ever weakened.
    """
    _row, token = invite(owner[1], email_of(outsider[0].user_id))
    workers = 6
    barrier = threading.Barrier(workers)
    statuses: List[int] = []
    lock = threading.Lock()

    def attempt() -> None:
        client = client_for(outsider[0])
        barrier.wait()
        response = client.post("/api/auth/invites/accept", json={"token": token})
        with lock:
            statuses.append(response.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    rows = [
        m for m in memberships_of(owner[0].workspace_id) if m.user_id == outsider[0].user_id
    ]
    assert len(rows) == 1, f"{len(rows)} memberships from one link; statuses={statuses}"
    assert statuses.count(200) == 1, f"more than one winner: {statuses}"


# --------------------------------------------------------------------------
# Cross-tenant


def test_an_invitation_to_one_workspace_is_worthless_against_another(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    """Accepting names no workspace at all — the invitation does, and it is
    loaded by token hash. So a token for A can only ever produce a membership in
    A, whatever the caller's `X-Workspace-Id` header says."""
    _row, token = invite(owner[1], email_of(outsider[0].user_id))
    outsider[1].headers["X-Workspace-Id"] = outsider[0].workspace_id
    accepted = outsider[1].post("/api/auth/invites/accept", json={"token": token})
    assert accepted.status_code == 200
    assert accepted.json()["workspace_id"] == owner[0].workspace_id
    assert not any(
        m.user_id == owner[0].user_id
        for m in memberships_of(outsider[0].workspace_id)
    )


def test_an_owner_cannot_see_or_revoke_another_workspaces_invitation(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    row, _token = invite(owner[1], new_email())
    assert outsider[1].delete(f"/api/admin/invites/{row['id']}").status_code == 404
    assert row["id"] not in outsider[1].get("/api/admin/invites").text
    assert invite_row(row["id"]).revoked_at is None


def test_an_owner_cannot_touch_a_membership_in_another_workspace(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    victim_id, _client = make_member(owner[0].workspace_id)
    membership = next(
        m for m in memberships_of(owner[0].workspace_id) if m.user_id == victim_id
    )
    assert outsider[1].delete(f"/api/admin/members/{membership.id}").status_code == 404
    assert (
        outsider[1]
        .patch(f"/api/admin/members/{membership.id}", json={"role": "owner"})
        .status_code
        == 404
    )
    still = next(
        m for m in memberships_of(owner[0].workspace_id) if m.user_id == victim_id
    )
    assert still.role == "member"


# --------------------------------------------------------------------------
# A member is not an owner


MEMBER_FORBIDDEN: List[Tuple[str, str, Optional[Dict[str, Any]]]] = [
    ("GET", "/api/admin/members", None),
    ("GET", "/api/admin/invites", None),
    ("POST", "/api/admin/invites", {"email": "someone@example.com", "role": "owner"}),
    ("DELETE", "/api/admin/invites/any-id", None),
    ("PATCH", "/api/admin/members/any-id", {"role": "owner"}),
    ("DELETE", "/api/admin/members/any-id", None),
]


@pytest.mark.parametrize(("method", "path", "body"), MEMBER_FORBIDDEN)
def test_a_member_is_refused_every_membership_route(
    owner: Tuple[Identity, TestClient],
    method: str,
    path: str,
    body: Optional[Dict[str, Any]],
) -> None:
    """These are the routes that hand out roles. A member who could reach them
    could make themselves an owner, and `require_owner` would guard nothing."""
    _user_id, member_client = make_member(owner[0].workspace_id)
    response = member_client.request(method, path, json=body)
    assert response.status_code == 403, f"{method} {path} -> {response.status_code}"
    assert response.json()["detail"] == "Owner role required"


def test_a_member_cannot_promote_themselves(
    owner: Tuple[Identity, TestClient]
) -> None:
    user_id, member_client = make_member(owner[0].workspace_id)
    membership = next(
        m for m in memberships_of(owner[0].workspace_id) if m.user_id == user_id
    )
    assert (
        member_client.patch(
            f"/api/admin/members/{membership.id}", json={"role": "owner"}
        ).status_code
        == 403
    )
    still = next(
        m for m in memberships_of(owner[0].workspace_id) if m.user_id == user_id
    )
    assert still.role == "member"


# --------------------------------------------------------------------------
# Roles, and the last owner


def test_a_role_the_product_does_not_have_is_refused(
    owner: Tuple[Identity, TestClient]
) -> None:
    """`Membership.role` is free text. Without this check a workspace could hold
    an "admin" that no branch in the codebase honours — a restriction the panel
    displays and nothing enforces."""
    rejected = owner[1].post(
        "/api/admin/invites", json={"email": new_email(), "role": "admin"}
    )
    assert rejected.status_code == 422
    user_id, _client = make_member(owner[0].workspace_id)
    membership = next(
        m for m in memberships_of(owner[0].workspace_id) if m.user_id == user_id
    )
    assert (
        owner[1]
        .patch(f"/api/admin/members/{membership.id}", json={"role": "superuser"})
        .status_code
        == 422
    )


def test_an_owner_can_promote_and_demote(owner: Tuple[Identity, TestClient]) -> None:
    user_id, _client = make_member(owner[0].workspace_id)
    membership = next(
        m for m in memberships_of(owner[0].workspace_id) if m.user_id == user_id
    )
    promoted = owner[1].patch(
        f"/api/admin/members/{membership.id}", json={"role": "owner"}
    )
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "owner"
    demoted = owner[1].patch(
        f"/api/admin/members/{membership.id}", json={"role": "member"}
    )
    assert demoted.status_code == 200
    assert demoted.json()["role"] == "member"


def test_the_last_owner_cannot_be_demoted(owner: Tuple[Identity, TestClient]) -> None:
    """A workspace with no owner cannot be administered by anybody, and there is
    no operator above the workspace to repair it — the same argument
    `_create_account` makes for writing the owner row with the workspace."""
    membership = next(
        m for m in memberships_of(owner[0].workspace_id) if m.user_id == owner[0].user_id
    )
    refused = owner[1].patch(
        f"/api/admin/members/{membership.id}", json={"role": "member"}
    )
    assert refused.status_code == 409
    assert "last owner" in refused.json()["detail"]
    assert owner[1].get("/api/auth/me").json()["role"] == "owner"


def test_the_last_owner_cannot_be_removed(owner: Tuple[Identity, TestClient]) -> None:
    membership = next(
        m for m in memberships_of(owner[0].workspace_id) if m.user_id == owner[0].user_id
    )
    refused = owner[1].delete(f"/api/admin/members/{membership.id}")
    assert refused.status_code == 409
    assert any(
        m.user_id == owner[0].user_id for m in memberships_of(owner[0].workspace_id)
    )


def test_an_owner_may_leave_once_there_is_a_second_owner(
    owner: Tuple[Identity, TestClient]
) -> None:
    """The rule is "the last owner", not "yourself": handing the workspace over
    and stepping out is a thing an owner is allowed to do."""
    successor_id, _client = make_member(owner[0].workspace_id, role="owner")
    membership = next(
        m for m in memberships_of(owner[0].workspace_id) if m.user_id == owner[0].user_id
    )
    assert owner[1].delete(f"/api/admin/members/{membership.id}").status_code == 204
    remaining = memberships_of(owner[0].workspace_id)
    assert [m.user_id for m in remaining] == [successor_id]


def test_a_removed_member_loses_the_workspace_on_their_very_next_request(
    owner: Tuple[Identity, TestClient]
) -> None:
    """Sessions are deliberately not revoked — a session is an identity, not a
    place, and the person may hold memberships elsewhere. Losing the membership
    is already total, because `_resolve_workspace` fails closed."""
    user_id, member_client = make_member(owner[0].workspace_id)
    assert member_client.get("/api/auth/me").status_code == 200
    membership = next(
        m for m in memberships_of(owner[0].workspace_id) if m.user_id == user_id
    )
    assert owner[1].delete(f"/api/admin/members/{membership.id}").status_code == 204
    assert member_client.get("/api/auth/me").status_code == 403


def test_removing_a_member_who_is_not_there_is_a_404(
    owner: Tuple[Identity, TestClient]
) -> None:
    assert owner[1].delete("/api/admin/members/nope").status_code == 404
    assert owner[1].patch("/api/admin/members/nope", json={"role": "member"}).status_code == 404


# --------------------------------------------------------------------------
# Input


def test_an_address_that_is_not_an_address_is_refused(
    owner: Tuple[Identity, TestClient]
) -> None:
    assert (
        owner[1].post("/api/admin/invites", json={"email": "not-an-address"}).status_code
        == 422
    )


def test_an_invitation_is_matched_case_insensitively(
    owner: Tuple[Identity, TestClient], outsider: Tuple[Identity, TestClient]
) -> None:
    """`users.email` is normalised on the way in, so the invitation has to be
    normalised the same way or an invite to Bob@Example.com is unanswerable."""
    invited = email_of(outsider[0].user_id)
    _row, token = invite(owner[1], invited.upper())
    assert outsider[1].post("/api/auth/invites/accept", json={"token": token}).status_code == 200
