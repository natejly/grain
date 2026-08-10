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
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, TypeVar

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

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
        async with streamablehttp_client(
            config.url, headers=dict(config.headers) or None
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
