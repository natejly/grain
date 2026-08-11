"""Cross-tenant isolation, proved by experiment.

Two fully populated workspaces, A and B. Everything below acts as A and names
B's ids — over HTTP for every route the app exposes, and in-process for every
agent tool, which never goes near HTTP and so is not covered by a single one of
the request-level probes.

The three shapes of assertion:

* a route that names B's resource must refuse it (never 2xx, never 5xx) and must
  not echo any of B's ids;
* a route that names nothing must answer only from A's rows;
* nothing in the whole sweep may change a single byte of B's data — proved by
  digesting every workspace-scoped row B owns before and after.

The route table lives in `isolation.py` and is compared against `app.openapi()`,
so a new endpoint cannot join the app without an isolation verdict.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List

import pytest
from conftest import TEST_BASE_URL, Identity, authenticate, issue_session
from cryptography.fernet import Fernet
from isolation import (
    CASES_BY_KEY,
    DENY,
    ROUTE_CASES,
    SCOPED,
    RouteCase,
    Tenant,
    build_tenant,
    replant_graph,
)
from pydantic import SecretStr
from sqlalchemy import select

from app.config import get_settings
from app.database import Base, SessionLocal
from app.main import app
from app.models import (
    Board,
    Dashboard,
    Document,
    GraphEdge,
    MemoryItem,
    Project,
    Run,
)
from app.services.llm_tools import ToolContext, build_registry

# --------------------------------------------------------------------------
# Fixtures


@pytest.fixture(scope="module", autouse=True)
def _encryption():
    """Connections and MCP secrets are Fernet-encrypted; give the module a key."""
    settings = get_settings()
    original = settings.integrations_encryption_key
    settings.integrations_encryption_key = SecretStr(Fernet.generate_key().decode())
    yield
    settings.integrations_encryption_key = original


@pytest.fixture(scope="module", autouse=True)
def _no_garmin_network(_encryption):
    """POST /api/integrations/garmin/credentials would dial Garmin otherwise."""
    from app.api import integrations
    from app.services.connectors.base import ConnectorError

    original = integrations.garmin_connector.login_with_credentials

    def refuse(email: str, password: str):
        raise ConnectorError("Garmin login failed (stubbed in tests)")

    integrations.garmin_connector.login_with_credentials = refuse
    yield
    integrations.garmin_connector.login_with_credentials = original


@pytest.fixture(scope="module")
def tenant_a(_encryption) -> Tenant:
    return build_tenant("Alpha")


@pytest.fixture(scope="module")
def tenant_b(_encryption) -> Tenant:
    return build_tenant("Bravo")


@pytest.fixture(scope="module")
def victim_digest(tenant_b: Tenant) -> str:
    return workspace_digest(tenant_b.workspace_id)


# --------------------------------------------------------------------------
# Helpers


def workspace_digest(workspace_id: str) -> str:
    """A stable hash of every workspace-scoped row a workspace owns.

    Table-driven off the metadata rather than a hand-written list, so a new
    model joins the tamper check for free.
    """
    digest = hashlib.sha256()
    db = SessionLocal()
    try:
        for name in sorted(Base.metadata.tables):
            table = Base.metadata.tables[name]
            if "workspace_id" not in table.c:
                continue
            rows = db.execute(
                table.select()
                .where(table.c.workspace_id == workspace_id)
                .order_by(table.c.id)
            ).all()
            digest.update(name.encode())
            for row in rows:
                digest.update(repr(tuple(row)).encode())
    finally:
        db.close()
    return digest.hexdigest()


def send(case: RouteCase, caller: Tenant, victim: Tenant):
    """Issue `case` as `caller`, pointed at `victim`'s resources."""
    client = caller.client
    if case.template == "/api/auth/logout":
        # Logging out would kill the module-scoped session every other case
        # rides on, so this one gets a throwaway session for the same user.
        client = _throwaway_client(caller)
    kwargs: Dict[str, Any] = {
        "params": case.params(victim),
        "headers": {"Idempotency-Key": "isolation-" + hashlib.sha1(
            case.key.encode()
        ).hexdigest()},
    }
    body = case.json_body(victim)
    if body is not None:
        kwargs["json"] = body
    if case.files:
        kwargs["files"] = {"file": ("probe.txt", b"probe", "text/plain")}
    return client.request(case.method, case.url(victim), **kwargs)


