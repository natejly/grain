"""Grain-as-MCP-server: the JSON-RPC surface at `POST /api/mcp`.

The tools are the real registry; the model is never involved (these call
read-only executors directly through the RPC). What is under test is the
protocol envelope, the bearer-token gate, the read-only narrowing, and the
unattended-scope policy refusal.
"""
from __future__ import annotations

import uuid

from app.database import SessionLocal
from app.models import ApiToken, ToolPolicy
from app.services import api_tokens as token_service


def _mint(client) -> str:
    """A live token secret for the seeded workspace, via the owner route."""
    response = client.post(
        "/api/api-tokens",
        headers={"Idempotency-Key": "mcp-token-" + uuid.uuid4().hex},
        json={"name": "test agent"},
    )
    assert response.status_code == 201, response.text
    secret = response.json()["secret"]
    assert secret.startswith("grain_")
    return secret


def _rpc(client, secret: str, method: str, params: dict | None = None, rpc_id=1):
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(
        "/api/mcp", headers={"Authorization": f"Bearer {secret}"}, json=body
    )


def _cleanup_tokens() -> None:
    db = SessionLocal()
    try:
        db.query(ApiToken).delete()
        db.query(ToolPolicy).delete()
        db.commit()
    finally:
        db.close()


def test_a_missing_or_bad_bearer_is_a_401(client):
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert client.post("/api/mcp", json=ping).status_code == 401
    bad = client.post(
        "/api/mcp",
        headers={"Authorization": "Bearer grain_not-a-real-token"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )
    assert bad.status_code == 401


def test_initialize_advertises_the_tools_capability(client):
    secret = _mint(client)
    try:
        response = _rpc(client, secret, "initialize")
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["protocolVersion"]
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"] == "grain"
    finally:
        _cleanup_tokens()


def test_tools_list_offers_only_read_only_tools(client):
    secret = _mint(client)
    try:
        response = _rpc(client, secret, "tools/list")
        assert response.status_code == 200
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        # Read-only research tools are present...
        assert "search_sources" in names
        assert "search_conversations" in names
        # ...and everything write-capable, force_ask, or loop-bound is absent.
        for absent in (
            "create_document",
            "edit_document",
            "fs_write",
            "remember",
            "ask_user",
            "delegate",
        ):
            assert absent not in names
        # Every offered tool carries an MCP inputSchema.
        for tool in response.json()["result"]["tools"]:
            assert tool["inputSchema"]["type"] == "object"
    finally:
        _cleanup_tokens()


def test_tools_call_runs_a_read_only_tool(client):
    secret = _mint(client)
    try:
        response = _rpc(
            client,
            secret,
            "tools/call",
            {"name": "list_datasets", "arguments": {}},
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"] is False
        assert result["content"][0]["type"] == "text"
    finally:
        _cleanup_tokens()


def test_the_knowledge_graph_is_shared_over_mcp(client):
    """`graph_export` rides the read-only registry onto the MCP surface, so an
    external client can pull the workspace's knowledge graph in one call."""
    import json

    secret = _mint(client)
    try:
        listed = _rpc(client, secret, "tools/list")
        names = {tool["name"] for tool in listed.json()["result"]["tools"]}
        assert {"graph_export", "graph_neighbors", "graph_path"} <= names
        response = _rpc(
            client,
            secret,
            "tools/call",
            {"name": "graph_export", "arguments": {"limit": 5}},
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"] is False
        snapshot = json.loads(result["content"][0]["text"])
        assert {"status", "entities", "edges"} <= set(snapshot)
    finally:
        _cleanup_tokens()


def test_an_unknown_tool_is_an_rpc_tool_error_not_a_crash(client):
    secret = _mint(client)
    try:
        response = _rpc(
            client, secret, "tools/call", {"name": "no_such_tool", "arguments": {}}
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"] is True
        assert "Unknown tool" in result["content"][0]["text"]
    finally:
        _cleanup_tokens()


def test_a_workflow_scope_deny_refuses_the_call(client):
    secret = _mint(client)
    db = SessionLocal()
    try:
        identity = client.get("/api/bootstrap").json()["identity"]
        db.add(
            ToolPolicy(
                workspace_id=identity["workspace_id"],
                tool_name="list_datasets",
                policy="deny",
                scope="workflow",
            )
        )
        db.commit()
        response = _rpc(
            client, secret, "tools/call", {"name": "list_datasets", "arguments": {}}
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"] is True
        assert "policy does not allow" in result["content"][0]["text"]
    finally:
        _cleanup_tokens()
        db.close()


def test_a_notification_is_accepted_with_no_body(client):
    secret = _mint(client)
    try:
        response = client.post(
            "/api/mcp",
            headers={"Authorization": f"Bearer {secret}"},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert response.status_code == 202
        assert response.content == b""
    finally:
        _cleanup_tokens()


def test_an_unknown_method_is_method_not_found(client):
    secret = _mint(client)
    try:
        response = _rpc(client, secret, "resources/list")
        assert response.status_code == 200
        assert response.json()["error"]["code"] == -32601
    finally:
        _cleanup_tokens()


def test_a_revoked_token_stops_working(client):
    secret = _mint(client)
    try:
        assert _rpc(client, secret, "ping").status_code == 200
        db = SessionLocal()
        try:
            from app.services.api_tokens import _digest

            token = (
                db.query(ApiToken)
                .filter(ApiToken.token_hash == _digest(secret))
                .one()
            )
            token_id = token.id
        finally:
            db.close()
        assert client.delete(f"/api/api-tokens/{token_id}").status_code == 204
        assert _rpc(client, secret, "ping").status_code == 401
    finally:
        _cleanup_tokens()


def test_resolve_declines_a_token_whose_member_left(client):
    """The token acts AS a member; access the member lost, the token loses."""
    secret = _mint(client)
    db = SessionLocal()
    try:
        from app.services.api_tokens import _digest

        token = db.query(ApiToken).filter(ApiToken.token_hash == _digest(secret)).one()
        # Simulate the member leaving by pointing the token at a stranger.
        token.user_id = "user-who-is-not-a-member"
        db.commit()
        assert token_service.resolve(db, secret) is None
    finally:
        _cleanup_tokens()
        db.close()


def test_malformed_bodies_never_500(client):
    """The isolation harness forbids a 500 from any route; the JSON-RPC handler
    must answer even a hostile body with a 2xx JSON-RPC error, not a crash."""
    secret = _mint(client)
    try:
        headers = {"Authorization": f"Bearer {secret}"}
        # Not JSON at all.
        r = client.post("/api/mcp", headers=headers, content=b"{not json")
        assert r.status_code == 200 and r.json()["error"]["code"] == -32700
        # JSON, but not a JSON-RPC 2.0 object.
        for body in ([], "a string", 42, {"jsonrpc": "1.0", "method": "ping"}):
            r = client.post("/api/mcp", headers=headers, json=body)
            assert r.status_code == 200, body
            assert r.json()["error"]["code"] == -32600, body
        # Valid envelope, but params the wrong type / missing name.
        for params in (None, [], "x", {}, {"name": 123}, {"arguments": "no"}):
            r = client.post(
                "/api/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
            )
            assert r.status_code == 200, params
            assert r.status_code != 500
        # A notification (no id) with a garbage method still 202s.
        r = client.post(
            "/api/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/whatever"},
        )
        assert r.status_code == 202
    finally:
        _cleanup_tokens()


def test_a_non_finite_or_non_scalar_id_never_500s(client):
    """json.loads accepts NaN/Infinity, which then crash JSONResponse's
    allow_nan=False dump OUTSIDE the body-parse try. Rejected at the parse
    boundary now — a crafted id must be a clean JSON-RPC error, never a 500."""
    secret = _mint(client)
    try:
        headers = {"Authorization": f"Bearer {secret}"}
        for raw in (
            b'{"jsonrpc":"2.0","id":NaN,"method":"ping"}',
            b'{"jsonrpc":"2.0","id":Infinity,"method":"ping"}',
            b'{"jsonrpc":"2.0","id":-Infinity,"method":"tools/list"}',
        ):
            r = client.post("/api/mcp", headers=headers, content=raw)
            assert r.status_code == 200, raw
            assert r.json()["error"]["code"] == -32700, raw
        # A container id is a well-formed JSON but an invalid JSON-RPC id.
        r = client.post(
            "/api/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": {"nested": 1}, "method": "ping"},
        )
        assert r.status_code == 200
        assert r.json()["error"]["code"] == -32600
    finally:
        _cleanup_tokens()
