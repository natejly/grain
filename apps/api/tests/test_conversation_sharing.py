"""Within-workspace conversation visibility: personal vs shared threads.

The cross-workspace property is proven by the isolation suite (tests/isolation.py
+ test_tenant_isolation.py). This file proves the *other* half the feature adds:
inside ONE workspace, a personal thread is the creator's alone, sharing makes it
visible to every member, and only the creator or an owner may flip that bit or
delete the thread. Two members of the same workspace exercise the relaxed
within-workspace `created_by` filter without ever touching the cross-workspace
`workspace_id` filter, which stays on every query.
"""
from __future__ import annotations

import os

import pytest
from conftest import TEST_BASE_URL, Identity, create_identity, issue_session
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import Membership, User


def _member(workspace_id: str, *, name: str, role: str = "member") -> tuple[TestClient, str, str]:
    """A fresh user placed in `workspace_id`, and a client authenticated as them.

    Returns (client, user_id, name). The workspace is their only membership, so a
    request with no selection header defaults to it — no cross-workspace ambiguity.
    """
    db = SessionLocal()
    try:
        user = User(email=f"{os.urandom(6).hex()}@example.com", name=name)
        db.add(user)
        db.flush()
        db.add(Membership(workspace_id=workspace_id, user_id=user.id, role=role))
        db.commit()
        user_id = user.id
    finally:
        db.close()
    token, csrf_token = issue_session(user_id)
    settings = get_settings()
    client = TestClient(app, base_url=TEST_BASE_URL)
    client.cookies.set(settings.session_cookie_name, token)
    client.headers[settings.csrf_header_name] = csrf_token
    return client, user_id, name


def _client_for(identity: Identity) -> TestClient:
    client = TestClient(app, base_url=TEST_BASE_URL)
    settings = get_settings()
    client.cookies.set(settings.session_cookie_name, identity.token)
    client.headers[settings.csrf_header_name] = identity.csrf_token
    return client


def _key() -> dict[str, str]:
    return {"Idempotency-Key": "share-test-" + os.urandom(8).hex()}


@pytest.fixture
def workspace_members():
    """One workspace with three roles: the creator/owner A, a plain member B, and
    a second owner D who did not create the thread. Plus an outsider C in a wholly
    separate workspace, to keep one cross-workspace assertion honest."""
    owner_a = create_identity(name="Member A", workspace_name="Shared workspace")
    client_a = _client_for(owner_a)
    client_b, user_b, name_b = _member(owner_a.workspace_id, name="Member B")
    client_d, user_d, _ = _member(owner_a.workspace_id, name="Owner D", role="owner")
    outsider = create_identity(name="Outsider C", workspace_name="Other workspace")
    client_c = _client_for(outsider)
    return {
        "a": (client_a, owner_a.user_id),
        "b": (client_b, user_b, name_b),
        "d": (client_d, user_d),
        "c": client_c,
    }


def _make_personal_thread(client_a: TestClient) -> str:
    created = client_a.post(
        "/api/conversations", headers=_key(), json={"title": "A's private thread"}
    )
    assert created.status_code == 201
    body = created.json()
    # New threads default to personal, owned by the creator, who may share them.
    assert body["shared"] is False
    assert body["owned"] is True
    assert body["can_share"] is True
    return body["id"]


def test_a_personal_thread_is_invisible_to_other_members(workspace_members):
    client_a, _ = workspace_members["a"]
    client_b, _, _ = workspace_members["b"]
    client_c = workspace_members["c"]
    conversation_id = _make_personal_thread(client_a)

    # The creator sees it in the rail; another member of the same workspace does not.
    assert conversation_id in [c["id"] for c in client_a.get("/api/conversations").json()]
    assert conversation_id not in [c["id"] for c in client_b.get("/api/conversations").json()]

    # And every by-id path 404s for the non-creator member — read, send, share, delete.
    assert client_b.get(f"/api/conversations/{conversation_id}/messages").status_code == 404
    assert (
        client_b.post(
            f"/api/conversations/{conversation_id}/messages",
            headers=_key(),
            json={"content": "let me in"},
        ).status_code
        == 404
    )
    assert (
        client_b.put(
            f"/api/conversations/{conversation_id}/share", json={"shared": True}
        ).status_code
        == 404
    )
    assert (
        client_b.delete(
            f"/api/conversations/{conversation_id}", headers=_key()
        ).status_code
        == 404
    )
    # The outsider (different workspace) is refused too — the workspace filter.
    assert client_c.get(f"/api/conversations/{conversation_id}/messages").status_code == 404


def test_sharing_makes_a_thread_visible_to_every_member_with_attribution(workspace_members):
    client_a, _ = workspace_members["a"]
    client_b, user_b, name_b = workspace_members["b"]
    conversation_id = _make_personal_thread(client_a)

    shared = client_a.put(
        f"/api/conversations/{conversation_id}/share", json={"shared": True}
    )
    assert shared.status_code == 200
    assert shared.json()["shared"] is True

    # B now sees the shared thread in the rail, but does not own it and cannot share it.
    listed = {c["id"]: c for c in client_b.get("/api/conversations").json()}
    assert conversation_id in listed
    assert listed[conversation_id]["shared"] is True
    assert listed[conversation_id]["owned"] is False
    assert listed[conversation_id]["can_share"] is False

    # B may read and send; the sent message is attributed to B.
    assert client_b.get(f"/api/conversations/{conversation_id}/messages").status_code == 200
    sent = client_b.post(
        f"/api/conversations/{conversation_id}/messages",
        headers=_key(),
        json={"content": "B joins the shared thread"},
    )
    assert sent.status_code == 202
    assert sent.json()["message"]["sender_id"] == user_b
    assert sent.json()["message"]["sender_name"] == name_b

    # And the attribution survives a reload: the user message names B as its sender.
    messages = client_b.get(f"/api/conversations/{conversation_id}/messages").json()
    user_message = next(m for m in messages if m["role"] == "user")
    assert user_message["sender_id"] == user_b
    assert user_message["sender_name"] == name_b


