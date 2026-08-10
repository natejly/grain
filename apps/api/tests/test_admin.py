"""The workspace admin panel: what it shows, who may see it, what it must never say.

Three things are being proved.

*It answers.* Each panel is driven once by an owner with data planted underneath
it, and the figures are checked against the rows rather than against each other —
a route that returned empty lists would satisfy every isolation assertion below
and be useless.

*Only an owner sees it.* The role gate lives in one dependency, so the test that
matters is the sweep: every route the admin router exposes, driven by a plain
member, must be 403. It enumerates the router rather than a hand-written list, so
a route added without the gate fails here instead of shipping.

*Nothing crosses out.* Two axes. A stranger in another workspace gets their own
empty panels and a 404 for our sandbox — the same shape as
`test_tenant_isolation.py`, in-module so the assertions can be specific. And no
response may carry a credential: the MCP secrets blob, an OAuth token, or the
provider-side sandbox id, which ADR 0005 says never appears in an API response
because it addresses a live machine without going through this server.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, create_identity, issue_session
from fastapi.testclient import TestClient

from app.config import get_settings
from app.database import SessionLocal
from app.main import app
from app.models import (
    Agent,
    AgentToolCall,
    AuditEvent,
    Chunk,
    Conversation,
    GraphEdge,
    GraphEntity,
    McpOAuthToken,
    McpServer,
    McpTool,
    Membership,
    MemoryItem,
    Run,
    SandboxSession,
    Source,
    User,
)
from app.services.sandbox import session as sessions

MISSING = "00000000-0000-4000-8000-0000000000ff"

# Distinctive strings written into the columns an admin response must never
# echo. Any of them appearing in a body is the failure, whatever route produced
# it, so they are checked against every admin route at once.
MCP_SECRET_BLOB = "gAAAAA-mcp-secrets-blob-must-not-leak"
MCP_ACCESS_TOKEN = "gAAAAA-mcp-access-token-must-not-leak"
MCP_REFRESH_TOKEN = "gAAAAA-mcp-refresh-token-must-not-leak"


@dataclass
class Fixture:
    """One workspace, its owner's client, and the ids planted inside it."""

    identity: Identity
    client: TestClient
    ids: Dict[str, str] = field(default_factory=dict)

    @property
    def workspace_id(self) -> str:
        return self.identity.workspace_id

    @property
    def user_id(self) -> str:
        return self.identity.user_id


def _client_for(identity: Identity) -> TestClient:
    return authenticate(TestClient(app, base_url=TEST_BASE_URL), identity)


