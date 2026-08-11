from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from ..config import Settings


class ToolSecurityError(ValueError):
    pass


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True for any address a public HTTPS tool must never reach.

    One definition, shared by the pre-connect DNS check and the post-connect peer
    check below, so the two can never come to disagree about what "internal"
    means — a private, loopback, link-local (cloud metadata lives at
    169.254.169.254), multicast, reserved, or unspecified address.
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_public_https_url(url: str, settings: Settings) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ToolSecurityError("Only HTTPS tool destinations are allowed")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host not in settings.allowed_tool_hosts:
        raise ToolSecurityError("Tool destination is not on the host allowlist")
    try:
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ToolSecurityError("Tool destination could not be resolved") from exc
    for address in addresses:
        if _ip_is_blocked(ipaddress.ip_address(address[4][0])):
            raise ToolSecurityError("Tool destination resolved to a blocked network")


def _peer_is_blocked(response: httpx.Response) -> bool:
    """Whether the connection was actually served from a blocked network.

    `validate_public_https_url` checks the addresses a host *resolves to*, but
    between that check and the socket connect httpx resolves the name again — so a
    host that answered a public address for the check can answer a private one for
    the connection (DNS rebinding). This reads the peer the socket genuinely used
    (httpx exposes it on the streaming response), so the caller can refuse the
    exchange before it reads a byte of the body. Pre-connect blocks a statically
    internal target; this catches the one that only turns internal on the second
    lookup.

    Unknown is *not* blocked: a transport with no network stream — a test double,
    or a backend that does not carry the address — leaves the pre-connect check as
    the sole defense rather than failing every request outright. A peer that is
    reported but unparseable is blocked, since a value we cannot read is one we
    cannot vouch for.
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
        return _ip_is_blocked(ipaddress.ip_address(str(info[0])))
    except ValueError:
        return True


def execute_read_only_get(
    url: str,
    settings: Settings,
    *,
    transport: Optional[httpx.BaseTransport] = None,
) -> tuple[int, str]:
    """Fetch a URL, following redirects, refusing anything not demonstrably public.

    `transport` is a seam for tests to drive the redirect and peer-address paths
    without a network; production passes nothing and httpx builds its default.
    """
    current = url
    with httpx.Client(
        timeout=10.0, follow_redirects=False, transport=transport
    ) as client:
        for _ in range(4):
            validate_public_https_url(current, settings)
            with client.stream(
                "GET",
                current,
                headers={"Accept": "application/json, text/plain;q=0.9"},
            ) as response:
                if _peer_is_blocked(response):
                    raise ToolSecurityError(
                        "Tool destination connected to a blocked network"
                    )
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ToolSecurityError("Tool redirect had no destination")
                    current = urljoin(current, location)
                    continue
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > settings.max_tool_response_bytes:
                        raise ToolSecurityError("Tool response exceeded the size limit")
                return response.status_code, body.decode("utf-8", errors="replace")
    raise ToolSecurityError("Tool request exceeded the redirect limit")


def parse_tool_prompt(prompt: str) -> Optional[str]:
    value = prompt.strip()
    if not value.lower().startswith("/tool "):
        return None
    name = value[6:].strip().split(" ", 1)[0]
    return name or None

