"""A person makes a second workspace.

Until `POST /api/auth/workspaces` existed, a workspace could only be born as a
side effect of signing up (`api.auth._create_account`), so "start a shared
space for this project" meant making a second account. That made two
long-standing claims untestable, and this file is where they are finally
pinned:

* the creator lands as OWNER, which is the whole point — inviting is
  owner-gated (`api/admin.create_invite`), so a workspace whose maker cannot
  invite into it is a workspace nobody can share;
* the new workspace is EMPTY of everyone else's things. Every scoped table
  hangs off `workspace_id`, and the promise the tenant-isolation suite makes
  between two accounts has to hold just as hard between two workspaces of the
  *same* account — which is the case nothing had ever exercised.

The organization the workspace lands in is derived from the caller and never
read off the request; `test_the_org_is_never_taken_from_the_caller` is what
stops that from quietly becoming an input.
"""
from __future__ import annotations

from typing import Any, Tuple

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, create_identity
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app
from app.models import Agent, AuditEvent, Membership, OrgMembership, Workspace


def client_for(identity: Identity) -> TestClient:
    client = TestClient(app, base_url=TEST_BASE_URL)
    authenticate(client, identity)
    client.headers["X-Workspace-Id"] = identity.workspace_id
    return client


@pytest.fixture
def db() -> Any:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def maker() -> Tuple[Identity, TestClient]:
    identity = create_identity(name="Ada Maker", workspace_name="Ada's workspace")
    return identity, client_for(identity)


def create(client: TestClient, name: str = "Project Grain") -> dict:
    made = client.post("/api/auth/workspaces", json={"name": name})
    assert made.status_code == 201, made.text
    return dict(made.json())


def test_the_maker_lands_as_owner_and_can_invite(
    maker: Tuple[Identity, TestClient],
) -> None:
    """Creation and sharing are one story, so they are one test.

    Owner is not a cosmetic role here: `require_owner` gates the invite route,
    and a member-level creator would produce a workspace that cannot be shared
    — the exact thing this endpoint exists to make possible.
    """
    _, client = maker
    made = create(client)
    assert made["role"] == "owner"
    # Never current: the request that made it still belonged to the old
    # workspace, and the client switches by selecting the returned id.
    assert made["is_current"] is False

    client.headers["X-Workspace-Id"] = made["id"]
    invited = client.post(
        "/api/admin/invites", json={"email": "colleague@example.com", "role": "member"}
    )
    assert invited.status_code == 201, invited.text
    assert invited.json()["invite"]["email"] == "colleague@example.com"


def test_it_appears_in_the_switcher_and_can_be_selected(
    maker: Tuple[Identity, TestClient],
) -> None:
    _, client = maker
    made = create(client, "Second")
    listed = client.get("/api/auth/workspaces").json()
    assert made["id"] in {row["id"] for row in listed}
    # `X-Workspace-Id` is believed only against memberships, so selecting it
    # proves the membership is real rather than merely listed.
    client.headers["X-Workspace-Id"] = made["id"]
    session = client.get("/api/auth/me")
    assert session.status_code == 200, session.text
    assert session.json()["workspace_id"] == made["id"]
    assert session.json()["role"] == "owner"


def test_the_new_workspace_starts_empty_of_the_old_ones_things(
    maker: Tuple[Identity, TestClient],
) -> None:
    """Same person, two workspaces, no bleed.

    The isolation suite proves this between two accounts. Between two
    workspaces of ONE account nothing checked it, and that is the case where a
    query that forgot its `workspace_id` clause still passes every auth check.
    """
    _, client = maker
    seeded = client.post("/api/todos", json={"name": "Old list"})
    assert seeded.status_code == 201, seeded.text

    made = create(client, "Fresh")
    client.headers["X-Workspace-Id"] = made["id"]
    assert client.get("/api/todos").json() == []
    assert client.get("/api/sources").json() == []
    assert client.get("/api/memory").json() == []


def test_it_arrives_with_a_starter_agent(
    maker: Tuple[Identity, TestClient], db: Any
) -> None:
    """The same shape signup writes.

    A workspace that came in through the other door and has no agent is a
    workspace where the first chat has nothing to answer it.
    """
    _, client = maker
    made = create(client, "Agented")
    agents = list(
        db.scalars(select(Agent).where(Agent.workspace_id == made["id"]))
    )
    assert len(agents) == 1


def test_the_org_is_never_taken_from_the_caller(
    maker: Tuple[Identity, TestClient], db: Any
) -> None:
    """A body naming an organization must not be able to place a workspace in it.

    Org membership is what governs tool policy, so adopting a workspace into
    somebody else's org would be inheriting — or escaping — a posture the
    caller does not hold.
    """
    stranger = create_identity(name="Someone Else", workspace_name="Their workspace")
    their_org = db.scalar(
        select(OrgMembership.organization_id).where(
            OrgMembership.user_id == stranger.user_id
        )
    )
    _, client = maker
    made = client.post(
        "/api/auth/workspaces",
        json={"name": "Sneaky", "organization_id": their_org},
    )
    assert made.status_code == 201, made.text
    landed = db.get(Workspace, made.json()["id"])
    assert landed is not None
    assert landed.organization_id != their_org
    # It landed in the caller's OWN org — the one they administer.
    mine = db.scalar(
        select(OrgMembership).where(
            OrgMembership.organization_id == landed.organization_id,
            OrgMembership.user_id == maker[0].user_id,
        )
    )
    assert mine is not None and mine.role == "admin"


@pytest.mark.parametrize("name", ["", "   ", "\t\n"])
def test_a_nameless_workspace_is_refused(
    maker: Tuple[Identity, TestClient], name: str
) -> None:
    _, client = maker
    assert client.post("/api/auth/workspaces", json={"name": name}).status_code == 422


def test_the_name_is_bounded(maker: Tuple[Identity, TestClient]) -> None:
    """The column is 120 chars; the schema refuses rather than the driver
    truncating silently."""
    _, client = maker
    assert (
        client.post("/api/auth/workspaces", json={"name": "x" * 200}).status_code == 422
    )


def test_creation_is_refused_without_a_session() -> None:
    anonymous = TestClient(app, base_url=TEST_BASE_URL)
    assert (
        anonymous.post("/api/auth/workspaces", json={"name": "Nope"}).status_code == 401
    )


def test_creation_lands_in_the_audit_log(
    maker: Tuple[Identity, TestClient], db: Any
) -> None:
    _, client = maker
    made = create(client, "Audited")
    event = db.scalar(
        select(AuditEvent).where(
            AuditEvent.workspace_id == made["id"],
            AuditEvent.action == "workspace.created",
        )
    )
    assert event is not None


def test_two_workspaces_may_share_a_name(
    maker: Tuple[Identity, TestClient], db: Any
) -> None:
    """Names are labels, not keys.

    Nothing addresses a workspace by name, and refusing a duplicate would
    block the ordinary case of one person keeping a "Scratch" in two orgs.
    """
    _, client = maker
    first = create(client, "Scratch")
    second = create(client, "Scratch")
    assert first["id"] != second["id"]
    held = list(
        db.scalars(
            select(Membership).where(
                Membership.workspace_id.in_([first["id"], second["id"]])
            )
        )
    )
    assert len(held) == 2
