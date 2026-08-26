"""Grain as an MCP *server*: the workspace's read-only tools, over JSON-RPC.

`services/mcp/` is the client half — Grain calling other people's servers.
This is the inverse: an external agent (Claude Code, Codex, an Agents-SDK
stack) points its MCP client at ``POST /api/mcp`` with a workspace bearer
token and gets the workspace's research surface as ordinary MCP tools. A
distribution channel, not only a feature: whatever harness a customer already
runs can now search this workspace's sources, quote its conversations, walk
its graph.

Deliberately hand-rolled and stateless rather than mounted from an MCP SDK
server: the protocol's core is four small methods, the streamable-HTTP
transport allows plain request/response JSON for them, and a second ASGI app
owning its own lifecycle inside this one would cost far more than ~150 lines.

The safety posture is the registry's own, applied twice:

- **Read-only tools only, and absent rather than denied.** Write-capable
  specs, `force_ask` specs, `ask_user` (no human on this channel to answer)
  and `delegate` (needs a live run to observe) are not in the offered list at
  all — the same "a tool a caller must not run is absent" rule the subject
  narrowing follows.
- **Policy still applies, at `workflow` scope.** An external caller is the
  definition of unattended, so a standing chat-scope allow does not authorise
  it, an org ceiling binds it, and anything that resolves to ask or deny is
  refused with a readable error — there is no approval card to park on here.

Tokens are workspace-wide, not scoped per tool: any minted token reaches every
offered tool, including one-call-bulk reads like ``graph_export``. The lever
for narrowing a workspace's exposure is a workflow-scope ``ToolPolicy`` deny
row for the tool in question — that is what the policy check above enforces.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import api_tokens as token_service
from ..services.agent_loop import WORKFLOW_SCOPE, resolve_policy
from ..services.audit import record_audit
from ..services.delegation import DELEGATE_TOOL
from ..services.llm_tools import ASK_USER, ToolContext, ToolSpec, build_registry
from .ratelimit import public_rate_limit

router = APIRouter(prefix="/api", tags=["mcp-server"])

PROTOCOL_VERSION = "2025-06-18"

#: JSON-RPC error codes, per spec.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def _bearer_identity(
    authorization: Optional[str], db: Session
) -> token_service.ResolvedToken:
    """The workspace identity behind the Authorization header, or a 401.

    Bearer-only on purpose: no cookie fallback means a browser can never be
    confused into calling this route with ambient credentials, which is also
    why it needs no CSRF defense.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="A bearer token is required")
    resolved = token_service.resolve(db, authorization.removeprefix("Bearer ").strip())
    if resolved is None:
        raise HTTPException(status_code=401, detail="The bearer token is not valid")
    return resolved


def _offered_registry(
    db: Session, identity: token_service.ResolvedToken
) -> Dict[str, ToolSpec]:
    context = ToolContext(
        workspace_id=identity.workspace_id,
        user_id=identity.user_id,
        conversation_id="",
    )
    registry = build_registry(db, context)
    return {
        name: spec
        for name, spec in registry.items()
        if spec.read_only
        and not spec.force_ask
        and name not in (DELEGATE_TOOL, ASK_USER)
    }


def _rpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


