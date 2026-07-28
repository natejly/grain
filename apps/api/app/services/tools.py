from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from ..config import Settings


class ToolSecurityError(ValueError):
    pass


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
        ip = ipaddress.ip_address(address[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ToolSecurityError("Tool destination resolved to a blocked network")


def execute_read_only_get(url: str, settings: Settings) -> tuple[int, str]:
    current = url
    with httpx.Client(timeout=10.0, follow_redirects=False) as client:
        for _ in range(4):
            validate_public_https_url(current, settings)
            with client.stream(
                "GET",
                current,
                headers={"Accept": "application/json, text/plain;q=0.9"},
            ) as response:
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

