from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import McpServer, McpTool
from app.services.llm_tools import ToolContext
from app.services.mcp import (
    McpError,
    ServerConfig,
    call_tool,
    list_tools,
    qualified_name,
    refresh_server_tools,
    registry_tools,
    split_qualified,
)
from app.services.mcp.client import MAX_RESULT_CHARS

FIXTURE = str(Path(__file__).parent / "fixtures" / "echo_mcp_server.py")


def _config(**overrides) -> ServerConfig:
    base = dict(
        name="echo",
        transport="stdio",
        command=sys.executable,
        args=[FIXTURE],
        env={"FIXTURE_SECRET": "swordfish"},
    )
    base.update(overrides)
    return ServerConfig(**base)


def test_list_tools_discovers_the_fixture_server():
    tools = list_tools(_config())
    names = {tool.name for tool in tools}
    assert {"echo", "read_env", "explode", "firehose"} <= names
    echo = next(tool for tool in tools if tool.name == "echo")
    assert echo.input_schema["type"] == "object"
    assert "message" in echo.input_schema["properties"]


def test_call_tool_round_trips_arguments():
    assert call_tool(_config(), "echo", {"message": "hi"}) == "echo: hi"


def test_env_secrets_reach_the_server_process():
    assert call_tool(_config(), "read_env", {}) == "swordfish"


def test_tool_errors_come_back_as_text_not_exceptions():
    result = call_tool(_config(), "explode", {})
    assert "error" in result.lower()


def test_oversized_results_are_truncated():
    result = call_tool(_config(), "firehose", {})
    assert len(result) <= MAX_RESULT_CHARS + 20
    assert result.endswith("(truncated)")


def test_unreachable_server_raises_mcp_error():
    with pytest.raises(McpError):
        list_tools(_config(command="/nonexistent/binary", args=[]))


def test_timeout_is_bounded():
    """A server that never speaks the protocol must not hang the caller."""
    with pytest.raises(McpError):
        list_tools(
            _config(command=sys.executable, args=["-c", "import time; time.sleep(30)"]),
            timeout=3.0,
        )


def test_qualified_name_round_trip():
    assert qualified_name("files", "read_file") == "mcp__files__read_file"
    assert split_qualified("mcp__files__read_file") == ("files", "read_file")
    with pytest.raises(ValueError):
        split_qualified("search_sources")


def test_refresh_caches_tools_and_registry_exposes_them(client):
    identity = client.get("/api/bootstrap").json()["identity"]
    db = SessionLocal()
    try:
        server = McpServer(
            workspace_id=identity["workspace_id"],
            name="echo",
            transport="stdio",
            command=sys.executable,
            args_json=json.dumps([FIXTURE]),
        )
        db.add(server)
        db.commit()

        tools = refresh_server_tools(db, server)
        assert {tool.name for tool in tools} >= {"echo", "explode"}
        db.refresh(server)
        assert server.status == "ready"
        assert server.last_connected_at is not None

        context = ToolContext(
            workspace_id=identity["workspace_id"],
            user_id=identity["user_id"],
            conversation_id="none",
        )
        specs = registry_tools(db, context)
        assert "mcp__echo__echo" in specs
        # MCP tools are arbitrary, so they must not default to running unattended.
        assert specs["mcp__echo__echo"].read_only is False

        result = specs["mcp__echo__echo"].executor(db, context, {"message": "via registry"})
        assert result.content == "echo: via registry"

        # Disabling a tool removes it from what the model can see.
        db.query(McpTool).filter(McpTool.name == "explode").update({"enabled": False})
        db.commit()
        assert "mcp__echo__explode" not in registry_tools(db, context)
    finally:
        db.query(McpTool).delete()
        db.query(McpServer).delete()
        db.commit()
        db.close()


def test_refresh_records_the_error_for_a_bad_server(client):
    identity = client.get("/api/bootstrap").json()["identity"]
    db = SessionLocal()
    try:
        server = McpServer(
            workspace_id=identity["workspace_id"],
            name="broken",
            transport="stdio",
            command="/nonexistent/binary",
        )
        db.add(server)
        db.commit()
        with pytest.raises(McpError):
            refresh_server_tools(db, server)
        db.refresh(server)
        assert server.status == "error"
        assert server.last_error
    finally:
        db.query(McpServer).delete()
        db.commit()
        db.close()