def _rpc_result(request_id: Any, result: Dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _call_tool(
    db: Session,
    identity: token_service.ResolvedToken,
    registry: Dict[str, ToolSpec],
    params: Dict[str, Any],
) -> Dict[str, Any]:
    name = str(params.get("name") or "")
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    spec = registry.get(name)
    if spec is None:
        return {
            "content": [{"type": "text", "text": f"Unknown tool “{name}”."}],
            "isError": True,
        }
    # The same single decision point every turn consults, at the unattended
    # scope. `ask` has no one here to ask, so it refuses like a deny — with
    # words that tell the operator which lever (a workflow-scope allow) exists.
    verdict = resolve_policy(
        db,
        workspace_id=identity.workspace_id,
        user_id=identity.user_id,
        spec=spec,
        scope=WORKFLOW_SCOPE,
    )
    if verdict != "allow":
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"The workspace's policy does not allow “{name}” for "
                        "unattended callers. An owner can grant it at workflow "
                        "scope under Rules."
                    ),
                }
            ],
            "isError": True,
        }
    try:
        result = spec.executor(db, _context_for(identity), arguments)
        text = result.bounded_content() or "(empty result)"
        if result.evidence:
            quoted = "\n".join(
                f"- {item.filename}, passage {item.ordinal + 1}: {item.excerpt[:300]}"
                for item in result.evidence[:8]
            )
            text = f"{text}\n\nPassages:\n{quoted}" if text != "(empty result)" else quoted
        is_error = False
    except Exception as exc:  # Tool bugs become tool errors, never 500s.
        db.rollback()
        text = f"Tool failed: {str(exc)[:300]}"
        is_error = True
    record_audit(
        db,
        workspace_id=identity.workspace_id,
        actor_id=identity.user_id,
        action="mcp_server.tool_called",
        resource_type="api_token",
        resource_id=identity.token_id,
        detail={"tool": name, "is_error": is_error},
    )
    db.commit()
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _context_for(identity: token_service.ResolvedToken) -> ToolContext:
    return ToolContext(
        workspace_id=identity.workspace_id,
        user_id=identity.user_id,
        conversation_id="",
    )


@router.post("/mcp", dependencies=[Depends(public_rate_limit("mcp"))])
async def mcp_endpoint(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> Response:
    """One stateless JSON-RPC exchange of the MCP streamable-HTTP transport.

    Notifications answer 202 with no body (the transport's contract for
    messages that expect no reply); everything else answers a single JSON-RPC
    response. Sessions, resumable streams and server-initiated messages are
    deliberately unimplemented — a tools-only server needs none of them, and
    every mainstream client falls back to plain request/response cleanly.
    """
    identity = _bearer_identity(authorization, db)
    try:
        # `parse_constant` rejects the non-standard NaN/Infinity tokens Python's
        # json accepts by default: an `id` of NaN would parse fine here and then
        # crash JSONResponse's `json.dumps(allow_nan=False)` at render time,
        # OUTSIDE this try — a crafted-body 500 the isolation harness forbids.
        message = json.loads(
            await request.body(),
            parse_constant=lambda _token: (_ for _ in ()).throw(ValueError()),
        )
    except ValueError:
        return _rpc_error(None, PARSE_ERROR, "The request body is not JSON")
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _rpc_error(None, INVALID_REQUEST, "Expected a JSON-RPC 2.0 message")
    request_id = message.get("id")
    # A JSON-RPC id is a string, a number, or null — never a container. A
    # non-scalar id cannot round-trip into the response envelope, so it is an
    # invalid request rather than something to echo back.
    if request_id is not None and not isinstance(request_id, (str, int, float)):
        return _rpc_error(None, INVALID_REQUEST, "The JSON-RPC id must be a scalar")
    method = str(message.get("method") or "")
    raw_params = message.get("params")
    params: Dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

    if method.startswith("notifications/"):
        return Response(status_code=202)
    if method == "initialize":
        return _rpc_result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "grain", "version": "1.0"},
                "instructions": (
                    "This server exposes one Grain workspace's read-only "
                    "research tools: search its sources and conversations, "
                    "query its datasets, walk its knowledge graph. Nothing "
                    "here can change the workspace."
                ),
            },
        )
    if method == "ping":
        return _rpc_result(request_id, {})
    if method == "tools/list":
        registry = _offered_registry(db, identity)
        return _rpc_result(
            request_id,
            {
                "tools": [
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "inputSchema": spec.parameters,
                    }
                    for spec in registry.values()
                ]
            },
        )
    if method == "tools/call":
        registry = _offered_registry(db, identity)
        return _rpc_result(request_id, _call_tool(db, identity, registry, params))
    return _rpc_error(request_id, METHOD_NOT_FOUND, f"Unknown method “{method}”")
