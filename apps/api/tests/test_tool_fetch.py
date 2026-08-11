"""`execute_read_only_get`: redirects re-checked, and the peer pinned against rebinding.

The SSRF defense has two halves and this file is the second one. `validate_public_https_url`
checks the addresses a host *resolves to*; but httpx resolves again to connect, so a name
that answered a public address for the check can answer a private one for the socket (DNS
rebinding). `_peer_is_blocked` reads the address the connection genuinely used and refuses
it before a byte of the body is read.

These are pure network helpers — no database, no app fixtures — so the file drives them
directly with a duck-typed settings object and a scripted transport. Run in isolation with
`--noconftest`; it needs none of the suite's DB scaffolding.
"""
from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import List, Optional, Tuple

import httpx
import pytest

from app.services.tools import ToolSecurityError, execute_read_only_get

ALLOWLIST_HOST = "api.github.com"
PUBLIC_IP = "140.82.112.3"
PRIVATE_IP = "10.0.0.7"
METADATA_IP = "169.254.169.254"  # cloud metadata — link-local, must never be reached


def _settings(max_bytes: int = 256 * 1024) -> SimpleNamespace:
    """Only the two attributes the fetcher reads, so no real Settings is built."""
    return SimpleNamespace(
        allowed_tool_hosts={ALLOWLIST_HOST},
        max_tool_response_bytes=max_bytes,
    )


def _public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every pre-connect resolution answers a public address, so the peer check is
    what any private connection has to be caught by."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443))
        ],
    )


class _FakeStream:
    """The one method `_peer_is_blocked` calls on httpx's network-stream extension."""

    def __init__(self, peer: Optional[Tuple[str, int]]) -> None:
        self._peer = peer

    def get_extra_info(self, name: str) -> Optional[Tuple[str, int]]:
        return self._peer if name == "server_addr" else None


class _Hop:
    def __init__(
        self,
        status: int = 200,
        *,
        headers: Optional[dict] = None,
        body: bytes = b"",
        peer: Optional[Tuple[str, int]] = (PUBLIC_IP, 443),
        stream: bool = True,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.body = body
        self.peer = peer
        self.stream = stream


class _ScriptedTransport(httpx.BaseTransport):
    """Answer each request from a queue of hops, each with a chosen peer address."""

    def __init__(self, hops: List[_Hop]) -> None:
        self._hops = list(hops)
        self.handled = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.handled += 1
        hop = self._hops.pop(0)
        extensions = {"network_stream": _FakeStream(hop.peer)} if hop.stream else {}
        return httpx.Response(
            hop.status, headers=hop.headers, content=hop.body, extensions=extensions
        )


def _fetch(transport: _ScriptedTransport, settings=None) -> Tuple[int, str]:
    return execute_read_only_get(
        f"https://{ALLOWLIST_HOST}/thing", settings or _settings(), transport=transport
    )


def test_a_public_peer_returns_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch)
    status, text = _fetch(_ScriptedTransport([_Hop(200, body=b"hello")]))
    assert (status, text) == (200, "hello")


def test_a_rebound_private_peer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: the name passed the DNS check, but the socket landed inside."""
    _public_dns(monkeypatch)
    transport = _ScriptedTransport([_Hop(200, body=b"secret", peer=(PRIVATE_IP, 443))])
    with pytest.raises(ToolSecurityError, match="blocked network"):
        _fetch(transport)


def test_a_peer_on_the_metadata_range_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch)
    transport = _ScriptedTransport([_Hop(200, body=b"creds", peer=(METADATA_IP, 80))])
    with pytest.raises(ToolSecurityError, match="blocked network"):
        _fetch(transport)


def test_an_unparseable_peer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch)
    transport = _ScriptedTransport([_Hop(200, body=b"x", peer=("not-an-ip", 443))])
    with pytest.raises(ToolSecurityError, match="blocked network"):
        _fetch(transport)


def test_a_transport_without_a_peer_address_falls_back_to_the_dns_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown is not blocked: the pre-connect check stands alone rather than the
    request failing outright on a backend that does not expose the address."""
    _public_dns(monkeypatch)
    status, text = _fetch(_ScriptedTransport([_Hop(200, body=b"ok", stream=False)]))
    assert (status, text) == (200, "ok")


def test_a_redirect_off_the_allowlist_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each hop is re-validated, so a redirect cannot walk off the allowlist."""
    _public_dns(monkeypatch)
    transport = _ScriptedTransport(
        [_Hop(302, headers={"location": "https://evil.example/"})]
    )
    with pytest.raises(ToolSecurityError, match="allowlist"):
        _fetch(transport)
    assert transport.handled == 1  # never dialed the second host


def test_the_redirect_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch)
    loop = {"location": f"https://{ALLOWLIST_HOST}/next"}
    transport = _ScriptedTransport([_Hop(302, headers=loop) for _ in range(4)])
    with pytest.raises(ToolSecurityError, match="redirect limit"):
        _fetch(transport)


def test_a_body_over_the_ceiling_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _public_dns(monkeypatch)
    transport = _ScriptedTransport([_Hop(200, body=b"x" * 100)])
    with pytest.raises(ToolSecurityError, match="size limit"):
        _fetch(transport, settings=_settings(max_bytes=10))