def test_sharing_never_reaches_a_member_of_another_workspace(workspace_members):
    """The load-bearing property: sharing relaxes visibility ONLY within the
    workspace. A shared thread is member-visible, never workspace-crossing — so
    the outsider in a *different* workspace must 404 on every by-id path AFTER
    the share exactly as they did before it, and it must never surface in their
    rail. This is the within-workspace half of what the isolation suite pins
    cross-workspace; here we prove the *shared* state specifically does not open
    a hole the personal state did not."""
    client_a, _ = workspace_members["a"]
    client_c = workspace_members["c"]
    conversation_id = _make_personal_thread(client_a)

    # Before sharing: the outsider cannot see or reach it.
    assert conversation_id not in [c["id"] for c in client_c.get("/api/conversations").json()]
    assert client_c.get(f"/api/conversations/{conversation_id}/messages").status_code == 404

    shared = client_a.put(
        f"/api/conversations/{conversation_id}/share", json={"shared": True}
    )
    assert shared.status_code == 200

    # After sharing: still nothing. The workspace filter never came off.
    assert conversation_id not in [c["id"] for c in client_c.get("/api/conversations").json()]
    assert client_c.get(f"/api/conversations/{conversation_id}/messages").status_code == 404
    assert (
        client_c.post(
            f"/api/conversations/{conversation_id}/messages",
            headers=_key(),
            json={"content": "cross-workspace?"},
        ).status_code
        == 404
    )
    # And the outsider cannot flip the bit either — a 404 (thread not in their
    # workspace), never a 403 that would confirm the thread exists.
    assert (
        client_c.put(
            f"/api/conversations/{conversation_id}/share", json={"shared": False}
        ).status_code
        == 404
    )
    assert (
        client_c.delete(
            f"/api/conversations/{conversation_id}", headers=_key()
        ).status_code
        == 404
    )


def test_only_creator_or_owner_may_share_or_delete(workspace_members):
    client_a, _ = workspace_members["a"]
    client_b, _, _ = workspace_members["b"]
    client_d, _ = workspace_members["d"]
    conversation_id = _make_personal_thread(client_a)
    client_a.put(f"/api/conversations/{conversation_id}/share", json={"shared": True})

    # A plain member sees the shared thread but may neither unshare nor delete it.
    assert (
        client_b.put(
            f"/api/conversations/{conversation_id}/share", json={"shared": False}
        ).status_code
        == 403
    )
    assert (
        client_b.delete(
            f"/api/conversations/{conversation_id}", headers=_key()
        ).status_code
        == 403
    )

    # A second owner — who did not create it — may, because the gate is creator-OR-owner.
    assert (
        client_d.put(
            f"/api/conversations/{conversation_id}/share", json={"shared": True}
        ).status_code
        == 200
    )


def test_unsharing_hides_the_thread_from_other_members_again(workspace_members):
    client_a, _ = workspace_members["a"]
    client_b, _, _ = workspace_members["b"]
    conversation_id = _make_personal_thread(client_a)
    client_a.put(f"/api/conversations/{conversation_id}/share", json={"shared": True})
    assert conversation_id in [c["id"] for c in client_b.get("/api/conversations").json()]

    unshared = client_a.put(
        f"/api/conversations/{conversation_id}/share", json={"shared": False}
    )
    assert unshared.status_code == 200
    assert unshared.json()["shared"] is False

    # Back to personal: B loses sight of it, and every by-id path 404s once more.
    assert conversation_id not in [c["id"] for c in client_b.get("/api/conversations").json()]
    assert client_b.get(f"/api/conversations/{conversation_id}/messages").status_code == 404


def test_rename_follows_visibility_and_refuses_subject_threads(workspace_members):
    client_a, _ = workspace_members["a"]
    client_b, _, _ = workspace_members["b"]
    conversation_id = _make_personal_thread(client_a)

    # The creator renames their personal thread; padding is trimmed.
    renamed = client_a.put(
        f"/api/conversations/{conversation_id}/title", json={"title": "  Q3 planning  "}
    )
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "Q3 planning"

    # Invisible means unrenameable — and 404, never 403, so the id's existence
    # is not confirmed to someone it is hidden from.
    assert (
        client_b.put(
            f"/api/conversations/{conversation_id}/title", json={"title": "B's now"}
        ).status_code
        == 404
    )

    # Shared, the name belongs to the collaboration: any member may change it,
    # same rule as the approval mode.
    client_a.put(f"/api/conversations/{conversation_id}/share", json={"shared": True})
    assert (
        client_b.put(
            f"/api/conversations/{conversation_id}/title", json={"title": "Ours"}
        ).status_code
        == 200
    )

    # All-whitespace collapses to empty, and an unfindable rail row is refused.
    assert (
        client_a.put(
            f"/api/conversations/{conversation_id}/title", json={"title": "   "}
        ).status_code
        == 422
    )

    # A subject thread is named by what it hangs off; hand-renaming would make
    # it stop saying so.
    document = client_a.post(
        "/api/documents",
        headers=_key(),
        json={"title": "Rename target", "kind": "markdown", "content": "x"},
    ).json()
    subject = client_a.post(
        f"/api/documents/{document['id']}/conversation", headers=_key()
    ).json()
    assert (
        client_a.put(
            f"/api/conversations/{subject['id']}/title", json={"title": "nope"}
        ).status_code
        == 409
    )
