"""The marketplace's Phase-1 promises, proven rather than asserted:

- Publishing is a snapshot. The listing holds the content the publisher saw;
  editing or deleting the source afterwards changes nothing published.
- The payload is an allowlist. Exactly the declared fields transfer — the strip
  test pins the key set, so a secret or workspace id can never ride along by
  a field slipping through. What a person *pastes into* a field is the lint's
  job: a token-shaped string refuses to publish, with the finding named.
- Installing is a copy that lands inert. The installer gets an ordinary local
  skill (`shared=False`), independent of the listing, editable, and safely
  renamed on collision rather than refused.
- The whole feature has an off switch that answers 404, as if it did not exist.
"""
from __future__ import annotations

import json
import os
import uuid
from typing import Any, Callable, Dict

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, issue_session
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    Agent,
    ListingInstall,
    ListingVersion,
    Membership,
    User,
    Workflow,
    Workspace,
)
from app.services import marketplace as marketplace_service

# --------------------------------------------------------------------------
# Fixtures and helpers


@pytest.fixture
def owner(identity_client: Callable[..., TestClient]) -> TestClient:
    return identity_client(
        name="Marketplace owner", workspace_name="Marketplace workspace"
    )


@pytest.fixture
def neighbor(identity_client: Callable[..., TestClient]) -> TestClient:
    """A second, unrelated workspace (its own organization)."""
    return identity_client(
        name="Marketplace neighbor", workspace_name="Neighbor workspace"
    )


def _key(prefix: str) -> Dict[str, str]:
    return {"Idempotency-Key": f"{prefix}-{uuid.uuid4().hex}"}


def _workspace_of(client: TestClient) -> str:
    return client.identity.workspace_id  # type: ignore[attr-defined,no-any-return]


def _authenticated(user_id: str, workspace_id: str) -> TestClient:
    token, csrf = issue_session(user_id)
    identity = Identity(
        user_id=user_id, workspace_id=workspace_id, token=token, csrf_token=csrf
    )
    client = authenticate(TestClient(app, base_url=TEST_BASE_URL), identity)
    client.identity = identity  # type: ignore[attr-defined]
    return client


def sibling_workspace(client: TestClient, *, name: str = "Sibling") -> TestClient:
    """A SECOND workspace under the caller's organization, with its own owner.

    This is the org tier's novel query shape: a reader whom no workspace filter
    admits and only the organization join may. Built directly in the DB because
    the product has no create-second-workspace API to lean on."""
    db = SessionLocal()
    try:
        organization_id = db.scalar(
            select(Workspace.organization_id).where(
                Workspace.id == _workspace_of(client)
            )
        )
        assert organization_id
        user = User(email=f"{os.urandom(6).hex()}@example.com", name=f"{name} owner")
        db.add(user)
        db.flush()
        workspace = Workspace(organization_id=organization_id, name=f"{name} workspace")
        db.add(workspace)
        db.flush()
        db.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        db.commit()
        user_id, workspace_id = user.id, workspace.id
    finally:
        db.close()
    return _authenticated(user_id, workspace_id)


def member_of(client: TestClient, *, name: str = "Member") -> TestClient:
    """A plain member inside the caller's own workspace."""
    db = SessionLocal()
    try:
        user = User(email=f"{os.urandom(6).hex()}@example.com", name=name)
        db.add(user)
        db.flush()
        db.add(
            Membership(
                workspace_id=_workspace_of(client), user_id=user.id, role="member"
            )
        )
        db.commit()
        user_id = user.id
    finally:
        db.close()
    return _authenticated(user_id, _workspace_of(client))


def create_skill(client: TestClient, **overrides: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "name": overrides.pop("name", "summarize"),
        "title": overrides.pop("title", "Summarize"),
        "body": overrides.pop("body", "Summarize the evidence."),
    }
    payload.update(overrides)
    response = client.post("/api/skills", json=payload, headers=_key("skill"))
    assert response.status_code == 201, response.text
    return response.json()


def publish(client: TestClient, skill_id: str, **overrides: Any) -> Any:
    payload: Dict[str, Any] = {
        "kind": "skill",
        "source_id": skill_id,
        "slug": overrides.pop("slug", "summarize"),
    }
    payload.update(overrides)
    return client.post(
        "/api/marketplace/listings", json=payload, headers=_key("publish")
    )


def install(client: TestClient, listing_id: str) -> Any:
    return client.post(
        f"/api/marketplace/listings/{listing_id}/install", headers=_key("install")
    )


# --------------------------------------------------------------------------
# Publish


