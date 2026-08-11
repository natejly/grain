"""Bridge configured MCP servers into the agent loop's tool registry."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...clock import utcnow
from ...config import Settings, get_settings
from ...models import McpServer, McpTool
from ..crypto import EncryptionNotConfiguredError, decrypt_secret, encrypt_secret
from ..llm_tools import ToolContext, ToolResult, ToolSpec
from .client import (
    McpAuthRequired,
    McpError,
    ServerConfig,
    TokenProvider,
    call_tool,
    list_tools,
)
from .oauth import McpOAuthError, token_provider

# Tool names reach the model as mcp__<server>__<tool>; the separator has to be
# something a server name cannot contain (names are slug-validated on write).
NAMESPACE = "mcp__"
SEPARATOR = "__"


def qualified_name(server_name: str, tool_name: str) -> str:
    return f"{NAMESPACE}{server_name}{SEPARATOR}{tool_name}"


def split_qualified(name: str) -> tuple[str, str]:
    """mcp__files__read_file -> ("files", "read_file"). Raises on a bad shape."""
    if not name.startswith(NAMESPACE):
        raise ValueError("not an MCP tool name")
    remainder = name[len(NAMESPACE) :]
    server, separator, tool = remainder.partition(SEPARATOR)
    if not separator or not server or not tool:
        raise ValueError("not an MCP tool name")
    return server, tool


def pack_secrets(secrets: Dict[str, str]) -> str:
    if not secrets:
        return ""
    return encrypt_secret(json.dumps(secrets))


def unpack_secrets(server: McpServer) -> Dict[str, str]:
    if not server.secrets_encrypted:
        return {}
    try:
        loaded = json.loads(decrypt_secret(server.secrets_encrypted))
    except EncryptionNotConfiguredError:
        raise
    except Exception:
        # A key rotation should disable the server loudly, not silently send an
        # MCP server a garbled environment.
        raise McpError("Stored credentials could not be decrypted") from None
    return {str(k): str(v) for k, v in loaded.items()} if isinstance(loaded, dict) else {}


def server_config(
    server: McpServer, settings: Optional[Settings] = None
) -> ServerConfig:
    settings = settings or get_settings()
    if server.transport == "stdio" and not settings.mcp_stdio_allowed:
        # The registration endpoint refuses new stdio servers outside
        # development, but a row could predate that gate or be inserted directly;
        # this is the boundary that actually runs the command, so it refuses too.
        # Both callers turn an McpError into a visible "error" status rather than
        # a spawn, so a stale stdio row is inert instead of live RCE.
        raise McpError(
            "stdio MCP servers run on the API host and are disabled outside "
            "development"
        )
    secrets = unpack_secrets(server)
    try:
        args = json.loads(server.args_json)
    except (ValueError, TypeError):
        args = []
    return ServerConfig(
        name=server.name,
        transport=server.transport,
        command=server.command,
        args=[str(item) for item in args] if isinstance(args, list) else [],
        env=secrets if server.transport == "stdio" else {},
        url=server.url,
        headers=secrets if server.transport == "http" else {},
    )


def tokens_for(
    db: Session,
    server: McpServer,
    user_id: str,
    settings: Optional[Settings] = None,
) -> Optional[TokenProvider]:
    """The OAuth token source for this (server, user), or None.

    None for stdio and for anonymous callers, which is what keeps a local
    subprocess server and the existing tests on exactly the old code path.
    """
    if server.transport != "http" or not user_id:
        return None
    return token_provider(db, server, user_id, settings or get_settings())


def refresh_server_tools(
    db: Session, server: McpServer, *, user_id: str = ""
) -> List[McpTool]:
    """Reconnect, re-enumerate, and reconcile the cached tool rows."""
    try:
        discovered = list_tools(
            server_config(server), tokens=tokens_for(db, server, user_id)
        )
    except McpAuthRequired as exc:
        # A server behind OAuth is not a broken server. Saying so is what lets
        # the UI offer Connect instead of a retry button that can never work.
        server.status = "needs_auth"
        server.last_error = str(exc)[:1000]
        db.commit()
        raise
    except McpError as exc:
        server.status = "error"
        server.last_error = str(exc)[:1000]
        db.commit()
        raise

    existing = {
        row.name: row
        for row in db.scalars(select(McpTool).where(McpTool.server_id == server.id))
    }
    seen = set()
    for info in discovered:
        seen.add(info.name)
        row = existing.get(info.name)
        if row is None:
            row = McpTool(
                workspace_id=server.workspace_id,
                server_id=server.id,
                name=info.name,
            )
            db.add(row)
        row.description = info.description[:2000]
        row.input_schema_json = json.dumps(info.input_schema)
    for name, row in existing.items():
        if name not in seen:
            db.delete(row)

    server.status = "ready"
    server.last_error = ""
    server.last_connected_at = utcnow()
    db.commit()
    return list(db.scalars(select(McpTool).where(McpTool.server_id == server.id)))


def _executor(server_id: str, tool_name: str):
    def execute(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
        server = db.get(McpServer, server_id)
        if server is None or not server.enabled:
            return ToolResult(content="Error: that MCP server is no longer available.")
        try:
            content = call_tool(
                server_config(server),
                tool_name,
                args,
                tokens=tokens_for(db, server, context.user_id),
            )
        except McpAuthRequired:
            # Addressed to the model, which cannot click Connect; naming the
            # server and the remedy is what stops it retrying the same call.
            return ToolResult(
                content=(
                    f"Error: the “{server.name}” MCP server needs the user to "
                    "connect their account in Settings before its tools work."
                )
            )
        except (McpError, McpOAuthError) as exc:
            return ToolResult(content=f"Error: {exc}")
        return ToolResult(content=content)

    return execute


def registry_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """Every enabled tool on every enabled server, namespaced and ask-by-default.

    Discovery is read from the cache rather than the network: building the
    registry happens on every turn, and the agent loop is synchronous.
    """
    rows = db.execute(
        select(McpTool, McpServer)
        .join(McpServer, McpServer.id == McpTool.server_id)
        .where(
            McpTool.workspace_id == context.workspace_id,
            McpTool.enabled.is_(True),
            McpServer.enabled.is_(True),
        )
    ).all()
    specs: Dict[str, ToolSpec] = {}
    for tool, server in rows:
        try:
            schema = json.loads(tool.input_schema_json)
        except (ValueError, TypeError):
            schema = {"type": "object", "properties": {}}
        name = qualified_name(server.name, tool.name)
        description = tool.description or f"{tool.name} on the {server.name} MCP server"
        specs[name] = ToolSpec(
            name=name,
            description=description[:1000],
            parameters=schema,
            executor=_executor(server.id, tool.name),
            # An MCP server's tools are arbitrary and may write, so they default
            # to prompting; a workspace ToolPolicy row can promote them to allow.
            read_only=False,
        )
    return specs