def _throwaway_client(tenant: Tenant):
    from fastapi.testclient import TestClient

    token, csrf = issue_session(tenant.user_id)
    client = TestClient(app, base_url=TEST_BASE_URL)
    return authenticate(
        client,
        Identity(
            user_id=tenant.user_id,
            workspace_id=tenant.workspace_id,
            token=token,
            csrf_token=csrf,
        ),
    )


def assert_no_leak(response, victim: Tenant, label: str) -> None:
    text = response.text or ""
    for value in victim.all_ids():
        assert value not in text, f"{label} leaked {victim.label}'s id {value}"
    for secret in (f"{victim.label} secret", f"{victim.label.lower()} secret"):
        assert secret not in text, f"{label} leaked {victim.label}'s content"


# --------------------------------------------------------------------------
# The route table must cover the app


def test_route_table_matches_the_app():
    """Every operation the app exposes has an isolation verdict, and no more."""
    spec = app.openapi()
    live = {
        f"{method.upper()} {path}"
        for path, operations in spec["paths"].items()
        for method in operations
        if method in {"get", "post", "put", "patch", "delete"}
    }
    covered = set(CASES_BY_KEY)
    assert live - covered == set(), "routes with no isolation case"
    assert covered - live == set(), "isolation cases for routes that no longer exist"


# --------------------------------------------------------------------------
# The sweep


@pytest.mark.parametrize("case", ROUTE_CASES, ids=lambda case: case.key)
def test_route_isolation(case: RouteCase, tenant_a: Tenant, tenant_b: Tenant):
    response = send(case, tenant_a, tenant_b)
    label = case.key

    # 500 is the only 5xx nothing may produce: it means an attacker-supplied id
    # reached an unhandled exception. 503 ("this provider is not configured") is
    # a deliberate answer and is allowed on the PUBLIC auth entry points.
    assert response.status_code != 500, (
        f"{label} raised an unhandled error. Body: {response.text[:400]}"
    )

    if case.verdict == DENY:
        assert response.status_code < 500, (
            f"{label} returned {response.status_code} for another tenant's "
            f"resource. Body: {response.text[:400]}"
        )
        assert response.status_code == case.expect, (
            f"{label} answered {response.status_code} for another tenant's "
            f"resource; expected {case.expect}. A refusal with the wrong reason "
            f"is still an oracle. Body: {response.text[:400]}"
        )
        assert_no_leak(response, tenant_b, label)
    elif case.verdict == SCOPED:
        assert_no_leak(response, tenant_b, label)


def test_sweep_did_not_touch_the_victim(
    tenant_a: Tenant, tenant_b: Tenant, victim_digest: str
):
    """Run last: nothing above may have changed a byte of B's workspace.

    The per-case assertions catch a 200 that returns foreign data. They cannot
    catch a route that refuses in its response but mutated on the way there, so
    the whole of B is digested before and after.
    """
    for case in ROUTE_CASES:
        send(case, tenant_a, tenant_b)
    assert workspace_digest(tenant_b.workspace_id) == victim_digest, (
        "the route sweep changed tenant B's data"
    )


# --------------------------------------------------------------------------
# Targeted route tests, where a generic "not 2xx" is too weak


def test_foreign_ids_answer_404_not_500_or_403_ambiguity(
    tenant_a: Tenant, tenant_b: Tenant
):
    """A resource in another workspace is indistinguishable from one that does
    not exist. Anything else confirms the id."""
    probes = [
        ("GET", f"/api/documents/{tenant_b.id('document')}"),
        ("GET", f"/api/projects/{tenant_b.id('project')}"),
        ("GET", f"/api/chunks/{tenant_b.id('chunk')}"),
        ("GET", f"/api/conversations/{tenant_b.id('conversation')}/messages"),
        ("GET", f"/api/db/connections/{tenant_b.id('db_connection')}/schema"),
        ("GET", f"/api/integrations/{tenant_b.id('integration_account')}/jobs"),
    ]
    missing = "00000000-0000-4000-8000-0000000000ff"
    for method, path in probes:
        foreign = tenant_a.client.request(method, path)
        absent = tenant_a.client.request(
            method, path.replace(path.split("/")[3], missing, 1)
        )
        assert foreign.status_code == 404, f"{path} -> {foreign.status_code}"
        assert foreign.status_code == absent.status_code