def test_publish_snapshots_the_content_the_publisher_saw(owner: TestClient) -> None:
    skill = create_skill(owner, name="brief", title="Brief", body="Be brief.")
    published = publish(owner, skill["id"], slug="brief")
    assert published.status_code == 201, published.text
    listing = published.json()
    assert listing["kind"] == "skill"
    assert listing["latest_version"] == 1
    assert listing["payload"]["body"] == "Be brief."

    # Editing the source afterwards mutates nothing published: the listing is a
    # snapshot, not a reference.
    edited = owner.patch(f"/api/skills/{skill['id']}", json={"body": "Rewritten."})
    assert edited.status_code == 200
    fetched = owner.get(f"/api/marketplace/listings/{listing['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["payload"]["body"] == "Be brief."


def test_the_payload_key_set_is_exactly_the_allowlist(owner: TestClient) -> None:
    """The strip test: what transfers is the serializer's field list, nothing
    else — not workspace_id, not created_by, not `shared`. If a field is ever
    added to `SkillPayload`, this test forces the addition to be a decision."""
    skill = create_skill(owner, name="strip", title="Strip", body="Check me.")
    listing = publish(owner, skill["id"], slug="strip").json()
    assert set(listing["payload"].keys()) == {
        "title",
        "description",
        "body",
        "args_json",
    }
    db = SessionLocal()
    try:
        row = db.query(ListingVersion).filter_by(listing_id=listing["id"]).one()
        stored = json.loads(row.payload_json)
    finally:
        db.close()
    assert set(stored.keys()) == {"title", "description", "body", "args_json"}


def test_a_token_shaped_string_refuses_to_publish(owner: TestClient) -> None:
    skill = create_skill(
        owner,
        name="leaky",
        title="Leaky",
        body="Use the key sk-" + "a1B2c3D4e5F6g7H8i9J0" + " to call the API.",
    )
    refused = publish(owner, skill["id"], slug="leaky")
    assert refused.status_code == 422
    assert "secret key" in refused.text or "key" in refused.text
    # Nothing was published.
    assert owner.get("/api/marketplace/listings").json() == []


def test_the_publishers_workspace_id_refuses_to_publish(owner: TestClient) -> None:
    workspace_id = owner.identity.workspace_id  # type: ignore[attr-defined]
    skill = create_skill(
        owner, name="idful", title="Idful", body=f"See workspace {workspace_id}."
    )
    refused = publish(owner, skill["id"], slug="idful")
    assert refused.status_code == 422
    assert "workspace" in refused.text


def test_duplicate_slug_is_a_conflict_but_republish_appends_a_version(
    owner: TestClient,
) -> None:
    first = create_skill(owner, name="one", title="One", body="First body.")
    assert publish(owner, first["id"], slug="taken").status_code == 201

    # A *different* skill reusing the slug is a 409 — same publisher or not,
    # a slug names one listing.
    other = create_skill(owner, name="two", title="Two", body="Second body.")
    conflict = publish(owner, other["id"], slug="taken")
    assert conflict.status_code == 409

    # Republishing the same source with unchanged content is also a conflict —
    # there is nothing to say a changelog about.
    unchanged = publish(owner, first["id"], slug="taken", changelog="no-op")
    assert unchanged.status_code == 409

    # An edited source republished without a changelog is refused; with one, it
    # appends version 2 and the history keeps both.
    owner.patch(f"/api/skills/{first['id']}", json={"body": "First body, sharper."})
    mute = publish(owner, first["id"], slug="taken")
    assert mute.status_code == 422
    second = publish(owner, first["id"], slug="taken", changelog="Sharpened the body.")
    assert second.status_code == 201, second.text
    assert second.json()["latest_version"] == 2
    versions = second.json()["versions"]
    assert [row["version"] for row in versions] == [2, 1]
    assert versions[0]["changelog"] == "Sharpened the body."


# --------------------------------------------------------------------------
# Install


def test_install_yields_an_independent_inert_editable_skill(owner: TestClient) -> None:
    skill = create_skill(owner, name="original", title="Original", body="Original body.")
    listing = publish(owner, skill["id"], slug="portable").json()
    installed = install(owner, listing["id"])
    assert installed.status_code == 201, installed.text
    copy = installed.json()
    # The copy takes the listing's slug (the original name is taken in this
    # workspace, so the deterministic suffix applies to *that* name space).
    assert copy["kind"] == "skill"
    assert copy["resource_id"] != skill["id"]

    fetched = owner.get(f"/api/skills/{copy['resource_id']}").json()
    assert fetched["body"] == "Original body."
    # Inert: never workspace-shared on arrival, whoever installs it.
    assert fetched["shared"] is False

    # Independent: editing the copy touches neither the source nor the listing.
    owner.patch(f"/api/skills/{copy['resource_id']}", json={"body": "Localized."})
    assert owner.get(f"/api/skills/{skill['id']}").json()["body"] == "Original body."
    assert (
        owner.get(f"/api/marketplace/listings/{listing['id']}").json()["payload"]["body"]
        == "Original body."
    )
    # And the counter moved.
    assert (
        owner.get(f"/api/marketplace/listings/{listing['id']}").json()["install_count"]
        == 1
    )


def test_a_name_collision_gets_a_deterministic_suffix(owner: TestClient) -> None:
    skill = create_skill(owner, name="popular", title="Popular", body="Wanted.")
    listing = publish(owner, skill["id"], slug="popular").json()
    first = install(owner, listing["id"]).json()
    second = install(owner, listing["id"]).json()
    # "popular" is taken by the source skill itself, so the copies suffix.
    assert first["name"] == "popular-2"
    assert second["name"] == "popular-3"


def test_a_deleted_source_leaves_the_listing_and_installs_intact(
    owner: TestClient,
) -> None:
    skill = create_skill(owner, name="fleeting", title="Fleeting", body="Still here.")
    listing = publish(owner, skill["id"], slug="durable").json()
    assert owner.delete(f"/api/skills/{skill['id']}").status_code == 204

    # The listing survives its source — `source_id` is provenance, not an FK.
    fetched = owner.get(f"/api/marketplace/listings/{listing['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["payload"]["body"] == "Still here."
    installed = install(owner, listing["id"])
    assert installed.status_code == 201
    body = owner.get(f"/api/skills/{installed.json()['resource_id']}").json()["body"]
    assert body == "Still here."


# --------------------------------------------------------------------------
# Visibility and the off switch


def test_a_workspace_listing_is_invisible_next_door(
    owner: TestClient, neighbor: TestClient
) -> None:
    skill = create_skill(owner, name="local", title="Local", body="Ours.")
    listing = publish(owner, skill["id"], slug="local").json()
    assert neighbor.get("/api/marketplace/listings").json() == []
    assert (
        neighbor.get(f"/api/marketplace/listings/{listing['id']}").status_code == 404
    )
    assert install(neighbor, listing["id"]).status_code == 404


def test_the_off_switch_answers_404_everywhere(
    owner: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = create_skill(owner, name="dark", title="Dark", body="Unlit.")
    listing = publish(owner, skill["id"], slug="dark").json()

    from app.api import marketplace as marketplace_api

    disabled = get_settings().model_copy(update={"marketplace_enabled": False})
    monkeypatch.setattr(marketplace_api, "get_settings", lambda: disabled)

    assert owner.get("/api/marketplace/listings").status_code == 404
    assert owner.get(f"/api/marketplace/listings/{listing['id']}").status_code == 404
    assert publish(owner, skill["id"], slug="dark-2").status_code == 404
    assert install(owner, listing["id"]).status_code == 404


# --------------------------------------------------------------------------
# The org tier


def test_an_org_listing_reaches_the_sibling_workspace_and_installs_locally(
    owner: TestClient,
) -> None:
    skill = create_skill(owner, name="org-wide", title="Org wide", body="For everyone.")
    listing = publish(owner, skill["id"], slug="org-wide", visibility="org").json()

    sibling = sibling_workspace(owner)
    cards = sibling.get("/api/marketplace/listings").json()
    assert [card["id"] for card in cards] == [listing["id"]]
    assert cards[0]["mine"] is False
    assert cards[0]["can_manage"] is False

    detail = sibling.get(f"/api/marketplace/listings/{listing['id']}").json()
    assert detail["payload"]["body"] == "For everyone."
    assert detail["publisher_workspace"] == "Marketplace workspace"

    installed = install(sibling, listing["id"])
    assert installed.status_code == 201, installed.text
    # The copy lands in the INSTALLER's workspace, private, and the publisher's
    # workspace gains nothing.
    copy = sibling.get(f"/api/skills/{installed.json()['resource_id']}").json()
    assert copy["body"] == "For everyone."
    assert copy["shared"] is False
    # (Names are per-workspace, so the copy may share the source's name; what
    # must not happen is a new ROW appearing in the publisher's workspace.)
    owner_ids = {row["id"] for row in owner.get("/api/skills").json()}
    assert owner_ids == {skill["id"]}


def test_a_workspace_listing_stays_invisible_to_the_sibling(owner: TestClient) -> None:
    skill = create_skill(owner, name="homebody", title="Homebody", body="Stays put.")
    listing = publish(owner, skill["id"], slug="homebody").json()
    sibling = sibling_workspace(owner)
    assert sibling.get("/api/marketplace/listings").json() == []
    assert (
        sibling.get(f"/api/marketplace/listings/{listing['id']}").status_code == 404
    )
    assert install(sibling, listing["id"]).status_code == 404


def test_org_visibility_is_owner_gated_at_publish_and_at_widen(
    owner: TestClient,
) -> None:
    member = member_of(owner)
    skill = create_skill(member, name="member-made", title="Member made", body="Fine.")

    # A member may publish to the workspace, not to the organization.
    refused = publish(member, skill["id"], slug="member-made", visibility="org")
    assert refused.status_code == 403
    published = publish(member, skill["id"], slug="member-made")
    assert published.status_code == 201
    listing_id = published.json()["id"]

    # Widening later is the same decision, behind the same gate: the authoring
    # member may rename their listing but not push it out of the workspace.
    renamed = member.patch(
        f"/api/marketplace/listings/{listing_id}", json={"title": "Member's finest"}
    )
    assert renamed.status_code == 200
    widened = member.patch(
        f"/api/marketplace/listings/{listing_id}", json={"visibility": "org"}
    )
    assert widened.status_code == 403
    by_owner = owner.patch(
        f"/api/marketplace/listings/{listing_id}", json={"visibility": "org"}
    )
    assert by_owner.status_code == 200
    sibling = sibling_workspace(owner)
    assert [row["id"] for row in sibling.get("/api/marketplace/listings").json()] == [
        listing_id
    ]


def test_republishing_an_org_listing_is_owner_gated_too(owner: TestClient) -> None:
    """New versions of an org listing ship org-wide instantly, so the author
    alone cannot append them: the gate that guards widening guards republish."""
    member = member_of(owner)
    skill = create_skill(member, name="escalate", title="Escalate", body="v1 body.")
    listing = publish(member, skill["id"], slug="escalate").json()
    assert (
        owner.patch(
            f"/api/marketplace/listings/{listing['id']}", json={"visibility": "org"}
        ).status_code
        == 200
    )

    # The authoring member edits the source and tries to push v2 org-wide.
    member.patch(f"/api/skills/{skill['id']}", json={"body": "v2 body."})
    refused = publish(member, skill["id"], slug="escalate", changelog="v2")
    assert refused.status_code == 403
    # The published head is still v1 — nothing shipped.
    detail = owner.get(f"/api/marketplace/listings/{listing['id']}").json()
    assert detail["latest_version"] == 1

    # An owner republishing their *own* org listing passes the same gate.
    owned = create_skill(owner, name="sanctioned", title="Sanctioned", body="v1.")
    org_listing = publish(
        owner, owned["id"], slug="sanctioned", visibility="org"
    ).json()
    owner.patch(f"/api/skills/{owned['id']}", json={"body": "v2."})
    approved = publish(owner, owned["id"], slug="sanctioned", changelog="v2")
    assert approved.status_code == 201
    assert approved.json()["id"] == org_listing["id"]
    assert approved.json()["latest_version"] == 2


def test_republish_with_org_visibility_widens_the_listing(owner: TestClient) -> None:
    skill = create_skill(owner, name="wider", title="Wider", body="v1 body.")
    listing = publish(owner, skill["id"], slug="wider").json()
    assert listing["visibility"] == "workspace"

    owner.patch(f"/api/skills/{skill['id']}", json={"body": "v2 body."})
    widened = publish(
        owner, skill["id"], slug="wider", changelog="v2", visibility="org"
    )
    assert widened.status_code == 201
    assert widened.json()["visibility"] == "org"

    # And the default (workspace) on a later republish never narrows it back.
    owner.patch(f"/api/skills/{skill['id']}", json={"body": "v3 body."})
    again = publish(owner, skill["id"], slug="wider", changelog="v3")
    assert again.status_code == 201
    assert again.json()["visibility"] == "org"


def test_a_taken_down_listing_refuses_republish(owner: TestClient) -> None:
    """The republish arm honors the taken-down guard PATCH already enforces —
    and answers with the same non-confirming "not available" as any conflict."""
    skill = create_skill(owner, name="seized", title="Seized", body="v1.")
    listing = publish(owner, skill["id"], slug="seized").json()
    db = SessionLocal()
    try:
        from app.models import Listing

        row = db.get(Listing, listing["id"])
        assert row is not None
        row.status = "taken_down"
        db.commit()
    finally:
        db.close()
    owner.patch(f"/api/skills/{skill['id']}", json={"body": "v2."})
    refused = publish(owner, skill["id"], slug="seized", changelog="v2")
    assert refused.status_code == 409
    assert "not available" in refused.text


def test_a_whitespace_title_update_is_refused(owner: TestClient) -> None:
    skill = create_skill(owner, name="titled", title="Titled", body="Body.")
    listing = publish(owner, skill["id"], slug="titled").json()
    refused = owner.patch(
        f"/api/marketplace/listings/{listing['id']}", json={"title": " "}
    )
    assert refused.status_code == 422
    assert (
        owner.get(f"/api/marketplace/listings/{listing['id']}").json()["title"]
        == "Titled"
    )


def test_delist_withdraws_everywhere_and_restore_returns(owner: TestClient) -> None:
    skill = create_skill(owner, name="seasonal", title="Seasonal", body="Sometimes.")
    listing = publish(owner, skill["id"], slug="seasonal", visibility="org").json()
    sibling = sibling_workspace(owner)

    # A visible-but-foreign manager attempt is a 403, not a silent no-op.
    assert (
        sibling.patch(
            f"/api/marketplace/listings/{listing['id']}", json={"status": "delisted"}
        ).status_code
        == 403
    )

    delisted = owner.patch(
        f"/api/marketplace/listings/{listing['id']}", json={"status": "delisted"}
    )
    assert delisted.status_code == 200
    assert sibling.get("/api/marketplace/listings").json() == []
    assert (
        sibling.get(f"/api/marketplace/listings/{listing['id']}").status_code == 404
    )
    assert install(sibling, listing["id"]).status_code == 404

    # Delisting is a status, not a delete: the manager restores it whole.
    restored = owner.patch(
        f"/api/marketplace/listings/{listing['id']}", json={"status": "published"}
    )
    assert restored.status_code == 200
    assert [row["id"] for row in sibling.get("/api/marketplace/listings").json()] == [
        listing["id"]
    ]


def test_a_non_author_member_may_not_manage_a_listing(owner: TestClient) -> None:
    member = member_of(owner)
    skill = create_skill(owner, name="owned", title="Owned", body="The owner's.")
    listing = publish(owner, skill["id"], slug="owned").json()
    assert (
        member.patch(
            f"/api/marketplace/listings/{listing['id']}", json={"title": "Grabbed"}
        ).status_code
        == 403
    )


# --------------------------------------------------------------------------
# Workflows and agents as listable kinds


def _plant_agent(client: TestClient, *, allowed_tools_json: str) -> str:
    """An agent written directly, so the test controls `allowed_tools_json`
    verbatim — including names the agents API would refuse to grant."""
    db = SessionLocal()
    try:
        agent = Agent(
            workspace_id=_workspace_of(client),
            name="Portable analyst",
            description="Answers from evidence.",
            instructions="Cite everything you claim.",
            allowed_tools_json=allowed_tools_json,
            created_by=client.identity.user_id,  # type: ignore[attr-defined]
            enabled=True,
        )
        db.add(agent)
        db.commit()
        return agent.id
    finally:
        db.close()


def _graph(trigger: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": "Evidence sweep",
        "description": "Gather the passages.",
        "trigger": trigger,
        "nodes": [
            {
                "id": "gather",
                "kind": "tool",
                "tool": "search_sources",
                "arguments": {"query": "quarterly figures"},
                "description": "Find the passages.",
            }
        ],
        "edges": [],
    }


def create_workflow(client: TestClient, trigger: Dict[str, Any]) -> Dict[str, Any]:
    response = client.post("/api/workflows", json={"graph": _graph(trigger)})
    assert response.status_code == 201, response.text
    return response.json()


def test_an_agent_payload_drops_workspace_tools_and_names_them(
    owner: TestClient,
) -> None:
    agent_id = _plant_agent(
        owner,
        allowed_tools_json=json.dumps(["search_sources", "mcp_fetch_secretly"]),
    )
    listing = publish(
        owner, agent_id, kind="agent", slug="portable-analyst"
    ).json()
    # The strip test, agent edition: exactly the allowlist, and the
    # workspace-specific grant travels as a *name in the warnings*, never as a
    # grant in the payload.
    assert set(listing["payload"].keys()) == {
        "name",
        "description",
        "instructions",
        "allowed_tools_json",
        "unresolved_tools",
    }
    assert json.loads(listing["payload"]["allowed_tools_json"]) == ["search_sources"]
    assert listing["payload"]["unresolved_tools"] == ["mcp_fetch_secretly"]


def test_an_installed_agent_lands_disabled_with_the_confirmed_subset(
    owner: TestClient,
) -> None:
    agent_id = _plant_agent(
        owner,
        allowed_tools_json=json.dumps(["search_sources", "recall_memory"]),
    )
    listing = publish(owner, agent_id, kind="agent", slug="scoped-analyst").json()

    # The scope sheet confirmed only one of the two requested tools.
    installed = owner.post(
        f"/api/marketplace/listings/{listing['id']}/install",
        json={"allowed_tools": ["search_sources"]},
        headers=_key("install"),
    )
    assert installed.status_code == 201, installed.text
    rows = {row["id"]: row for row in owner.get("/api/agents").json()}
    copy = rows[installed.json()["resource_id"]]
    # Disabled ALWAYS — enabling is a separate deliberate act in the editor.
    assert copy["enabled"] is False
    assert copy["allowed_tools"] == ["search_sources"]


def test_a_workflow_payload_is_the_program_never_the_timer(owner: TestClient) -> None:
    workflow = create_workflow(
        owner, {"kind": "schedule", "cron": "0 9 * * 1", "timezone": "UTC"}
    )
    listing = publish(
        owner, workflow["id"], kind="workflow", slug="evidence-sweep"
    ).json()
    assert set(listing["payload"].keys()) == {
        "name",
        "description",
        "source_prompt",
        "graph_json",
    }
    graph = json.loads(listing["payload"]["graph_json"])
    assert graph["trigger"] == {"kind": "manual", "cron": "", "timezone": "UTC"}
    assert "schedule_cron" not in listing["payload"]


def test_an_installed_workflow_is_a_manual_draft_and_warns_on_holes(
    owner: TestClient,
) -> None:
    workflow = create_workflow(owner, {"kind": "manual", "cron": "", "timezone": "UTC"})
    listing = publish(
        owner, workflow["id"], kind="workflow", slug="sweep-org", visibility="org"
    ).json()

    sibling = sibling_workspace(owner)
    clean = install(sibling, listing["id"])
    assert clean.status_code == 201, clean.text
    assert clean.json()["warnings"] == []
    rows = {row["id"]: row for row in sibling.get("/api/workflows").json()}
    copy = rows[clean.json()["resource_id"]]
    assert copy["status"] == "draft"
    assert copy["trigger_kind"] == "manual"

    # A listing whose graph names a tool that no longer resolves anywhere: the
    # install still lands (a draft with a named hole beats a refusal with no
    # artifact to fix), and the hole is named in the warnings.
    db = SessionLocal()
    try:
        row = (
            db.query(ListingVersion)
            .filter_by(listing_id=listing["id"])
            .one()
        )
        payload = json.loads(row.payload_json)
        graph = json.loads(payload["graph_json"])
        graph["nodes"][0]["tool"] = "vanished_tool"
        payload["graph_json"] = json.dumps(graph, separators=(",", ":"), sort_keys=True)
        row.payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        db.commit()
    finally:
        db.close()
    holed = install(sibling, listing["id"])
    assert holed.status_code == 201, holed.text
    assert any("vanished_tool" in warning for warning in holed.json()["warnings"])


def test_crons_are_not_a_publishable_kind(owner: TestClient) -> None:
    refused = owner.post(
        "/api/marketplace/listings",
        json={"kind": "cron", "source_id": "anything", "slug": "never"},
        headers=_key("publish"),
    )
    assert refused.status_code == 422


# --------------------------------------------------------------------------
# The lint itself, unit-level


def test_lint_names_what_it_found() -> None:
    findings = marketplace_service.lint_strings(
        [
            "Bearer abcdefghijklmnopqrstuvwx0123456789",
            "https://example.com/report?access_token=deadbeefcafe1234",
        ],
        workspace_id="ws-1",
    )
    assert any("bearer token" in finding for finding in findings)
    assert any("URL carrying a credential" in finding for finding in findings)


def test_lint_passes_ordinary_prose() -> None:
    assert (
        marketplace_service.lint_strings(
            [
                "Summarize the attached evidence in three bullet points, "
                "then list open questions. Prefer primary sources."
            ],
            workspace_id="ws-1",
        )
        == []
    )


# --------------------------------------------------------------------------
# Lineage: updates, divergence, pins (Phase 4)


def apply_update(client: TestClient, listing_id: str, **body: Any) -> Any:
    return client.post(
        f"/api/marketplace/listings/{listing_id}/update",
        json=body,
        headers=_key("update"),
    )


def pin(client: TestClient, listing_id: str, pinned: bool) -> Any:
    return client.post(
        f"/api/marketplace/listings/{listing_id}/pin", json={"pinned": pinned}
    )


def _state_of(client: TestClient, listing_id: str) -> str:
    detail = client.get(f"/api/marketplace/listings/{listing_id}").json()
    return detail["install_state"]  # type: ignore[no-any-return]


def test_install_records_one_lineage_row_and_reinstall_repoints(
    owner: TestClient,
) -> None:
    skill = create_skill(owner, name="lineal", title="Lineal", body="v1.")
    listing = publish(owner, skill["id"], slug="lineal").json()
    install(owner, listing["id"])
    second = install(owner, listing["id"]).json()
    db = SessionLocal()
    try:
        rows = db.query(ListingInstall).filter_by(listing_id=listing["id"]).all()
        # One row per workspace per listing: the re-install re-pointed it.
        assert len(rows) == 1
        assert rows[0].target_id == second["resource_id"]
    finally:
        db.close()
    assert _state_of(owner, listing["id"]) == "installed"


def test_a_new_version_offers_an_update_and_applying_takes_it(
    owner: TestClient,
) -> None:
    skill = create_skill(owner, name="fresh", title="Fresh", body="v1.")
    listing = publish(owner, skill["id"], slug="fresh").json()
    copy = install(owner, listing["id"]).json()
    assert _state_of(owner, listing["id"]) == "installed"
    # Nothing newer yet: there is no update to apply.
    assert apply_update(owner, listing["id"]).status_code == 409

    owner.patch(f"/api/skills/{skill['id']}", json={"body": "v2."})
    publish(owner, skill["id"], slug="fresh", changelog="v2")
    assert _state_of(owner, listing["id"]) == "update_available"

    applied = apply_update(owner, listing["id"])
    assert applied.status_code == 200, applied.text
    assert owner.get(f"/api/skills/{copy['resource_id']}").json()["body"] == "v2."
    assert _state_of(owner, listing["id"]) == "installed"
    # Applying appended an ordinary skill version: the pre-update copy is
    # restorable through the same history any local edit would be.
    versions = owner.get(f"/api/skills/{copy['resource_id']}/versions").json()
    assert len(versions) >= 2


def test_a_locally_edited_copy_diverges_and_refuses_silent_overwrite(
    owner: TestClient,
) -> None:
    skill = create_skill(owner, name="edited", title="Edited", body="v1.")
    listing = publish(owner, skill["id"], slug="edited").json()
    copy = install(owner, listing["id"]).json()
    owner.patch(f"/api/skills/{copy['resource_id']}", json={"body": "My local take."})
    assert _state_of(owner, listing["id"]) == "diverged"

    owner.patch(f"/api/skills/{skill['id']}", json={"body": "v2."})
    publish(owner, skill["id"], slug="edited", changelog="v2")
    # Divergence outranks the newer version: the state is about the copy.
    assert _state_of(owner, listing["id"]) == "diverged"

    refused = apply_update(owner, listing["id"])
    assert refused.status_code == 409
    assert "local edits" in refused.text
    assert (
        owner.get(f"/api/skills/{copy['resource_id']}").json()["body"]
        == "My local take."
    )
    forced = apply_update(owner, listing["id"], confirm_overwrite=True)
    assert forced.status_code == 200, forced.text
    assert owner.get(f"/api/skills/{copy['resource_id']}").json()["body"] == "v2."


def test_a_pin_suppresses_update_offers_until_lifted(owner: TestClient) -> None:
    skill = create_skill(owner, name="steady", title="Steady", body="v1.")
    listing = publish(owner, skill["id"], slug="steady").json()
    install(owner, listing["id"])
    pinned = pin(owner, listing["id"], True)
    assert pinned.status_code == 200
    assert pinned.json()["pinned"] is True

    owner.patch(f"/api/skills/{skill['id']}", json={"body": "v2."})
    publish(owner, skill["id"], slug="steady", changelog="v2")
    # Suppressed at the source, not in the UI: the API itself stops offering.
    assert _state_of(owner, listing["id"]) == "installed"

    lifted = pin(owner, listing["id"], False)
    assert lifted.json()["install_state"] == "update_available"


def test_a_deleted_copy_reads_as_not_installed_and_update_says_reinstall(
    owner: TestClient,
) -> None:
    skill = create_skill(owner, name="gone", title="Gone", body="v1.")
    listing = publish(owner, skill["id"], slug="gone").json()
    copy = install(owner, listing["id"]).json()
    assert pin(owner, listing["id"], True).status_code == 200
    owner.delete(f"/api/skills/{copy['resource_id']}")
    detail = owner.get(f"/api/marketplace/listings/{listing['id']}").json()
    # The whole install contract resets with the copy: no state, no stale pin,
    # and the pin toggle answers like nothing was ever installed.
    assert detail["install_state"] == ""
    assert detail["pinned"] is False
    assert pin(owner, listing["id"], False).status_code == 404
    owner.patch(f"/api/skills/{skill['id']}", json={"body": "v2."})
    publish(owner, skill["id"], slug="gone", changelog="v2")
    answer = apply_update(owner, listing["id"])
    assert answer.status_code == 409
    assert "install it again" in answer.text


def test_a_pin_needs_an_install_and_a_reinstall_clears_it(owner: TestClient) -> None:
    skill = create_skill(owner, name="pinless", title="Pinless", body="v1.")
    listing = publish(owner, skill["id"], slug="pinless").json()
    # Nothing installed yet: nothing to pin.
    assert pin(owner, listing["id"], True).status_code == 404

    install(owner, listing["id"])
    assert pin(owner, listing["id"], True).json()["pinned"] is True
    # A fresh install is exactly the "I want the head now" act the pin
    # existed to prevent happening silently — so it lifts the pin.
    install(owner, listing["id"])
    assert (
        owner.get(f"/api/marketplace/listings/{listing['id']}").json()["pinned"]
        is False
    )


def test_a_diverged_copy_at_head_can_resync_with_consent(owner: TestClient) -> None:
    """Divergence with no newer version is not a dead end: a confirmed update
    re-syncs the copy to the published content."""
    skill = create_skill(owner, name="strayed", title="Strayed", body="v1.")
    listing = publish(owner, skill["id"], slug="strayed").json()
    copy = install(owner, listing["id"]).json()
    owner.patch(f"/api/skills/{copy['resource_id']}", json={"body": "Strayed away."})
    assert _state_of(owner, listing["id"]) == "diverged"

    refused = apply_update(owner, listing["id"])
    assert refused.status_code == 409
    assert "local edits" in refused.text
    resynced = apply_update(owner, listing["id"], confirm_overwrite=True)
    assert resynced.status_code == 200, resynced.text
    assert owner.get(f"/api/skills/{copy['resource_id']}").json()["body"] == "v1."
    assert _state_of(owner, listing["id"]) == "installed"


def test_an_agents_local_grant_edit_counts_as_divergence(owner: TestClient) -> None:
    """The marquee claim of local_content_hash: the grant is part of the copy's
    identity, so reshaping it makes updates ask before overwriting anything."""
    agent_id = _plant_agent(
        owner, allowed_tools_json=json.dumps(["recall_memory", "search_sources"])
    )
    listing = publish(owner, agent_id, kind="agent", slug="regrant-analyst").json()
    installed = owner.post(
        f"/api/marketplace/listings/{listing['id']}/install",
        json={"allowed_tools": ["recall_memory", "search_sources"]},
        headers=_key("install"),
    ).json()
    assert _state_of(owner, listing["id"]) == "installed"

    db = SessionLocal()
    try:
        copy = db.get(Agent, installed["resource_id"])
        assert copy is not None
        copy.allowed_tools_json = json.dumps(["search_sources"])
        db.commit()
    finally:
        db.close()
    assert _state_of(owner, listing["id"]) == "diverged"


def test_an_agent_update_brings_words_never_reach(owner: TestClient) -> None:
    agent_id = _plant_agent(owner, allowed_tools_json=json.dumps(["search_sources"]))
    listing = publish(owner, agent_id, kind="agent", slug="growing-analyst").json()
    installed = owner.post(
        f"/api/marketplace/listings/{listing['id']}/install",
        json={"allowed_tools": ["search_sources"]},
        headers=_key("install"),
    ).json()

    # v2 rewrites the instructions and asks for one more (portable) tool.
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        assert agent is not None
        agent.instructions = "Cite everything; recall context first."
        agent.allowed_tools_json = json.dumps(["recall_memory", "search_sources"])
        db.commit()
    finally:
        db.close()
    republished = publish(
        owner, agent_id, kind="agent", slug="growing-analyst", changelog="wants memory"
    )
    assert republished.status_code == 201, republished.text

    applied = apply_update(owner, listing["id"])
    assert applied.status_code == 200, applied.text
    # The widened request travels as a warning, never as a grant.
    assert any("recall_memory" in warning for warning in applied.json()["warnings"])
    rows = {row["id"]: row for row in owner.get("/api/agents").json()}
    copy = rows[installed["resource_id"]]
    assert copy["enabled"] is False
    assert copy["allowed_tools"] == ["search_sources"]
    assert "recall context" in copy["instructions"]


def test_a_workflow_update_replaces_the_program_and_bumps_the_version(
    owner: TestClient,
) -> None:
    workflow = create_workflow(
        owner, {"kind": "manual", "cron": "", "timezone": "UTC"}
    )
    listing = publish(
        owner, workflow["id"], kind="workflow", slug="sweep-updated"
    ).json()
    copy = install(owner, listing["id"]).json()

    db = SessionLocal()
    try:
        row = db.get(Workflow, workflow["id"])
        assert row is not None
        row.description = "Gather the passages, and the tables."
        db.commit()
    finally:
        db.close()
    republished = publish(
        owner,
        workflow["id"],
        kind="workflow",
        slug="sweep-updated",
        changelog="tables too",
    )
    assert republished.status_code == 201, republished.text

    applied = apply_update(owner, listing["id"])
    assert applied.status_code == 200, applied.text
    db = SessionLocal()
    try:
        updated = db.get(Workflow, copy["resource_id"])
        assert updated is not None
        assert updated.description == "Gather the passages, and the tables."
        assert updated.version == 2
        # The installer's operational state survives the update.
        assert updated.status == "draft"
        assert updated.trigger_kind == "manual"
    finally:
        db.close()
    assert _state_of(owner, listing["id"]) == "installed"


def test_a_workflow_update_keeps_an_armed_schedule(owner: TestClient) -> None:
    """Arming a copy is a graph edit (the trigger lives in the graph and the
    columns derive from it), so it reads as divergence; a confirmed update
    replaces the program but splices the local trigger back into the stored
    graph, keeping the schedule armed AND the graph agreeing with the columns.
    """
    workflow = create_workflow(
        owner, {"kind": "manual", "cron": "", "timezone": "UTC"}
    )
    listing = publish(
        owner, workflow["id"], kind="workflow", slug="sweep-armed"
    ).json()
    copy = install(owner, listing["id"]).json()

    # The installer arms their copy the way the product does: trigger in the
    # graph, columns derived from it.
    db = SessionLocal()
    try:
        armed = db.get(Workflow, copy["resource_id"])
        assert armed is not None
        graph = json.loads(armed.graph_json)
        graph["trigger"] = {"kind": "schedule", "cron": "0 9 * * 1", "timezone": "UTC"}
        armed.graph_json = json.dumps(graph, separators=(",", ":"), sort_keys=True)
        armed.trigger_kind = "schedule"
        armed.schedule_cron = "0 9 * * 1"
        db.commit()
    finally:
        db.close()
    assert _state_of(owner, listing["id"]) == "diverged"

    db = SessionLocal()
    try:
        source = db.get(Workflow, workflow["id"])
        assert source is not None
        source.description = "Sharper sweep."
        db.commit()
    finally:
        db.close()
    republished = publish(
        owner, workflow["id"], kind="workflow", slug="sweep-armed", changelog="v2"
    )
    assert republished.status_code == 201, republished.text

    applied = apply_update(owner, listing["id"], confirm_overwrite=True)
    assert applied.status_code == 200, applied.text
    db = SessionLocal()
    try:
        updated = db.get(Workflow, copy["resource_id"])
        assert updated is not None
        assert updated.description == "Sharper sweep."
        assert updated.trigger_kind == "schedule"
        assert updated.schedule_cron == "0 9 * * 1"
        stored = json.loads(updated.graph_json)
        assert stored["trigger"]["kind"] == "schedule"
        assert stored["trigger"]["cron"] == "0 9 * * 1"
    finally:
        db.close()
    assert _state_of(owner, listing["id"]) == "installed"