def _plant(label: str, identity: Identity) -> Dict[str, str]:
    """Give a workspace one row in each table the admin panels read.

    Written straight to the tables rather than through the API: the panels are
    read models, and routing the setup through create endpoints would make these
    tests fail for reasons that have nothing to do with the panels.
    """
    ids: Dict[str, str] = {}
    workspace_id, user_id = identity.workspace_id, identity.user_id
    db = SessionLocal()
    try:
        agent = db.query(Agent).filter(Agent.workspace_id == workspace_id).first()
        assert agent is not None, "create_identity should have made an agent"
        ids["agent"] = agent.id

        conversation = Conversation(
            workspace_id=workspace_id, created_by=user_id, title=f"{label} thread"
        )
        db.add(conversation)
        db.flush()
        ids["conversation"] = conversation.id

        run = Run(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            agent_id=agent.id,
            created_by=user_id,
            status="waiting_for_approval",
            prompt=f"{label} private prompt",
        )
        db.add(run)
        db.flush()
        ids["run"] = run.id

        approval = AgentToolCall(
            workspace_id=workspace_id,
            run_id=run.id,
            name="edit_document",
            arguments_json=json.dumps({"title": f"{label} doc"}),
            status="proposed",
            proposal_preview=f"{label} private diff",
        )
        db.add(approval)
        db.flush()
        ids["approval"] = approval.id

        source = Source(
            workspace_id=workspace_id,
            created_by=user_id,
            filename=f"{label.lower()}.csv",
            media_type="text/csv",
            object_key="",
            byte_size=4096,
            status="ready",
            chunk_count=1,
        )
        db.add(source)
        db.flush()
        ids["source"] = source.id

        db.add(
            Chunk(
                workspace_id=workspace_id,
                source_id=source.id,
                ordinal=0,
                content=f"{label} private passage",
                char_start=0,
                char_end=10,
                token_count=4,
            )
        )
        db.add(
            MemoryItem(
                workspace_id=workspace_id,
                conversation_id=conversation.id,
                kind="fact",
                content=f"{label} private memory",
                normalized_key=f"{label.lower()}-memory",
                status="active",
            )
        )

        entity = GraphEntity(
            workspace_id=workspace_id,
            name=f"{label} Entity",
            normalized_name=f"{label.lower()} entity",
        )
        neighbour = GraphEntity(
            workspace_id=workspace_id,
            name=f"{label} Neighbour",
            normalized_name=f"{label.lower()} neighbour",
        )
        db.add_all([entity, neighbour])
        db.flush()
        db.add(
            GraphEdge(
                workspace_id=workspace_id,
                from_entity_id=entity.id,
                to_entity_id=neighbour.id,
            )
        )

        server = McpServer(
            workspace_id=workspace_id,
            name=f"{label.lower()}-server",
            transport="stdio",
            command="/bin/true",
            secrets_encrypted=MCP_SECRET_BLOB,
            status="ready",
            last_error="",
            created_by=user_id,
        )
        db.add(server)
        db.flush()
        ids["mcp_server"] = server.id
        db.add(
            McpTool(
                workspace_id=workspace_id,
                server_id=server.id,
                name="read_thing",
                description=f"{label} tool",
            )
        )
        db.add(
            McpOAuthToken(
                workspace_id=workspace_id,
                server_id=server.id,
                user_id=user_id,
                access_token_enc=MCP_ACCESS_TOKEN,
                refresh_token_enc=MCP_REFRESH_TOKEN,
                scopes="read",
            )
        )

        db.add(
            AuditEvent(
                workspace_id=workspace_id,
                actor_id=user_id,
                action="source.uploaded",
                resource_type="source",
                resource_id=source.id,
                detail_json=json.dumps({"filename": source.filename}),
            )
        )
        # A row with no user behind it: workers write these, and the outer join
        # in the audit route is the only reason they still come back.
        db.add(
            AuditEvent(
                workspace_id=workspace_id,
                actor_id="",
                action="graph.rebuilt",
                resource_type="graph",
                resource_id="",
                detail_json="not json at all",
            )
        )
        db.commit()
    finally:
        db.close()
    return ids


def _plant_sandbox(workspace_id: str, user_id: str, label: str) -> Tuple[str, str]:
    """A running sandbox row; returns (session id, provider-side external id)."""
    external_id = f"{label.lower()}-{uuid.uuid4()}"
    db = SessionLocal()
    try:
        row = SandboxSession(
            workspace_id=workspace_id,
            created_by=user_id,
            provider="fake",
            external_id=external_id,
            label=f"{label} box",
            status="running",
            network_policy="allowlist",
            allow_hosts_json=json.dumps(["pypi.org"]),
            exec_count=3,
        )
        db.add(row)
        db.commit()
        return row.id, external_id
    finally:
        db.close()


@pytest.fixture(scope="module")
def owner() -> Fixture:
    identity = create_identity(name="Admin owner", workspace_name="Admin workspace")
    fixture = Fixture(identity=identity, client=_client_for(identity))
    fixture.ids.update(_plant("Alpha", identity))
    return fixture


@pytest.fixture(scope="module")
def member(owner: Fixture) -> TestClient:
    """A second person in the *same* workspace, with role "member"."""
    db = SessionLocal()
    try:
        user = User(email=f"{uuid.uuid4().hex}@example.com", name="Plain member")
        db.add(user)
        db.flush()
        db.add(
            Membership(
                workspace_id=owner.workspace_id, user_id=user.id, role="member"
            )
        )
        db.commit()
        user_id = user.id
    finally:
        db.close()
    token, csrf = issue_session(user_id)
    return _client_for(
        Identity(
            user_id=user_id,
            workspace_id=owner.workspace_id,
            token=token,
            csrf_token=csrf,
        )
    )


@pytest.fixture(scope="module")
def stranger() -> Fixture:
    """An owner of a different workspace, so "not yours" is testable."""
    identity = create_identity(name="Other owner", workspace_name="Other workspace")
    fixture = Fixture(identity=identity, client=_client_for(identity))
    fixture.ids.update(_plant("Bravo", identity))
    return fixture