def test_bootstrap_hands_out_only_the_callers_own_identity_and_agent(
    tenant_a: Tenant, tenant_b: Tenant
):
    """Bootstrap is the one route that hands the client an id it did not ask for.

    The generic leak check is too weak here: "some other workspace's agent"
    happens to sort after A's most of the time, so it would pass by luck. Pin
    the value instead — the default agent must be A's own, and the identity must
    describe A.
    """
    body = tenant_a.client.get("/api/bootstrap").json()
    assert body["identity"]["user_id"] == tenant_a.user_id
    assert body["identity"]["workspace_id"] == tenant_a.workspace_id
    assert body["default_agent_id"] == tenant_a.id("agent")
    assert body["default_agent_id"] != tenant_b.id("agent")


def test_x_workspace_id_header_cannot_select_a_foreign_workspace(
    tenant_a: Tenant, tenant_b: Tenant
):
    response = tenant_a.client.get(
        "/api/documents", headers={"X-Workspace-Id": tenant_b.workspace_id}
    )
    assert response.status_code == 403
    assert_no_leak(response, tenant_b, "X-Workspace-Id selection")


def test_sse_stream_cannot_subscribe_to_a_foreign_run(
    tenant_a: Tenant, tenant_b: Tenant
):
    """The stream is the one route that keeps reading the database after the
    request is authorized, so it gets its own proof."""
    denied = tenant_a.client.get(f"/api/runs/{tenant_b.id('run')}/events")
    assert denied.status_code == 404
    assert "text/event-stream" not in denied.headers.get("content-type", "")
    assert_no_leak(denied, tenant_b, "run event stream")

    # Last-Event-ID is the other way in: it is a header, not a path id, and a
    # resumed stream must still be bound to the caller's workspace.
    resumed = tenant_a.client.get(
        f"/api/runs/{tenant_b.id('run')}/events",
        headers={"Last-Event-ID": "0"},
        params={"after": 0},
    )
    assert resumed.status_code == 404
    assert_no_leak(resumed, tenant_b, "resumed run event stream")


def test_own_run_stream_still_works(tenant_a: Tenant):
    """The isolation checks above would also pass if the route were simply
    broken, so prove the allowed case still streams."""
    db = SessionLocal()
    try:
        run = db.get(Run, tenant_a.id("run"))
        assert run is not None
        run.status = "completed"
        db.commit()
    finally:
        db.close()
    with tenant_a.client.stream(
        "GET", f"/api/runs/{tenant_a.id('run')}/events"
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "run.queued" in body


def test_private_app_is_not_reachable_through_the_published_surface(
    tenant_a: Tenant, tenant_b: Tenant
):
    """/published is unauthenticated by design; it must only serve apps whose
    owner made them public *and* published."""
    for path in (
        f"/published/apps/{tenant_b.id('app_slug')}",
        f"/published/apps/{tenant_b.id('app_slug')}/frame",
    ):
        anonymous = tenant_a.client.request("GET", path)
        assert anonymous.status_code == 404, path
        assert_no_leak(anonymous, tenant_b, path)


def test_generating_into_a_foreign_app_is_refused(
    tenant_a: Tenant, tenant_b: Tenant
):
    """The one app route whose body references nothing of the victim's.

    Every other /api/apps route re-filters the release by workspace on its way
    through, so they refuse even if the app lookup itself stopped filtering.
    Code generation with no datasets bound does not: if `_workspace_app` ever
    resolves by id alone, this writes a release into B's app and answers 201
    with B's name and description. It is the probe that actually pins the
    lookup.
    """
    before = workspace_digest(tenant_b.workspace_id)
    response = tenant_a.client.post(
        f"/api/apps/{tenant_b.id('app')}/generate",
        json={"prompt": "render something", "dataset_ids": []},
        headers={"Idempotency-Key": "foreign-app-generate-0001"},
    )
    assert response.status_code == 404, response.text
    assert_no_leak(response, tenant_b, "app generate")
    assert workspace_digest(tenant_b.workspace_id) == before

    # And the same call against A's own app succeeds, so the 404 above is the
    # workspace filter talking and not a broken route.
    own = tenant_a.client.post(
        f"/api/apps/{tenant_a.id('app')}/generate",
        json={"prompt": "render something", "dataset_ids": []},
        headers={"Idempotency-Key": "own-app-generate-0001"},
    )
    assert own.status_code == 201, own.text


def test_idempotency_replay_cannot_cross_workspaces(
    tenant_a: Tenant, tenant_b: Tenant
):
    """Replay records are looked up by (workspace, operation, key) and the row
    they point at is re-fetched. The same key used by both tenants must return
    each tenant's own resource, never the other's."""
    key = {"Idempotency-Key": "shared-idempotency-key-0001"}
    first = tenant_a.client.post(
        "/api/conversations", json={"title": "A's conversation"}, headers=key
    )
    assert first.status_code == 201
    second = tenant_b.client.post(
        "/api/conversations", json={"title": "B's conversation"}, headers=key
    )
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]

    replay = tenant_a.client.post(
        "/api/conversations", json={"title": "ignored"}, headers=key
    )
    assert replay.json()["id"] == first.json()["id"]


