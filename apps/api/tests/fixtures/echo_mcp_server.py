"""A minimal stdio MCP server used by the MCP client tests.

Exposes three tools: one that echoes, one that always fails, and one that
returns more text than the client's cap so truncation can be asserted.
"""
from __future__ import annotations

import os
import sys

from mcp.server.fastmcp import FastMCP

server = FastMCP("echo-fixture")


@server.tool()
def echo(message: str) -> str:
    """Echo a message back."""
    return f"echo: {message}"


@server.tool()
def read_env() -> str:
    """Return a value passed through the server's environment."""
    return os.environ.get("FIXTURE_SECRET", "(unset)")


@server.tool()
def explode() -> str:
    """Always raise, so the client's error path can be exercised."""
    raise ValueError("intentional fixture failure")


@server.tool()
def firehose() -> str:
    """Return more text than the client will keep."""
    return "x" * 20000


if __name__ == "__main__":
    sys.exit(server.run())
