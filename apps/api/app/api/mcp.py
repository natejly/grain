from __future__ import annotations

import json
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..config import Settings, get_settings
from ..database import get_db
from ..models import McpOAuthClient, McpOAuthToken, McpServer, McpTool, OAuthState
from ..schemas import ApiModel, McpServerOut, McpServerRequest, McpToolOut
from ..services.audit import record_audit
from ..services.crypto import EncryptionNotConfiguredError
from ..services.mcp import (
    McpAuthRequired,
    McpError,
    McpOAuthError,
    pack_secrets,
    refresh_server_tools,
)
from ..services.mcp import oauth as mcp_oauth

router = APIRouter(prefix="/api/mcp", tags=["mcp"])

# Server names namespace the tools the model sees (mcp__<name>__<tool>), so they
# must not contain the separator or anything the model API rejects in a name.
NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,58}[a-z0-9])?$")


class McpAuthStatusOut(ApiModel):
    """Everything the Connect button needs and nothing a token could leak."""

    server_id: str
    required: bool
    connected: bool
    issuer: str
    scopes: str
    expires_in_seconds: Optional[int]


class McpConnectOut(BaseModel):
    authorize_url: str


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


def _validate(payload: McpServerRequest, settings: Settings) -> None:
    if not NAME_RE.match(payload.name):
        raise HTTPException(
            status_code=422,
            detail="Name must be lowercase letters, digits, and dashes",
        )
    if payload.transport == "stdio" and not settings.mcp_stdio_allowed:
        # A stdio server is an arbitrary command run as this API's own user;
        # registering one outside development is remote code execution on the
        # host. Refuse it here, and server_config refuses to spawn one too.
        raise HTTPException(
            status_code=403,
            detail=(
                "stdio MCP servers run commands on the API host and are only "
                "allowed in development. Register an http server instead."
            ),
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
    settings: Settings = Depends(get_settings),
) -> McpServerOut:
    _validate(payload, settings)
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
        # The user id is what makes a remote server use *this* caller's token
        # rather than whichever workspace member connected it first.
        tools = refresh_server_tools(db, server, user_id=actor.user_id)
    except McpAuthRequired as exc:
        # Not an error state: the row is fine and the user simply has to
        # consent, so the status the UI sees is the one that offers Connect.
        return _error_out(
            server, _tools_for(db, server.id), str(exc), status="needs_auth"
        )
    except (McpError, McpOAuthError, EncryptionNotConfiguredError) as exc:
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
    server: McpServer,
    tools: List[McpTool],
    message: str,
    status: str = "error",
) -> McpServerOut:
    out = _server_out(server, tools)
    return out.model_copy(update={"status": status, "last_error": message[:1000]})


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
    # Every user's tokens and this deployment's registration reference the
    # server row; leaving them behind would both violate the foreign key and
    # keep decryptable credentials for a server nobody can see any more.
    db.execute(delete(McpOAuthToken).where(McpOAuthToken.server_id == server.id))
    db.execute(delete(McpOAuthClient).where(McpOAuthClient.server_id == server.id))
    db.execute(delete(OAuthState).where(OAuthState.server_id == server.id))
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


# --------------------------------------------------------------------------
# OAuth 2.1, mirroring the connector routes in api/integrations.py.
#
# The one deliberate difference is the shape of the identity: a connector is
# connected once for a workspace, while an MCP server authorizes a *person*, so
# every route below is scoped by actor.user_id as well as workspace, and the
# callback restores the user from the state row rather than from a cookie.


@router.get("/servers/{server_id}/auth", response_model=McpAuthStatusOut)
def server_auth_status(
    server_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> McpAuthStatusOut:
    server = _load(db, actor, server_id)
    status = mcp_oauth.auth_status(db, server, actor.user_id)
    return McpAuthStatusOut(
        server_id=server.id,
        required=status.required,
        connected=status.connected,
        issuer=status.issuer,
        scopes=status.scopes,
        expires_in_seconds=status.expires_in_seconds,
    )


@router.post("/servers/{server_id}/connect", response_model=McpConnectOut)
def connect_server(
    server_id: str,
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> McpConnectOut:
    """Discover, register, and hand back the URL to send the browser to.

    This is the only route that dials the network on the user's behalf, so a
    failure here is a 400 with the discovery error verbatim — those messages are
    written to be legible and are guaranteed to carry no URL or credential.
    """
    server = _load(db, actor, server_id)
    try:
        authorize_url = mcp_oauth.begin_authorization(
            db, server, actor.user_id, settings
        )
    except (McpOAuthError, EncryptionNotConfiguredError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="mcp_server.auth_started",
        resource_type="mcp_server",
        resource_id=server.id,
        detail={"name": server.name},
    )
    db.commit()
    return McpConnectOut(authorize_url=authorize_url)


@router.get("/oauth/callback")
def oauth_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Unauthenticated by design: the state row is the credential.

    The browser arrives here as a top-level navigation from the authorization
    server, so there is no session cookie to rely on. What proves the callback
    is ours is the single-use state row, which also carries the user it belongs
    to — reading the identity from anything the query string says would let an
    attacker attach their account to somebody else's server.
    """
    web_origin = settings.allowed_web_origins[0] if settings.allowed_web_origins else ""
    if error:
        # Consume the state anyway so a denied consent cannot be replayed later.
        db.execute(
            delete(OAuthState).where(
                OAuthState.provider == mcp_oauth.STATE_PROVIDER,
                OAuthState.state == state,
            )
        )
        db.commit()
        return RedirectResponse(url=f"{web_origin}/?mcp_error=denied")
    try:
        token = mcp_oauth.complete_authorization(db, state, code, settings)
    except (McpOAuthError, EncryptionNotConfiguredError):
        # The reason is not put in the URL: it would be attacker-influenced text
        # rendered by the web app, and the user can retry from the MCP view.
        return RedirectResponse(url=f"{web_origin}/?mcp_error=failed")
    # Scoped to the workspace the token row belongs to; there is no actor here
    # to scope by, and a primary-key fetch would read a row this callback has no
    # business reading if the two ever disagreed.
    server = db.scalar(
        select(McpServer).where(
            McpServer.id == token.server_id,
            McpServer.workspace_id == token.workspace_id,
        )
    )
    record_audit(
        db,
        workspace_id=token.workspace_id,
        actor_id=token.user_id,
        action="mcp_server.connected",
        resource_type="mcp_server",
        resource_id=token.server_id,
        detail={"name": server.name if server is not None else ""},
    )
    if server is not None and server.status == "needs_auth":
        server.status = "unknown"
        server.last_error = ""
    db.commit()
    return RedirectResponse(url=f"{web_origin}/?mcp_connected={token.server_id}")


@router.post("/servers/{server_id}/disconnect", response_model=McpAuthStatusOut)
def disconnect_server(
    server_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> McpAuthStatusOut:
    """Forget this caller's token. Other members of the workspace keep theirs."""
    server = _load(db, actor, server_id)
    if mcp_oauth.disconnect(db, server, actor.user_id):
        record_audit(
            db,
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            action="mcp_server.disconnected",
            resource_type="mcp_server",
            resource_id=server.id,
            detail={"name": server.name},
        )
        db.commit()
    status = mcp_oauth.auth_status(db, server, actor.user_id)
    return McpAuthStatusOut(
        server_id=server.id,
        required=status.required,
        connected=status.connected,
        issuer=status.issuer,
        scopes=status.scopes,
        expires_in_seconds=status.expires_in_seconds,
    )