def test_oauth_state_from_another_workspace_is_bound_to_its_own_workspace(
    tenant_a: Tenant, tenant_b: Tenant
):
    """The integration callback trusts the state row, not the caller, for the
    workspace it writes into — so B's state must never land in A."""
    from app.models import IntegrationAccount

    before = workspace_digest(tenant_a.workspace_id)
    response = tenant_a.client.get(
        "/api/integrations/strava/callback",
        params={"state": tenant_b.id("oauth_state_value"), "error": "access_denied"},
        follow_redirects=False,
    )
    assert response.status_code < 500
    assert workspace_digest(tenant_a.workspace_id) == before, (
        "another workspace's OAuth state wrote into the caller's workspace"
    )
    db = SessionLocal()
    try:
        accounts = list(
            db.scalars(
                select(IntegrationAccount).where(
                    IntegrationAccount.workspace_id == tenant_a.workspace_id
                )
            )
        )
    finally:
        db.close()
    # Only the one the fixture planted; the callback added nothing.
    assert [account.id for account in accounts] == [
        tenant_a.id("integration_account")
    ]


# --------------------------------------------------------------------------
# Agent tools: the surface that never touches HTTP


@pytest.fixture
def registry_a(tenant_a: Tenant):
    db = SessionLocal()
    context = ToolContext(
        workspace_id=tenant_a.workspace_id,
        user_id=tenant_a.user_id,
        conversation_id=tenant_a.id("conversation"),
    )
    try:
        yield db, context, build_registry(db, context)
    finally:
        db.close()


def run_tool(registry_a, name: str, args: Dict[str, Any]) -> str:
    db, context, registry = registry_a
    assert name in registry, f"{name} is not in the registry: {sorted(registry)}"
    return registry[name].executor(db, context, args).content


def foreign_markers(victim: Tenant) -> List[str]:
    return [
        victim.workspace_id,
        victim.id("document"),
        victim.id("board"),
        victim.id("project"),
        victim.id("memory"),
        victim.id("dataset"),
        victim.id("db_connection"),
        victim.id("mcp_server"),
        f"{victim.label} secret",
    ]


def assert_tool_leaked_nothing(
    output: str, victim: Tenant, label: str, args: Any = None
) -> None:
    """No marker of the victim's may appear in the tool's output.

    An id the caller itself passed in is exempt: a tool that says "no memory
    with id X here" is echoing the attacker's own argument back, which reveals
    nothing. Anything the caller did not supply, it must not have learned.
    """
    supplied = json.dumps(args, default=str) if args else ""
    for marker in foreign_markers(victim):
        if marker and marker in supplied:
            continue
        assert marker not in output, f"tool {label} leaked {marker!r}"