@pytest.fixture
def fake_provider():
    """Pin the sandbox driver, exactly as tests/test_sandbox_api.py does.

    The kill path calls the provider, and a real one would need a key and a
    network. Drivers are cached, so the cache is dropped either side.
    """
    from app.services.sandbox.provider import reset_provider_cache

    settings = get_settings()
    was_enabled, was_provider = settings.sandbox_enabled, settings.sandbox_provider
    settings.sandbox_enabled, settings.sandbox_provider = True, "fake"
    reset_provider_cache()
    yield
    settings.sandbox_enabled, settings.sandbox_provider = was_enabled, was_provider
    reset_provider_cache()


def admin_requests(session_id: str) -> List[Tuple[str, str]]:
    """Every operation the admin router exposes, as (method, url).

    Read off the app rather than listed by hand: a route added without the owner
    gate, or without a cross-tenant answer, then fails the sweeps below instead
    of quietly joining the API.

    Enumerated from the OpenAPI schema rather than by walking `app.routes`,
    because FastAPI now wraps an included router in an opaque `_IncludedRouter`
    whose paths are not reachable by attribute — the schema is the flattened,
    stable view of the same thing.
    """
    requests: List[Tuple[str, str]] = [
        (method.upper(), path.replace("{session_id}", session_id))
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/api/admin")
        for method in operations
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    assert requests, "no admin routes found; did the router move?"
    return requests


# --------------------------------------------------------------------------
# The panels answer


def test_members_lists_the_roster_with_roles_and_join_dates(
    owner: Fixture, member: TestClient
):
    response = owner.client.get("/api/admin/members")
    assert response.status_code == 200, response.text
    rows = response.json()
    by_role = {row["role"]: row for row in rows}
    assert set(by_role) == {"owner", "member"}
    assert by_role["owner"]["user_id"] == owner.user_id
    assert by_role["owner"]["is_self"] is True
    assert by_role["member"]["is_self"] is False
    assert by_role["member"]["name"] == "Plain member"
    assert "@" in by_role["member"]["email"]
    assert by_role["owner"]["joined_at"] <= by_role["member"]["joined_at"]
    assert by_role["owner"]["status"] == "active"


def test_audit_log_pages_newest_first_and_survives_a_bad_detail_column(
    owner: Fixture,
):
    page = owner.client.get("/api/admin/audit-events", params={"limit": 1}).json()
    assert page["total"] >= 2
    assert page["limit"] == 1 and page["offset"] == 0
    assert page["has_more"] is True
    assert len(page["entries"]) == 1

    everything = owner.client.get(
        "/api/admin/audit-events", params={"limit": 200}
    ).json()
    actions = [entry["action"] for entry in everything["entries"]]
    assert "source.uploaded" in actions and "graph.rebuilt" in actions
    timestamps = [entry["created_at"] for entry in everything["entries"]]
    assert timestamps == sorted(timestamps, reverse=True)
    assert everything["has_more"] is False

    upload = next(e for e in everything["entries"] if e["action"] == "source.uploaded")
    assert upload["actor_id"] == owner.user_id
    assert upload["actor_name"] == "Admin owner"
    assert upload["detail"]["filename"].endswith(".csv")

    # The row whose detail column is not JSON still comes back, as an empty
    # object, and with no user resolved behind it.
    rebuilt = next(e for e in everything["entries"] if e["action"] == "graph.rebuilt")
    assert rebuilt["detail"] == {}
    assert rebuilt["actor_name"] == "" and rebuilt["actor_email"] == ""

    # The second page of a one-per-page walk is a different row.
    second = owner.client.get(
        "/api/admin/audit-events", params={"limit": 1, "offset": 1}
    ).json()
    assert second["entries"][0]["id"] != page["entries"][0]["id"]


def test_audit_paging_refuses_an_unbounded_walk(owner: Fixture):
    """Explicit caps, not defaults that happen to be small."""
    assert owner.client.get(
        "/api/admin/audit-events", params={"limit": 5000}
    ).status_code == 422
    assert owner.client.get(
        "/api/admin/audit-events", params={"offset": 10_001}
    ).status_code == 422


def test_activity_reports_run_statuses_and_the_approval_queue(owner: Fixture):
    body = owner.client.get("/api/admin/activity").json()
    assert body["run_status_counts"]["waiting_for_approval"] >= 1
    assert body["tool_call_status_counts"]["proposed"] >= 1

    run = next(row for row in body["recent_runs"] if row["id"] == owner.ids["run"])
    assert run["status"] == "waiting_for_approval"
    assert run["prompt_preview"] == "Alpha private prompt"
    assert run["conversation_id"] == owner.ids["conversation"]

    pending = [row["id"] for row in body["pending_approvals"]]
    assert owner.ids["approval"] in pending
    approval = next(
        row for row in body["pending_approvals"] if row["id"] == owner.ids["approval"]
    )
    assert approval["status"] == "proposed"
    assert approval["proposal_preview"] == "Alpha private diff"


def test_activity_previews_are_clipped(owner: Fixture):
    """A run's prompt is unbounded; the panel's copy of it is not."""
    from app.api.admin import PREVIEW_CHARS

    db = SessionLocal()
    try:
        run = db.get(Run, owner.ids["run"])
        assert run is not None
        original = run.prompt
        run.prompt = "x" * (PREVIEW_CHARS * 3)
        db.commit()
    finally:
        db.close()
    try:
        body = owner.client.get("/api/admin/activity").json()
        row = next(r for r in body["recent_runs"] if r["id"] == owner.ids["run"])
        assert len(row["prompt_preview"]) == PREVIEW_CHARS + 1  # + the ellipsis
    finally:
        db = SessionLocal()
        try:
            run = db.get(Run, owner.ids["run"])
            assert run is not None
            run.prompt = original
            db.commit()
        finally:
            db.close()


def test_storage_counts_the_workspaces_own_rows(owner: Fixture):
    body = owner.client.get("/api/admin/storage").json()
    assert body["sources_by_status"] == {"ready": 1}
    assert body["source_count"] == 1
    assert body["source_bytes"] == 4096
    assert body["chunk_count"] == 1
    assert body["memory_by_status"] == {"active": 1}
    assert body["memory_item_count"] == 1
    assert body["graph_entity_count"] == 2
    assert body["graph_edge_count"] == 1


def test_mcp_servers_report_health_and_a_tool_count(owner: Fixture):
    rows = owner.client.get("/api/admin/mcp-servers").json()
    assert len(rows) == 1
    server = rows[0]
    assert server["id"] == owner.ids["mcp_server"]
    assert server["transport"] == "stdio"
    assert server["enabled"] is True
    assert server["status"] == "ready"
    assert server["last_error"] == ""
    assert server["last_connected_at"] is None
    assert server["has_secrets"] is True
    assert server["tool_count"] == 1


def test_sandbox_sessions_list_without_the_provider_id(owner: Fixture):
    session_id, external_id = _plant_sandbox(
        owner.workspace_id, owner.user_id, "Alpha"
    )
    response = owner.client.get("/api/admin/sandbox-sessions")
    assert response.status_code == 200, response.text
    row = next(r for r in response.json() if r["id"] == session_id)
    assert row["status"] == "running"
    assert row["provider"] == "fake"
    assert row["network_policy"] == "allowlist"
    assert row["exec_count"] == 3
    assert row["killed_at"] is None
    assert row["created_at"] and row["last_used_at"]
    assert external_id not in response.text
    assert "external_id" not in response.text


def test_killing_a_session_goes_through_resolve_session(
    owner: Fixture, fake_provider, monkeypatch: pytest.MonkeyPatch
):
    """The kill action must resolve the id the one way ids are resolvable.

    `resolve_session` is the tenancy boundary — it filters by workspace inside
    the query — so "did the route call it" is the whole security property, and a
    route that selected `SandboxSession` by id itself would pass every other
    assertion in this file.
    """
    session_id, _external = _plant_sandbox(owner.workspace_id, owner.user_id, "Alpha")
    seen: List[Tuple[str, str]] = []
    original = sessions.resolve_session

    def recording(db, *, workspace_id: str, session_id: str):
        seen.append((workspace_id, session_id))
        return original(db, workspace_id=workspace_id, session_id=session_id)

    monkeypatch.setattr(sessions, "resolve_session", recording)

    response = owner.client.delete(f"/api/admin/sandbox-sessions/{session_id}")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "killed"
    assert response.json()["killed_at"] is not None
    assert seen and all(pair == (owner.workspace_id, session_id) for pair in seen)

    # Idempotent, matching kill_session's own contract.
    again = owner.client.delete(f"/api/admin/sandbox-sessions/{session_id}")
    assert again.status_code == 200
    assert again.json()["status"] == "killed"

    audit = owner.client.get(
        "/api/admin/audit-events", params={"limit": 5}
    ).json()["entries"]
    assert any(
        entry["action"] == "sandbox_session.killed"
        and entry["resource_id"] == session_id
        for entry in audit
    )


def test_killing_a_session_that_does_not_exist_is_a_404(owner: Fixture):
    assert (
        owner.client.delete(f"/api/admin/sandbox-sessions/{MISSING}").status_code == 404
    )


# --------------------------------------------------------------------------
# Only an owner


def test_every_admin_route_refuses_a_plain_member(owner: Fixture, member: TestClient):
    session_id, _external = _plant_sandbox(owner.workspace_id, owner.user_id, "Alpha")
    for method, url in admin_requests(session_id):
        response = member.request(method, url)
        assert response.status_code == 403, f"{method} {url} -> {response.status_code}"
        assert response.json()["detail"] == "Owner role required"


def test_a_member_can_still_use_the_rest_of_the_api(member: TestClient):
    """The 403s above must come from the role gate, not from a broken session."""
    assert member.get("/api/sources").status_code == 200


def test_every_admin_route_refuses_an_anonymous_caller(anonymous_client, owner: Fixture):
    for method, url in admin_requests(MISSING):
        response = anonymous_client.request(method, url)
        assert response.status_code == 401, f"{method} {url} -> {response.status_code}"


# --------------------------------------------------------------------------
# Nothing crosses out


def test_admin_panels_show_nothing_from_another_workspace(
    owner: Fixture, stranger: Fixture
):
    """Act as the stranger, and none of our workspace may appear in the answer.

    Mirrors the leak check in test_tenant_isolation.py: every id we own, plus the
    marker text planted in our rows, scanned across every admin response.
    """
    session_id, external_id = _plant_sandbox(
        owner.workspace_id, owner.user_id, "Alpha"
    )
    markers = [
        owner.workspace_id,
        owner.user_id,
        session_id,
        external_id,
        "Alpha private",
        "Admin owner",
        *owner.ids.values(),
    ]
    for method, url in admin_requests(session_id):
        response = stranger.client.request(method, url)
        assert response.status_code in {200, 404}, f"{method} {url}: {response.text}"
        for marker in markers:
            assert marker not in response.text, f"{method} {url} leaked {marker!r}"

    # And specifically: our session is not theirs to kill, and it is still alive.
    assert (
        stranger.client.delete(
            f"/api/admin/sandbox-sessions/{session_id}"
        ).status_code
        == 404
    )
    db = SessionLocal()
    try:
        row = db.get(SandboxSession, session_id)
        assert row is not None and row.status == "running"
    finally:
        db.close()


def test_a_foreign_session_is_indistinguishable_from_a_missing_one(
    owner: Fixture, stranger: Fixture
):
    """A 403 here would confirm the id names a real sandbox somewhere."""
    session_id, _external = _plant_sandbox(owner.workspace_id, owner.user_id, "Alpha")
    foreign = stranger.client.delete(f"/api/admin/sandbox-sessions/{session_id}")
    absent = stranger.client.delete(f"/api/admin/sandbox-sessions/{MISSING}")
    assert foreign.status_code == absent.status_code == 404
    assert foreign.json() == absent.json()
    assert session_id not in foreign.text


def test_no_admin_route_returns_a_secret(owner: Fixture):
    """Read the credentials out of the database, then look for them in the bodies.

    Asserted against the stored values rather than against a list of field names,
    so a response model that grows a field carrying one of them fails here even
    if the field is called something innocent.
    """
    session_id, external_id = _plant_sandbox(
        owner.workspace_id, owner.user_id, "Alpha"
    )
    db = SessionLocal()
    try:
        server = db.get(McpServer, owner.ids["mcp_server"])
        assert server is not None and server.secrets_encrypted
        token = (
            db.query(McpOAuthToken)
            .filter(McpOAuthToken.server_id == server.id)
            .one()
        )
        secrets = [
            server.secrets_encrypted,
            token.access_token_enc,
            token.refresh_token_enc,
            external_id,
        ]
    finally:
        db.close()
    assert secrets == [
        MCP_SECRET_BLOB,
        MCP_ACCESS_TOKEN,
        MCP_REFRESH_TOKEN,
        external_id,
    ]
    for method, url in admin_requests(session_id):
        if method == "DELETE":
            # Skipped deliberately: killing the session here would race the
            # assertions above it. The killed row's shape is checked in
            # test_killing_a_session_goes_through_resolve_session, which reads
            # the same model.
            continue
        response = owner.client.request(method, url)
        assert response.status_code == 200, f"{method} {url}: {response.text}"
        for secret in secrets:
            assert secret not in response.text, f"{method} {url} leaked a secret"
