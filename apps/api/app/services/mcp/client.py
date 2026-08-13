"""A thin, synchronous facade over the async MCP SDK.

The rest of this API is synchronous — SQLAlchemy sessions, tool executors, and
background tasks — so every MCP operation is run to completion on a dedicated
event loop in its own thread. That works whether the caller sits in a threadpool
worker (`process_run`) or inside the running event loop, which a bare
`asyncio.run()` would refuse to do.

Each operation opens its own connection and closes it again. A pooled session
would avoid respawning stdio servers per call, but it would have to outlive the
request on a background loop; correctness first, and the cost is a process spawn
on tools the user has already been asked to approve.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar
from urllib.parse import urlparse

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared._httpx_utils import create_mcp_http_client

from ...config import get_settings

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_RESULT_CHARS = 8000

T = TypeVar("T")

# Called with force_refresh; returns this user's bearer token or None when they
# have never connected. `services/mcp/oauth.token_provider` builds one. Kept as
# a bare callable so this layer stays free of sessions and rows.
TokenProvider = Callable[[bool], Optional[str]]


class McpError(RuntimeError):
    """Any failure talking to an MCP server, safe to show the user."""


class McpAuthRequired(McpError):
    """The server refused us and no token can be obtained without the user.

    Distinct from McpError because the remedy is a Connect button, not a retry:
    a server behind OAuth that the user has not authorized must not read as
    "unreachable", which is what it looked like before this existed.
    """


@dataclass(frozen=True)
class McpToolInfo:
    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ServerConfig:
    """Connection details for one server, already decrypted."""

    name: str
    transport: str
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)


def _run_sync(coro_factory: Callable[[], Awaitable[T]], timeout: float) -> T:
    """Run a coroutine to completion on a private loop in a private thread."""

    def runner() -> T:
        return asyncio.run(asyncio.wait_for(coro_factory(), timeout=timeout))

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp") as pool:
        # The outer timeout is a backstop: wait_for already bounds the awaited
        # work, but a server wedged in connection setup should not pin a thread.
        return pool.submit(runner).result(timeout=timeout + 10)


def _is_own_network(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for an address that is only ever this API host's own network.

    One definition, shared by the pre-connect DNS check and the post-connect peer
    check, so the two can never come to disagree about what "internal" means.
    RFC1918 is deliberately absent: an operator's internal MCP server in their own
    VPC is a real thing, so private ranges stay reachable. What is refused is the
    set that is never a tenant's server and always this host — loopback,
    link-local (cloud metadata lives at 169.254.169.254), multicast, unspecified.
    """
    return ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified


def _guard_http_target(url: str) -> None:
    """Refuse an http MCP endpoint on the API's own network before dialing it.

    The SDK's streamable transport connects to `config.url` on every tool call
    with no address check of its own, so without this a server registered at the
    API host's loopback or the cloud-metadata address (169.254.169.254) would be
    reached each time — a plain SSRF the OAuth discovery flow already guards but
    this transport did not.

    Refused only outside development: a `http://localhost` MCP server is a normal
    local workflow that `api/mcp.py` registers on purpose, and the tests run
    there, so gating on the environment keeps both working while a deployment —
    where "localhost" is only ever the API's own box — is protected. RFC1918 is
    left allowed, because an operator's internal MCP server in their own VPC is a
    real thing; what is blocked is the range that is never a tenant's server and
    always this host: loopback, link-local, multicast, unspecified.

    The fast half of the defense: it fails a statically internal target before a
    socket opens, with a legible message. The host that only turns internal on the
    second lookup — DNS rebinding between this check and the SDK's own dial — is
    caught by `_refuse_rebound_peer`, which reads back the address the connection
    genuinely used. Together they are the same pre/post pair the tool fetcher runs.
    """
    if get_settings().is_dev_env:
        return
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return
    try:
        infos = socket.getaddrinfo(host, parsed.port or None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return  # let the transport surface the real resolution failure
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _is_own_network(ip):
            raise McpError(
                f"“{host}” resolves to {ip}, an address on this server's own "
                "network; an MCP server may not point at the API host or the "
                "cloud metadata service."
            )


def _peer_is_blocked(response: httpx.Response) -> bool:
    """Whether the socket actually connected to this host's own network.

    `_guard_http_target` checks the addresses a host *resolves to*, but the SDK
    resolves the name again to dial, so a host that answered a public address for
    the check can answer 169.254.169.254 for the connection (DNS rebinding). This
    reads the peer the socket genuinely used — httpx exposes it on the response —
    so the exchange is refused before a byte of the body is read. It shares the
    pre-connect check's own-network set, RFC1918 included as reachable.

    Unknown is not blocked: a transport that carries no address (a test double)
    leaves the pre-connect check as the sole defense rather than failing every
    call outright. An address reported but unparseable IS blocked — a value that
    cannot be read is one that cannot be vouched for.
    """
    stream = response.extensions.get("network_stream")
    if stream is None:
        return False
    try:
        info = stream.get_extra_info("server_addr")
    except Exception:  # noqa: BLE001 - extra-info is best-effort, never fatal
        return False
    if not info:
        return False
    try:
        return _is_own_network(ipaddress.ip_address(str(info[0])))
    except ValueError:
        return True


async def _refuse_rebound_peer(response: httpx.Response) -> None:
    """httpx response hook: abort if the connection landed on our own network.

    Dev-gated for the same reason `_guard_http_target` is: a `http://localhost`
    MCP server is a normal local workflow there. Outside development it is the
    post-connect half of the rebinding defense — the case the pre-connect DNS
    check structurally cannot see.
    """
    if get_settings().is_dev_env:
        return
    if _peer_is_blocked(response):
        raise McpError(
            "This MCP server's connection landed on an address on this server's "
            "own network; refusing the exchange. An MCP server may not point at "
            "the API host or the cloud metadata service."
        )


def _pinned_client_factory(
    headers: Optional[Dict[str, str]] = None,
    timeout: Optional[httpx.Timeout] = None,
    auth: Optional[httpx.Auth] = None,
) -> httpx.AsyncClient:
    """The SDK's own httpx client, plus the peer-address hook.

    Built through `create_mcp_http_client` so it inherits whatever the SDK
    configures (redirect policy, headers, auth) rather than reconstructing it and
    drifting; the one addition is the response hook that reads back the connected
    peer. Every response — including each redirect hop — passes through it.
    """
    client = create_mcp_http_client(headers=headers, timeout=timeout, auth=auth)
    client.event_hooks["response"].append(_refuse_rebound_peer)
    return client


async def _with_session(
    config: ServerConfig,
    timeout: float,
    action: Callable[[ClientSession], Awaitable[T]],
) -> T:
    read_timeout = timedelta(seconds=timeout)
    if config.transport == "stdio":
        if not config.command:
            raise McpError("A stdio server needs a command")
        params = StdioServerParameters(
            command=config.command,
            args=list(config.args),
            env=dict(config.env) or None,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(
                read, write, read_timeout_seconds=read_timeout
            ) as session:
                await session.initialize()
                return await action(session)
    if config.transport == "http":
        if not config.url:
            raise McpError("An HTTP server needs a URL")
        _guard_http_target(config.url)
        async with streamablehttp_client(
            config.url,
            headers=dict(config.headers) or None,
            httpx_client_factory=_pinned_client_factory,
        ) as (read, write, _get_session_id):
            async with ClientSession(
                read, write, read_timeout_seconds=read_timeout
            ) as session:
                await session.initialize()
                return await action(session)
    raise McpError(f"Unsupported transport “{config.transport}”")


def _is_unauthorized(exc: BaseException, depth: int = 0) -> bool:
    """Did this failure come back as a 401?

    The SDK runs the HTTP transport inside an anyio task group, so a 401 does
    not arrive as a bare httpx.HTTPStatusError: it arrives wrapped in an
    ExceptionGroup, or re-raised with the original hanging off __cause__. The
    walk is bounded because a cycle through __context__ is possible and this
    runs on the failure path of every tool call.
    """
    if depth > 8:
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 401
    # Duck-typed rather than `isinstance(exc, BaseExceptionGroup)` so it also
    # matches the `exceptiongroup` backport anyio raises on older interpreters.
    members = getattr(exc, "exceptions", None)
    if isinstance(members, (tuple, list)):
        return any(
            isinstance(inner, BaseException) and _is_unauthorized(inner, depth + 1)
            for inner in members
        )
    for nested in (exc.__cause__, exc.__context__):
        if nested is not None and _is_unauthorized(nested, depth + 1):
            return True
    return False


def _find_mcp_error(exc: BaseException, depth: int = 0) -> Optional[McpError]:
    """Recover an McpError the SDK's task group wrapped.

    `_refuse_rebound_peer` raises inside an anyio task group, so its McpError
    reaches the caller inside an ExceptionGroup, or hanging off __cause__ /
    __context__, rather than bare — and the plain `except McpError` would miss it,
    surfacing the generic transport failure instead of the security message.
    Bounded like `_is_unauthorized`: a __context__ cycle is possible and this runs
    on the failure path of every call.
    """
    if depth > 8:
        return None
    if isinstance(exc, McpError):
        return exc
    members = getattr(exc, "exceptions", None)
    if isinstance(members, (tuple, list)):
        for inner in members:
            if isinstance(inner, BaseException):
                found = _find_mcp_error(inner, depth + 1)
                if found is not None:
                    return found
    for nested in (exc.__cause__, exc.__context__):
        if nested is not None:
            found = _find_mcp_error(nested, depth + 1)
            if found is not None:
                return found
    return None


def _authorized(config: ServerConfig, token: Optional[str]) -> ServerConfig:
    if not token:
        return config
    return replace(config, headers={**config.headers, "Authorization": f"Bearer {token}"})


def _run(
    config: ServerConfig,
    timeout: float,
    action: Callable[[ClientSession], Awaitable[T]],
    tokens: Optional[TokenProvider],
) -> T:
    """One attempt, then at most one more with a freshly acquired token.

    Exactly one retry: a server that answers 401 to a token the authorization
    server just minted is misconfigured, and looping would turn that into a
    stall on the interactive path.
    """
    token = tokens(False) if tokens is not None else None
    try:
        return _run_sync(
            lambda: _with_session(_authorized(config, token), timeout, action), timeout
        )
    except Exception as exc:
        if tokens is None or config.transport != "http" or not _is_unauthorized(exc):
            raise
        retry_token = tokens(True)
        if not retry_token:
            raise McpAuthRequired(
                "This server needs you to connect an account before it can be used"
            ) from exc
    return _run_sync(
        lambda: _with_session(_authorized(config, retry_token), timeout, action), timeout
    )


def list_tools(
    config: ServerConfig,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    tokens: Optional[TokenProvider] = None,
) -> List[McpToolInfo]:
    """Connect, initialize, and enumerate the server's tools."""

    async def action(session: ClientSession) -> List[McpToolInfo]:
        result = await session.list_tools()
        return [
            McpToolInfo(
                name=tool.name,
                description=tool.description or "",
                input_schema=_normalize_schema(tool.inputSchema),
            )
            for tool in result.tools
        ]

    try:
        return _run(config, timeout, action, tokens)
    except McpError:
        raise
    except Exception as exc:
        wrapped = _find_mcp_error(exc)
        if wrapped is not None:
            raise wrapped from exc
        raise McpError(_describe(exc)) from exc


def call_tool(
    config: ServerConfig,
    tool_name: str,
    arguments: Dict[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    tokens: Optional[TokenProvider] = None,
) -> str:
    """Invoke one tool and render its content blocks as bounded text."""

    async def action(session: ClientSession) -> str:
        result = await session.call_tool(tool_name, arguments)
        text = _render_content(result.content)
        if getattr(result, "isError", False):
            return "The MCP tool reported an error:\n" + text
        return text

    try:
        return _run(config, timeout, action, tokens)
    except McpError:
        raise
    except Exception as exc:
        wrapped = _find_mcp_error(exc)
        if wrapped is not None:
            raise wrapped from exc
        raise McpError(_describe(exc)) from exc


def _normalize_schema(schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Coerce a tool's input schema into the object shape the model API wants."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return {"type": "object", "properties": {}}
    return schema


def _render_content(blocks: Any) -> str:
    """Flatten MCP content blocks into text the model can read."""
    parts: List[str] = []
    for block in blocks or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            parts.append(getattr(block, "text", "") or "")
        elif kind == "resource":
            resource = getattr(block, "resource", None)
            text = getattr(resource, "text", None)
            uri = getattr(resource, "uri", "")
            parts.append(text if text else f"[resource {uri}]")
        elif kind == "image":
            # The model API is not given image bytes here; name it so the model
            # knows something came back rather than seeing an empty result.
            parts.append(f"[image {getattr(block, 'mimeType', 'image')} omitted]")
        else:
            parts.append(_stringify(block))
    rendered = "\n".join(part for part in parts if part).strip()
    if not rendered:
        return "(the tool returned no content)"
    if len(rendered) > MAX_RESULT_CHARS:
        return rendered[:MAX_RESULT_CHARS] + "\n…(truncated)"
    return rendered


def _stringify(block: Any) -> str:
    dump = getattr(block, "model_dump", None)
    if callable(dump):
        try:
            return json.dumps(dump(exclude_none=True), default=str)
        except (TypeError, ValueError):
            return str(block)
    return str(block)


def _describe(exc: Exception) -> str:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "The MCP server timed out"
    message = str(exc).strip() or exc.__class__.__name__
    return message[:400]