@pytest.mark.parametrize(
    "tool,args_key",
    [
        ("read_document", "document_id"),
        ("read_board", "board_id"),
        ("fs_list", "project_id"),
        ("fs_read", "project_id"),
        ("query_dataset", "dataset_id"),
    ],
)
def test_agent_tools_reject_foreign_ids(
    registry_a, tenant_b: Tenant, tool: str, args_key: str
):
    kinds = {
        "document_id": "document",
        "board_id": "board",
        "project_id": "project",
        "dataset_id": "dataset",
    }
    args: Dict[str, Any] = {args_key: tenant_b.id(kinds[args_key])}
    if tool == "fs_read":
        args["path"] = "index.tsx"
    if tool == "query_dataset":
        args["query"] = {"limit": 5}
    output = run_tool(registry_a, tool, args)
    assert_tool_leaked_nothing(output, tenant_b, tool, args)


@pytest.mark.parametrize(
    "tool,args",
    [
        ("read_document", {"title": "Bravo brief"}),
        ("read_board", {"name": "Bravo board"}),
        ("fs_list", {"project": "Bravo project"}),
        ("describe_schema", {"connection": "Bravo warehouse"}),
    ],
)
def test_agent_tools_reject_foreign_names(
    registry_a, tenant_b: Tenant, tool: str, args: Dict[str, Any]
):
    """Name-based addressing is the likeliest leak: `resolve`/`find_by_name`
    helpers that forget the workspace filter."""
    output = run_tool(registry_a, tool, args)
    assert_tool_leaked_nothing(output, tenant_b, tool, args)


def test_graph_lookup_resolves_the_callers_entity_not_the_other_tenants(
    registry_a, tenant_a: Tenant, tenant_b: Tenant
):
    """Both workspaces have an entity called "Atlas". A must get its own."""
    replant_graph(tenant_a)
    replant_graph(tenant_b)
    output = run_tool(registry_a, "graph_lookup", {"entity": "Atlas"})
    assert tenant_a.id("graph_entity_neighbour") not in output  # ids are not emitted
    assert "Alpha Neighbour" in output
    assert "Bravo Neighbour" not in output
    assert_tool_leaked_nothing(output, tenant_b, "graph_lookup")

    walk = run_tool(registry_a, "graph_neighbors", {"entity": "Atlas"})
    assert "Alpha Neighbour" in walk
    assert "Bravo Neighbour" not in walk


def test_graph_tools_cannot_resolve_an_entity_only_the_other_tenant_has(
    registry_a, tenant_a: Tenant, tenant_b: Tenant
):
    """The shared "Atlas" name proves A gets *a* match; it does not prove the
    match was filtered, because A's own row may simply have sorted first. Only
    B has an entity called "Bravo Neighbour" — A must not be able to see it, and
    must not be told it exists."""
    replant_graph(tenant_a)
    replant_graph(tenant_b)
    for tool in ("graph_lookup", "graph_neighbors"):
        output = run_tool(registry_a, tool, {"entity": "Bravo Neighbour"})
        assert "No graph entity" in output, (
            f"{tool} resolved another tenant's entity: {output}"
        )
    path = run_tool(
        registry_a,
        "graph_path",
        {"from_entity": "Atlas", "to_entity": "Bravo Neighbour"},
    )
    assert "No graph entity" in path, path


