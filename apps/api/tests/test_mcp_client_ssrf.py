"""The http MCP transport refuses the API's own host — before AND after dialing.

`services/mcp/client.py` connects to a registered server URL on every tool call
through an SDK transport that does no address check of its own. Left open, a
server registered at the API's loopback or the cloud-metadata address would be
reached each time. Two halves close it, in a deployment only (a `http://localhost`
server is a normal dev workflow and stays allowed):

- `_guard_http_target` — the pre-connect DNS check, refusing a statically
  internal target before a socket opens.
- `_peer_is_blocked` / `_refuse_rebound_peer` — the post-connect peer check,
  reading back the address the socket genuinely used, so a host that resolves
  public for the pre-check and private on the real dial (DNS rebinding) is still
  caught. This is the residual the pre-connect check structurally cannot see.

Both are pure functions over the URL / the httpx response, so this drives them
directly — no database, no app fixtures. Run with `--noconftest`.
"""
from __future__ import annotations

import asyncio
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


# -- the post-connect peer pin: the rebinding case the DNS check cannot see --


def _response_from(server_addr: object, *, has_stream: bool = True) -> object:
    """A stand-in httpx response carrying (or not) the peer the socket used."""
    if not has_stream:
        return SimpleNamespace(extensions={})
    stream = SimpleNamespace(
        get_extra_info=lambda key: server_addr if key == "server_addr" else None
    )
    return SimpleNamespace(extensions={"network_stream": stream})


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "::1", "0.0.0.0"])
def test_a_rebound_peer_on_our_network_is_blocked(ip: str) -> None:
    # The address the socket ACTUALLY reached, whatever the pre-check saw.
    assert mcp_client._peer_is_blocked(_response_from((ip, 443))) is True


@pytest.mark.parametrize("ip", ["10.1.2.3", "192.168.0.9", "93.184.216.34"])
def test_a_private_or_public_peer_is_allowed(ip: str) -> None:
    # RFC1918 stays reachable (the operator's own VPC), same as the pre-check.
    assert mcp_client._peer_is_blocked(_response_from((ip, 443))) is False


def test_a_peer_with_no_network_stream_is_not_blocked() -> None:
    # A transport that carries no address leaves the pre-connect check as the sole
    # defense rather than failing every call — unknown is not blocked.
    assert mcp_client._peer_is_blocked(_response_from(None, has_stream=False)) is False


def test_an_unparseable_peer_is_blocked() -> None:
    # A value that cannot be read is one that cannot be vouched for.
    assert mcp_client._peer_is_blocked(_response_from(("not-an-ip", 443))) is True


def test_the_hook_raises_in_a_deployment_on_a_rebound_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, dev=False)
    with pytest.raises(McpError, match="own network"):
        asyncio.run(mcp_client._refuse_rebound_peer(_response_from(("169.254.169.254", 80))))


def test_the_hook_allows_a_public_peer_in_a_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, dev=False)
    asyncio.run(mcp_client._refuse_rebound_peer(_response_from(("93.184.216.34", 443))))


def test_the_hook_is_inert_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    # A localhost MCP server is a normal dev workflow; the peer pin must not fire.
    _env(monkeypatch, dev=True)
    asyncio.run(mcp_client._refuse_rebound_peer(_response_from(("127.0.0.1", 9000))))


def test_the_factory_wires_the_hook_onto_the_client() -> None:
    async def _build_and_check() -> None:
        pinned = mcp_client._pinned_client_factory()
        try:
            assert mcp_client._refuse_rebound_peer in pinned.event_hooks["response"]
        finally:
            await pinned.aclose()

    asyncio.run(_build_and_check())


class _Group(Exception):
    """A stand-in for the group anyio raises — the walker duck-types `.exceptions`
    (like `_is_unauthorized`), so this exercises the same branch a real
    ExceptionGroup would, without naming the 3.11 builtin ruff targets below."""

    def __init__(self, *members: BaseException) -> None:
        super().__init__("transport failed")
        self.exceptions = list(members)


def test_a_wrapped_peer_error_is_recovered() -> None:
    # The hook raises inside the SDK's anyio task group, so its McpError arrives
    # inside an ExceptionGroup or off __cause__ — the caller must still surface it.
    boom = McpError("landed on this server's own network")
    grouped = _Group(RuntimeError("noise"), boom)
    assert mcp_client._find_mcp_error(grouped) is boom

    chained = RuntimeError("outer")
    chained.__cause__ = boom
    assert mcp_client._find_mcp_error(chained) is boom

    assert mcp_client._find_mcp_error(RuntimeError("unrelated")) is None
