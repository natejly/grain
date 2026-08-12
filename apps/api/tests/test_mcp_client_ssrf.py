"""The http MCP transport refuses the API's own host before dialing it.

`services/mcp/client.py` connects to a registered server URL on every tool call
through an SDK transport that does no address check of its own. Left open, a
server registered at the API's loopback or the cloud-metadata address would be
reached each time. `_guard_http_target` closes that — in a deployment only; a
`http://localhost` server is a normal dev workflow and stays allowed there.

`_guard_http_target` is a pure function over the URL (reading settings and DNS),
so this drives it directly — no database, no app fixtures. Run with
`--noconftest`.
"""
from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from app.services.mcp import client as mcp_client
from app.services.mcp.client import McpError


def _env(monkeypatch: pytest.MonkeyPatch, *, dev: bool) -> None:
    monkeypatch.setattr(mcp_client, "get_settings", lambda: SimpleNamespace(is_dev_env=dev))


def _resolves_to(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))],
    )


# -- in a deployment, the API's own host and the metadata service are refused --


def test_a_deployment_refuses_a_loopback_server(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, dev=False)
    _resolves_to(monkeypatch, "127.0.0.1")
    with pytest.raises(McpError, match="own network"):
        mcp_client._guard_http_target("https://tools.local/mcp")


def test_a_deployment_refuses_the_metadata_address(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, dev=False)
    _resolves_to(monkeypatch, "169.254.169.254")
    with pytest.raises(McpError, match="own network"):
        mcp_client._guard_http_target("https://metadata/mcp")


def test_a_deployment_allows_a_private_vpc_server(monkeypatch: pytest.MonkeyPatch) -> None:
    # RFC1918 is the operator's own network — an internal MCP server is plausible.
    _env(monkeypatch, dev=False)
    _resolves_to(monkeypatch, "10.1.2.3")
    mcp_client._guard_http_target("https://mcp.internal/mcp")


def test_a_deployment_allows_a_public_server(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, dev=False)
    _resolves_to(monkeypatch, "93.184.216.34")
    mcp_client._guard_http_target("https://mcp.example.com/mcp")


def test_an_unresolvable_host_is_left_for_the_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, dev=False)

    def _fail(*args, **kwargs):
        raise socket.gaierror("name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    mcp_client._guard_http_target("https://nope.invalid/mcp")


# -- in development, a localhost server stays allowed --


def test_development_allows_a_loopback_server(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, dev=True)
    # getaddrinfo must never even be consulted in dev; make it fail loudly if it is.
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("resolved in dev")),
    )
    mcp_client._guard_http_target("http://localhost:9000/mcp")