def test_an_edge_pointing_out_of_the_workspace_resolves_no_foreign_name(
    registry_a, tenant_a: Tenant, tenant_b: Tenant
):
    """The one place a workspace filter is *derived* rather than stated.

    Neighbour names are looked up by the ids on an edge, and those edges are
    workspace-scoped — so the id set "must" already be local. Nothing enforces
    it: the foreign key points at graph_entities, not at (workspace_id, id). An
    edge in A that points at an entity in B is therefore representable, and if
    the name lookup trusted the derivation it would read B's row out. Plant
    exactly that edge and check no foreign name comes back.
    """
    replant_graph(tenant_a)
    replant_graph(tenant_b)
    db = SessionLocal()
    try:
        db.add(
            GraphEdge(
                workspace_id=tenant_a.workspace_id,
                from_entity_id=tenant_a.id("graph_entity"),
                to_entity_id=tenant_b.id("graph_entity_neighbour"),
                relation="mentions",
                weight=9,
            )
        )
        db.commit()
    finally:
        db.close()
    try:
        output = run_tool(registry_a, "graph_lookup", {"entity": "Atlas"})
        assert "Bravo Neighbour" not in output, output
        digest = run_tool(registry_a, "recall_memory", {"query": "Atlas"})
        assert "Bravo Neighbour" not in digest, digest
    finally:
        replant_graph(tenant_a)


def test_memory_tools_cannot_read_or_tombstone_foreign_memories(
    registry_a, tenant_b: Tenant
):
    search = run_tool(registry_a, "search_memory", {"query": "secret memory"})
    assert_tool_leaked_nothing(search, tenant_b, "search_memory")

    recall = run_tool(registry_a, "recall_memory", {"query": "secret memory"})
    assert_tool_leaked_nothing(recall, tenant_b, "recall_memory")

    forget_by_id = run_tool(
        registry_a, "forget", {"memory_id": tenant_b.id("memory")}
    )
    assert forget_by_id.startswith("Error:")

    forget_by_text = run_tool(registry_a, "forget", {"content": "Bravo secret memory"})
    assert forget_by_text.startswith("Error:")

    db = SessionLocal()
    try:
        item = db.get(MemoryItem, tenant_b.id("memory"))
        assert item is not None and item.status == "active"
    finally:
        db.close()


def test_listing_tools_only_see_the_callers_rows(registry_a, tenant_b: Tenant):
    for tool in ("list_documents", "list_projects", "list_datasets", "list_connections"):
        output = run_tool(registry_a, tool, {})
        assert_tool_leaked_nothing(output, tenant_b, tool)


def test_mcp_registry_excludes_other_workspaces_servers(
    registry_a, tenant_a: Tenant, tenant_b: Tenant
):
    _db, _context, registry = registry_a
    assert "mcp__alpha-server__read_secret" in registry
    assert "mcp__bravo-server__read_secret" not in registry


def test_write_tools_cannot_mutate_a_foreign_workspace(registry_a, tenant_b: Tenant):
    before = workspace_digest(tenant_b.workspace_id)
    attempts = [
        (
            "edit_document",
            {
                "document_id": tenant_b.id("document"),
                "find": "Bravo",
                "replace": "Alpha",
            },
        ),
        (
            "edit_document",
            {"title": "Bravo brief", "find": "Bravo", "replace": "Alpha"},
        ),
        (
            "board_add_card",
            {"board_id": tenant_b.id("board"), "column": "Todo", "title": "by A"},
        ),
        ("board_add_card", {"board": "Bravo board", "column": "Todo", "title": "by A"}),
        (
            "board_move_card",
            {"board_id": tenant_b.id("board"), "card": "Bravo secret card", "column": "Done"},
        ),
        (
            "board_delete_card",
            {"board_id": tenant_b.id("board"), "card": "Bravo secret card"},
        ),
        (
            "board_rename_column",
            {"board_id": tenant_b.id("board"), "column": "Todo", "name": "by A"},
        ),
        (
            "fs_write",
            {
                "project_id": tenant_b.id("project"),
                "path": "planted.tsx",
                "content": "by A",
            },
        ),
        ("fs_write", {"project": "Bravo project", "path": "planted.tsx", "content": "by A"}),
        ("fs_delete", {"project_id": tenant_b.id("project"), "path": "index.tsx"}),
        ("forget", {"memory_id": tenant_b.id("memory")}),
    ]
    for name, args in attempts:
        output = run_tool(registry_a, name, args)
        assert_tool_leaked_nothing(output, tenant_b, name, args)
    assert workspace_digest(tenant_b.workspace_id) == before, (
        "an agent tool mutated another workspace"
    )


