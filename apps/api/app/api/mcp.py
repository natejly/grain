from __future__ import annotations

import json
import re
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import McpServer, McpTool
from ..schemas import McpServerOut, McpServerRequest, McpToolOut
from ..services.audit import record_audit
from ..services.crypto import EncryptionNotConfiguredError
from ..services.mcp import McpError, pack_secrets, refresh_server_tools

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

# Server names namespace the tools the model sees (mcp__<name>__<tool>), so they
# must not contain the separator or anything the model API rejects in a name.
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$")


def _server_out(server: McpServer, tools: List[McpTool]) -> McpServerOut:
    try:
        args = json.loads(server.args_json)
    except (ValueError, TypeError):
        args = []
    return McpServerOut(
        id=server.id,
        name=server.name,
        transport=server.transport,
        command=server.command,
        args=args if isinstance(args, list) else [],
        url=server.url,
        enabled=server.enabled,
        status=server.status,
        last_error=server.last_error,
        has_secrets=bool(server.secrets_encrypted),
        tools=[
            McpToolOut(
                id=tool.id,
                name=tool.name,
                description=tool.description,
                enabled=tool.enabled,
            )
            for tool in tools
        ],
        created_at=server.created_at,
    )


def _tools_for(db: Session, server_id: str) -> List[McpTool]:
    return list(
        db.scalars(
            select(McpTool)
            .where(McpTool.server_id == server_id)
            .order_by(McpTool.name.asc())
        )
    )


def _load(db: Session, actor: Actor, server_id: str) -> McpServer:
    server = db.scalar(
        select(McpServer).where(
            McpServer.id == server_id, McpServer.workspace_id == actor.workspace_id
        )
    )
    if server is None:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return server


def _validate(payload: McpServerRequest) -> None:
    if not NAME_RE.match(payload.name):
        raise HTTPException(
            status_code=422,
            detail="Name must be lowercase letters, digits, and dashes",
        )
    if payload.transport == "stdio" and not payload.command.strip():
        raise HTTPException(status_code=422, detail="A stdio server needs a command")
    if payload.transport == "http" and not payload.url.strip():
        raise HTTPException(status_code=422, detail="An HTTP server needs a URL")
    if payload.transport == "http" and not payload.url.startswith("https://"):
        # Bearer tokens ride in these headers; refuse to send them in the clear.
        if not payload.url.startswith("http://127.0.0.1") and not payload.url.startswith(
            "http://localhost"
        ):
            raise HTTPException(
                status_code=422, detail="Remote MCP servers must use https://"
            )


@router.get("/servers", response_model=List[McpServerOut])
def list_servers(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[McpServerOut]:
    servers = list(
        db.scalars(
            select(McpServer)
            .where(McpServer.workspace_id == actor.workspace_id)
            .order_by(McpServer.created_at.asc())
        )
    )
    return [_server_out(server, _tools_for(db, server.id)) for server in servers]


@router.post("/servers", response_model=McpServerOut, status_code=201)
def create_server(
    payload: McpServerRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> McpServerOut:
    _validate(payload)
    duplicate = db.scalar(
        select(McpServer).where(
            McpServer.workspace_id == actor.workspace_id,
            McpServer.name == payload.name,
        )
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="That server name is taken")
    try:
        secrets = pack_secrets(payload.secrets)
    except EncryptionNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    server = McpServer(
        workspace_id=actor.workspace_id,
        name=payload.name,
        transport=payload.transport,
        command=payload.command.strip(),
        args_json=json.dumps(payload.args),
        url=payload.url.strip(),
        secrets_encrypted=secrets,
        created_by=actor.user_id,
    )
    db.add(server)
    # The id is assigned on insert, and the audit row references it. Without
    # this flush record_audit stores a NULL resource_id and the commit fails
    # the NOT NULL constraint, so every create answered 500.
    db.flush()
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="mcp_server.created",
        resource_type="mcp_server",
        resource_id=server.id,
        detail={"name": server.name, "transport": server.transport},
    )
    db.commit()
    return _server_out(server, [])


@router.post("/servers/{server_id}/refresh", response_model=McpServerOut)
def refresh_server(
    server_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> McpServerOut:
    """Connect, re-enumerate tools, and record whether the server is reachable."""
    server = _load(db, actor, server_id)
    try:
        tools = refresh_server_tools(db, server)
    except (McpError, EncryptionNotConfiguredError) as exc:
        # An unreachable server is an expected state, not a 500 — return the row
        # with its error so the UI can show the reason inline.
        return _error_out(server, _tools_for(db, server.id), str(exc))
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="mcp_server.refreshed",
        resource_type="mcp_server",
        resource_id=server.id,
        detail={"tool_count": len(tools)},
    )
    db.commit()
    return _server_out(server, tools)


def _error_out(
    server: McpServer, tools: List[McpTool], message: str
) -> McpServerOut:
    out = _server_out(server, tools)
    return out.model_copy(update={"status": "error", "last_error": message[:1000]})


@router.patch("/servers/{server_id}", response_model=McpServerOut)
def update_server(
    server_id: str,
    enabled: bool,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> McpServerOut:
    server = _load(db, actor, server_id)
    server.enabled = enabled
    db.commit()
    return _server_out(server, _tools_for(db, server.id))


@router.patch("/tools/{tool_id}", response_model=McpToolOut)
def update_tool(
    tool_id: str,
    enabled: bool,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> McpToolOut:
    tool = db.scalar(
        select(McpTool).where(
            McpTool.id == tool_id, McpTool.workspace_id == actor.workspace_id
        )
    )
    if tool is None:
        raise HTTPException(status_code=404, detail="MCP tool not found")
    tool.enabled = enabled
    db.commit()
    return McpToolOut(
        id=tool.id,
        name=tool.name,
        description=tool.description,
        enabled=tool.enabled,
    )


@router.delete("/servers/{server_id}", status_code=204)
def delete_server(
    server_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    server = _load(db, actor, server_id)
    for tool in _tools_for(db, server.id):
        db.delete(tool)
    db.delete(server)
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="mcp_server.deleted",
        resource_type="mcp_server",
        resource_id=server_id,
        detail={"name": server.name},
    )
    db.commit()