def test_write_tools_still_work_inside_the_callers_workspace(
    registry_a, tenant_a: Tenant
):
    """The refusals above must come from the workspace filter, not from the
    tools being inert."""
    output = run_tool(
        registry_a,
        "edit_document",
        {"title": "Alpha brief", "find": "secret", "replace": "edited"},
    )
    assert not output.startswith("Error:"), output
    db = SessionLocal()
    try:
        document = db.get(Document, tenant_a.id("document"))
        assert document is not None
        assert "edited document body" in document.content
    finally:
        db.close()


def test_create_by_name_does_not_collide_across_workspaces(
    registry_a, tenant_a: Tenant, tenant_b: Tenant
):
    """Uniqueness is per workspace. A must be able to create a document, board
    and project named exactly like B's — a global uniqueness check would both
    leak B's names and deny A a legitimate write."""
    for tool, args, table, column in (
        ("create_document", {"title": "Bravo brief", "content": "A's own"}, Document, "title"),
        ("create_board", {"name": "Bravo board"}, Board, "name"),
        ("create_project", {"name": "Bravo project"}, Project, "name"),
    ):
        output = run_tool(registry_a, tool, args)
        assert not output.startswith("Error:"), f"{tool}: {output}"
        db = SessionLocal()
        try:
            rows = list(
                db.scalars(
                    select(table).where(table.workspace_id == tenant_a.workspace_id)
                )
            )
        finally:
            db.close()
        assert any(getattr(row, column).startswith("Bravo") for row in rows)


# --------------------------------------------------------------------------
# The primary-key fetches


DB_GET_ALLOWLIST = {
    # (file, symbol fetched): why fetching by primary key alone is safe here.
    ("app/auth.py", "Workspace"): "id comes from the caller's own membership row",
    ("app/auth.py", "User"): "id comes from the caller's own session row",
    ("app/auth.py", "Agent"): "dev seed only; guarded by is_dev_env",
    ("app/auth.py", "Tool"): "dev seed only; guarded by is_dev_env",
    ("app/api/auth.py", "User"): "id comes from the caller's session or token row",
    ("app/api/auth.py", "Workspace"): "id comes from the caller's membership row",
    ("app/api/auth.py", "UserSession"): "id comes from the caller's own actor",
    ("app/api/chat.py", "Conversation"): "workspace re-checked on the next line",
    ("app/api/generated_apps.py", "AppRelease"): "workspace re-checked on the next line",
    ("app/api/integrations.py", "IntegrationAccount"): "id from a scoped replay row",
    ("app/api/integrations.py", "SyncJob"): "id from a scoped replay row",
    ("app/services/ingestion.py", "Source"): "worker; id from an authorized route",
    ("app/services/memory.py", "Run"): "worker; id from an authorized route",
    ("app/services/agent_loop.py", "Agent"): "workspace re-checked on the next line",
    ("app/services/agent_loop.py", "AgentToolCall"): "id is the loop's own row",
    ("app/services/agent_loop.py", "Conversation"): "workspace re-checked on the next line",
    ("app/services/agent_loop.py", "Document"): "workspace re-checked on the next line",
    ("app/api/doc_pending.py", "Run"): "workspace re-checked on the next line",
    ("app/services/runs.py", "Run"): "worker; id from an authorized route",
    ("app/services/runs.py", "ToolCall"): "worker; id from an authorized route",
    ("app/services/connectors/strava.py", "IntegrationAccount"): "worker; scoped id",
    ("app/services/connectors/gmail.py", "IntegrationAccount"): "worker; scoped id",
    ("app/services/connectors/garmin.py", "IntegrationAccount"): "worker; scoped id",
    ("app/services/connectors/sync_jobs.py", "SyncJob"): "worker; scoped id",
    ("app/services/connectors/sync_jobs.py", "IntegrationAccount"): "read off the job",
    ("app/services/mcp/registry.py", "McpServer"): "id bound from a scoped query",
    ("app/services/workflows/executor.py", "WorkflowRun"): (
        "worker; id from an authorized route or the run's own row"
    ),
    ("app/services/workflows/executor.py", "Run"): "id read off the workflow run",
    ("app/services/workflows/executor.py", "AgentToolCall"): (
        "id is the row the executor just wrote"
    ),
}


def test_every_db_get_call_site_is_reviewed():
    """`db.get(Model, id)` fetches by primary key with no workspace filter.

    Each existing call site has been checked and recorded above. This fails when
    a new one appears, so "fetch by id" is a decision someone makes on purpose
    rather than a habit that quietly spreads.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    pattern = re.compile(r"\bdb(?:_session)?\.get\(\s*([A-Z]\w+)")
    found = set()
    for path in sorted((root / "app").rglob("*.py")):
        for match in pattern.finditer(path.read_text()):
            found.add((str(path.relative_to(root)), match.group(1)))
    unreviewed = found - set(DB_GET_ALLOWLIST)
    assert not unreviewed, (
        "new primary-key fetches with no workspace filter; review each one and "
        f"record why it is safe: {sorted(unreviewed)}"
    )


def test_dashboard_run_reads_only_its_own_workspaces_dataset(
    tenant_a: Tenant, tenant_b: Tenant
):
    """A dashboard stores a dataset_id. Running it must resolve that id inside
    the dashboard's own workspace, never by primary key alone."""
    db = SessionLocal()
    try:
        dashboard = db.get(Dashboard, tenant_a.id("dashboard"))
        assert dashboard is not None
        dashboard.dataset_id = tenant_b.id("dataset")
        db.commit()
        response = tenant_a.client.post(f"/api/dashboards/{tenant_a.id('dashboard')}/run")
        assert response.status_code in {404, 422}, response.text
        assert_no_leak(response, tenant_b, "dashboard run")
    finally:
        db.close()


def test_board_card_ids_are_scoped_to_the_board_not_the_table(
    tenant_a: Tenant, tenant_b: Tenant
):
    """Card and column operations take a board (workspace-checked) plus a raw
    card id. The card must be looked up inside that board."""
    before = workspace_digest(tenant_b.workspace_id)
    moved = tenant_a.client.post(
        f"/api/boards/{tenant_a.id('board')}/cards/{tenant_b.id('board_card')}/move",
        params={"column": "Done"},
    )
    assert moved.status_code == 422
    deleted = tenant_a.client.delete(
        f"/api/boards/{tenant_a.id('board')}/cards/{tenant_b.id('board_card')}"
    )
    assert deleted.status_code == 404
    renamed = tenant_a.client.patch(
        f"/api/board-ops/{tenant_a.id('board')}/columns/{tenant_b.id('board_column')}",
        json={"name": "renamed by A"},
    )
    assert renamed.status_code == 422
    assert workspace_digest(tenant_b.workspace_id) == before


def test_document_version_ids_are_scoped_to_the_document(
    tenant_a: Tenant, tenant_b: Tenant
):
    response = tenant_a.client.post(
        f"/api/documents/{tenant_a.id('document')}"
        f"/versions/{tenant_b.id('document_version')}/restore"
    )
    assert response.status_code == 404
    assert_no_leak(response, tenant_b, "document version restore")


def test_app_release_ids_are_scoped_to_the_app(tenant_a: Tenant, tenant_b: Tenant):
    for path in (
        f"/api/apps/{tenant_a.id('app')}/releases/{tenant_b.id('app_release')}/frame",
    ):
        response = tenant_a.client.get(path)
        assert response.status_code == 404, path
        assert_no_leak(response, tenant_b, path)
    for path in (
        f"/api/apps/{tenant_a.id('app')}/releases/{tenant_b.id('app_release')}/publish",
        f"/api/apps/{tenant_a.id('app')}/rollback/{tenant_b.id('app_release')}",
    ):
        response = tenant_a.client.post(
            path, headers={"Idempotency-Key": "release-scope-" + path[-12:]}
        )
        assert response.status_code == 404, path
        assert_no_leak(response, tenant_b, path)


def test_sync_jobs_are_scoped_to_their_account(tenant_a: Tenant, tenant_b: Tenant):
    response = tenant_a.client.get(
        f"/api/integrations/{tenant_b.id('integration_account')}/jobs"
    )
    assert response.status_code == 404
    assert json.dumps(response.json()).count("Bravo") == 0
