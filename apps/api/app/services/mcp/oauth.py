"""OAuth 2.1 for remote MCP servers, discovered rather than configured.

Roughly half the public MCP registry is remote and most of it authenticates, so
"paste a bearer token in the headers box" is not a product. The MCP
authorization spec answers that with a chain of RFCs the client walks entirely
on its own: a 401 names the protected-resource metadata (RFC 9728), that names
an authorization server, its metadata (RFC 8414) names the endpoints, and
dynamic client registration (RFC 7591) mints credentials on the spot. Nothing
here is ever put in a settings file, because the whole point is that a user can
add a server this deployment has never heard of and have it work.

Three things in here are security, not plumbing, and they are the reason the
module is shaped the way it is.

**Every URL is attacker-controlled.** The MCP server URL is user-supplied, and
each discovery step lets *that* server hand us the next URL to fetch — issuer,
token endpoint, registration endpoint. That is a server-side request forgery
primitive aimed at the VPC and at 169.254.169.254, and it does not stop after
the first hop, so `_validate_destination` runs on every hop rather than once at
the front door. It delegates to `services.tools.validate_public_https_url`; see
that function's call site below for exactly what is relaxed and why.

**PKCE is the only thing standing between a leaked code and a session.** These
are public clients — dynamic registration usually yields no client secret — so
the authorization code is bearer-grade until it is redeemed. The verifier is
generated here, stored encrypted, and never sent to the browser; S256 only,
because `plain` reduces PKCE to a decorative parameter.

**A token is bound to one resource and one human.** `resource` (RFC 8707) is
sent on both legs and recorded, and a token whose recorded resource does not
match the server being called is refused — that check is what stops a token
minted for a server the user trusts from being replayed against one they do
not. Rows are keyed per (server, user) because an MCP server authorizes the
person, so sharing a workspace must never share a Linear account.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse, urlunparse

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...clock import utcnow
from ...config import Settings
from ...models import McpOAuthClient, McpOAuthToken, McpServer, OAuthState
from ..crypto import EncryptionNotConfiguredError, decrypt_secret, encrypt_secret
from ..tools import ToolSecurityError, validate_public_https_url

# Tests set this to an httpx.MockTransport so the whole flow runs offline.
HTTP_TRANSPORT: Optional[httpx.BaseTransport] = None

# `provider` on OAuthState is a 24-char slug shared with the connector flows;
# MCP reuses that table (see the model docstring) and needs its own value so a
# connector callback can never consume an MCP state row or vice versa.
STATE_PROVIDER = "mcp"
STATE_TTL = timedelta(minutes=10)

CALLBACK_PATH = "/api/mcp/oauth/callback"

# RFC 9728 §3.1 and RFC 8414 §3.1. Both specs insert the resource/issuer path
# *after* the well-known segment rather than appending, which is the detail
# servers hosting several tenants under one origin depend on.
PROTECTED_RESOURCE_WELL_KNOWN = "/.well-known/oauth-protected-resource"
AUTH_SERVER_WELL_KNOWN = "/.well-known/oauth-authorization-server"
OPENID_WELL_KNOWN = "/.well-known/openid-configuration"

# A metadata document is small; a server that streams us megabytes of JSON is
# either broken or trying to exhaust the process, and either way we stop.
MAX_METADATA_BYTES = 256 * 1024
DISCOVERY_TIMEOUT = 10.0
TOKEN_TIMEOUT = 15.0

# Enough entropy that guessing is hopeless, short enough for the 64-char column.
STATE_BYTES = 32

_RESOURCE_METADATA_RE = re.compile(
    r'resource_metadata\s*=\s*(?:"([^"]+)"|([^\s,]+))', re.IGNORECASE
)


class McpOAuthError(RuntimeError):
    """An authorization step failed, phrased for a human.

    Every message that reaches this class is shown in the UI and handed to the
    model, so none of them may carry a provider stack trace, a URL we were
    tricked into fetching, a code, or a token.
    """


@dataclass(frozen=True)
class AuthChallenge:
    """What a 401 from an MCP server told us, if anything."""

    resource_metadata_url: str = ""


@dataclass(frozen=True)
class ProtectedResource:
    """RFC 9728 metadata: which authorization server guards this resource."""

    resource: str
    authorization_servers: Tuple[str, ...]
    scopes: Tuple[str, ...]


@dataclass(frozen=True)
class AuthServer:
    """RFC 8414 metadata, already reduced to the endpoints this flow uses."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str
    scopes: Tuple[str, ...]
    # RFC 8414 §2 `code_challenge_methods_supported`. Empty means the server did
    # not publish it, which most do not and which the MCP default-endpoint
    # fallback cannot publish at all; a server that *does* publish it without
    # S256 is refused rather than silently downgraded.
    code_challenge_methods: Tuple[str, ...] = ()


@dataclass(frozen=True)
class AuthStatus:
    """What the UI needs to render a Connect button and nothing more."""

    required: bool
    connected: bool
    issuer: str = ""
    scopes: str = ""
    expires_in_seconds: Optional[int] = None


def http_client(timeout: float = DISCOVERY_TIMEOUT) -> httpx.Client:
    """Redirects are deliberately not followed.

    A 302 is a second attacker-chosen URL, and following it inside httpx would
    skip `_validate_destination` — the SSRF guard would then protect only the
    first hop of a chain the server controls.
    """
    return httpx.Client(timeout=timeout, transport=HTTP_TRANSPORT, follow_redirects=False)


def callback_url(settings: Settings) -> str:
    return settings.oauth_redirect_base.rstrip("/") + CALLBACK_PATH


def canonical_resource(url: str) -> str:
    """The RFC 8707 resource identifier for an MCP server URL.

    Lowercased scheme and host, no query, no fragment, no trailing slash. Two
    spellings of the same endpoint must produce the same string or the
    replay check below would reject a token it had just minted.
    """
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if not host:
        raise McpOAuthError("That server URL is not a valid address")
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme.lower(), netloc, path, "", "", ""))


def _validate_destination(url: str, settings: Settings) -> None:
    """SSRF guard for one hop of discovery.

    Reuses `validate_public_https_url` rather than growing a second copy of the
    DNS-resolution and IP-range rejection, which would drift. One check is
    relaxed and only one: the `TOOL_HOST_ALLOWLIST` gate cannot apply, because
    connecting to an MCP server this deployment has never heard of is the
    entire feature and a fixed allowlist would refuse all of them. It is
    relaxed by widening the allowlist to exactly the host of the URL already in
    hand, which leaves HTTPS-only, DNS resolution, and the rejection of
    private, loopback, link-local, multicast, reserved and unspecified
    addresses all in force — 169.254.169.254 and 10.0.0.0/8 are still refused.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise McpOAuthError("An authorization endpoint was not a usable URL")
    permissive = settings.model_copy(update={"tool_host_allowlist": host})
    try:
        validate_public_https_url(url, permissive)
    except ToolSecurityError as exc:
        # `exc` names the reason but never the URL, so it is safe to surface.
        raise McpOAuthError(f"Refused to contact that address: {exc}") from exc


def _read_json(response: httpx.Response) -> Dict[str, Any]:
    """Parse a metadata document, giving up on one that will not stop.

    The cap is applied *while* reading. Slicing `response.content` instead is
    not a limit at all — httpx has already pulled the entire body into memory by
    the time there is anything to measure, so the check would fire after the
    damage it exists to prevent. Every caller therefore hands this a streamed
    response, and the loop below is what makes MAX_METADATA_BYTES real.
    """
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > MAX_METADATA_BYTES:
            raise McpOAuthError("An authorization server returned an oversized document")
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise McpOAuthError("An authorization server returned a malformed reply") from exc
    if not isinstance(parsed, dict):
        raise McpOAuthError("An authorization server returned an unexpected reply")
    return parsed


def _get_metadata(
    client: httpx.Client, url: str, settings: Settings
) -> Optional[Dict[str, Any]]:
    """Fetch one metadata document; None when the server does not publish it.

    A 404 here is ordinary — most of discovery is trying documented locations
    in order — so only a transport failure or an unparseable 200 is an error.
    """
    _validate_destination(url, settings)
    try:
        with client.stream("GET", url, headers={"Accept": "application/json"}) as response:
            if response.status_code != 200:
                return None
            return _read_json(response)
    except httpx.HTTPError as exc:
        raise McpOAuthError("Could not reach that authorization server") from exc


def _string_list(value: Any) -> Tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item for item in value.split() if item)
    if isinstance(value, list):
        return tuple(str(item) for item in value if isinstance(item, str) and item)
    return ()


def _well_known_candidates(base: str, suffix: str) -> List[str]:
    """Both spellings of a well-known URL, path-inserted first.

    RFC 8414 says the path component of the issuer goes after the well-known
    segment; plenty of real servers only serve the naive appended form, and a
    handful serve neither and only answer at the origin. Trying all three is
    what "fall back sanely" means here.
    """
    parsed = urlparse(base)
    origin = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    path = parsed.path.rstrip("/")
    candidates = [f"{origin}{suffix}{path}" if path else f"{origin}{suffix}"]
    if path:
        candidates.append(f"{origin}{path}{suffix}")
        candidates.append(f"{origin}{suffix}")
    return candidates


def probe_authentication(url: str, settings: Settings) -> Optional[AuthChallenge]:
    """Ask the MCP endpoint whether it wants OAuth, and what it points at.

    Sent as a bare `initialize` because a GET on a streamable-HTTP endpoint is
    the SSE channel and several servers answer it with 405 regardless of
    authentication, which tells us nothing.
    """
    _validate_destination(url, settings)
    payload = {
        "jsonrpc": "2.0",
        "id": "auth-probe",
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "fieldnote", "version": "1.0"},
        },
    }
    try:
        with http_client() as client:
            response = client.post(
                url,
                json=payload,
                headers={"Accept": "application/json, text/event-stream"},
            )
    except httpx.HTTPError as exc:
        raise McpOAuthError("Could not reach that MCP server") from exc
    if response.status_code != 401:
        return None
    return AuthChallenge(
        resource_metadata_url=parse_challenge(response.headers.get("www-authenticate", ""))
    )


def parse_challenge(header: str) -> str:
    """Pull `resource_metadata` out of a WWW-Authenticate header.

    Returned empty rather than raising when the header is missing the hint:
    RFC 9728 makes it a SHOULD, and the well-known fallback below covers the
    servers that omit it.
    """
    match = _RESOURCE_METADATA_RE.search(header or "")
    if match is None:
        return ""
    return (match.group(1) or match.group(2) or "").strip()


def fetch_protected_resource(
    server_url: str, challenge: AuthChallenge, settings: Settings
) -> ProtectedResource:
    resource = canonical_resource(server_url)
    candidates: List[str] = []
    if challenge.resource_metadata_url:
        # Only honoured when it stays on the resource's own origin. A 401 that
        # points at somebody else's metadata document is how an attacker would
        # nominate an authorization server they control, and we would then send
        # the user there to consent.
        if _same_origin(challenge.resource_metadata_url, server_url):
            candidates.append(challenge.resource_metadata_url)
        else:
            raise McpOAuthError(
                "That server pointed its authorization metadata at another host"
            )
    candidates.extend(_well_known_candidates(server_url, PROTECTED_RESOURCE_WELL_KNOWN))

    with http_client() as client:
        for candidate in candidates:
            document = _get_metadata(client, candidate, settings)
            if document is None:
                continue
            servers = _string_list(document.get("authorization_servers"))
            if not servers:
                continue
            return ProtectedResource(
                resource=str(document.get("resource") or resource),
                authorization_servers=servers,
                scopes=_string_list(document.get("scopes_supported")),
            )
    # Nothing published. The MCP spec's fallback is that the resource server is
    # also its own authorization server, which is what a single-origin server
    # like a self-hosted deployment looks like.
    return ProtectedResource(
        resource=resource,
        authorization_servers=(_origin(server_url),),
        scopes=(),
    )


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), "", "", "", ""))


def _same_origin(left: str, right: str) -> bool:
    return _origin(left) == _origin(right)


def fetch_auth_server(issuer: str, settings: Settings) -> AuthServer:
    candidates = _well_known_candidates(issuer, AUTH_SERVER_WELL_KNOWN)
    candidates.extend(_well_known_candidates(issuer, OPENID_WELL_KNOWN))
    with http_client() as client:
        for candidate in candidates:
            document = _get_metadata(client, candidate, settings)
            if document is None:
                continue
            authorize = str(document.get("authorization_endpoint") or "")
            token = str(document.get("token_endpoint") or "")
            if not authorize or not token:
                continue
            declared = str(document.get("issuer") or issuer)
            if declared.rstrip("/") != issuer.rstrip("/"):
                # RFC 8414 §3.3: the `issuer` in the document MUST be identical
                # to the one the well-known URL was built from, and if it is not
                # then "the data contained in the response MUST NOT be used".
                # It is not cosmetic here: the issuer is what the registration
                # is keyed on and what the UI names as the account's provider,
                # so an unchecked one lets a document claim to be Google while
                # the endpoints point somewhere else. Skipped rather than
                # raised, because the next candidate spelling — or the MCP
                # default endpoints below — may still be the right document for
                # this issuer, which is the multi-tenant case.
                continue
            return AuthServer(
                issuer=declared,
                authorization_endpoint=authorize,
                token_endpoint=token,
                registration_endpoint=str(document.get("registration_endpoint") or ""),
                scopes=_string_list(document.get("scopes_supported")),
                code_challenge_methods=_string_list(
                    document.get("code_challenge_methods_supported")
                ),
            )
    # No metadata at all. The MCP spec names default endpoint paths for exactly
    # this case, so a server that implements the flow without publishing a
    # document still connects instead of dying on a KeyError.
    base = issuer.rstrip("/")
    return AuthServer(
        issuer=base,
        authorization_endpoint=f"{base}/authorize",
        token_endpoint=f"{base}/token",
        registration_endpoint=f"{base}/register",
        scopes=(),
    )


def discover(server_url: str, settings: Settings) -> Tuple[ProtectedResource, AuthServer]:
    """Walk 401 -> resource metadata -> authorization-server metadata."""
    challenge = probe_authentication(server_url, settings) or AuthChallenge()
    resource = fetch_protected_resource(server_url, challenge, settings)
    issuer = resource.authorization_servers[0]
    return resource, fetch_auth_server(issuer, settings)


def _pkce_pair() -> Tuple[str, str]:
    """A verifier and its S256 challenge. `plain` is never offered."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def current_client(db: Session, server_id: str) -> Optional[McpOAuthClient]:
    """The registration a token exchange for this server should use.

    `_retire_other_issuers` keeps this single-valued, so the ordering is only a
    belt-and-braces tiebreak: neither this function's callers nor the token rows
    carry an issuer, so if two registrations for one server could coexist there
    would be no way to tell which of them minted the credential in hand.
    """
    return db.scalars(
        select(McpOAuthClient)
        .where(McpOAuthClient.server_id == server_id)
        .order_by(McpOAuthClient.updated_at.desc())
        .limit(1)
    ).first()


def client_for_issuer(
    db: Session, server_id: str, issuer: str
) -> Optional[McpOAuthClient]:
    """The registration for one specific authorization server.

    What `current_client` cannot do. A code exchange knows, from the state row,
    exactly which issuer its authorize URL was built from, and must use that one
    or none — resolving it by recency instead is the authorization-server mix-up
    described in migration 0018.
    """
    return db.scalars(
        select(McpOAuthClient).where(
            McpOAuthClient.server_id == server_id,
            McpOAuthClient.issuer == issuer,
        )
    ).first()


def _retire_other_issuers(db: Session, server_id: str, issuer: str) -> None:
    """Void everything a *previous* authorization server minted for this server.

    An MCP server is allowed to move its authorization server, and one that has
    been compromised will. Leaving the old registration behind made the
    (server -> registration) lookup many-valued, and "newest wins" then aimed the
    refresh leg — which carries a refresh token the *old* issuer minted — at
    whatever host the server most recently nominated, handing one user's
    long-lived credential to a party that never issued it and needing no action
    from that user. Nothing the old issuer minted is usable at the new one
    anyway, so the only safe reading of a rotation is that every credential for
    this server is void and each user consents again.
    """
    stale = list(
        db.scalars(
            select(McpOAuthClient).where(
                McpOAuthClient.server_id == server_id,
                McpOAuthClient.issuer != issuer,
            )
        )
    )
    if not stale:
        return
    for registration in stale:
        db.delete(registration)
    for token in db.scalars(
        select(McpOAuthToken).where(McpOAuthToken.server_id == server_id)
    ):
        # Cleared rather than deleted: the row is what the UI hangs "reconnect"
        # off, and a user whose server silently went blank learns nothing.
        token.access_token_enc = ""
        token.refresh_token_enc = ""
        token.status = "expired"
    db.flush()


def register_client(
    db: Session,
    server: McpServer,
    auth_server: AuthServer,
    scopes: Tuple[str, ...],
    settings: Settings,
) -> McpOAuthClient:
    """Reuse this deployment's registration at this issuer, or mint one.

    Keyed on (server, issuer): a server that moves its authorization server
    must get a fresh registration rather than reuse credentials the new issuer
    never granted.
    """
    redirect_uri = callback_url(settings)
    existing = db.scalar(
        select(McpOAuthClient).where(
            McpOAuthClient.server_id == server.id,
            McpOAuthClient.issuer == auth_server.issuer,
        )
    )
    if existing is not None and existing.client_id:
        if existing.redirect_uri != redirect_uri:
            # OAUTH_REDIRECT_BASE moved under a live registration. Re-registering
            # silently would leave two clients at the issuer and no way to tell
            # which one the next callback belongs to.
            raise McpOAuthError(
                "This deployment's callback URL changed since that server was "
                "registered; disconnect and connect it again"
            )
        existing.authorization_endpoint = auth_server.authorization_endpoint
        existing.token_endpoint = auth_server.token_endpoint
        # A server can rotate away and back again; this branch is reached on the
        # way back, and the intervening issuer's registration must not survive it.
        _retire_other_issuers(db, server.id, auth_server.issuer)
        return existing

    if not auth_server.registration_endpoint:
        raise McpOAuthError(
            "That server's authorization server does not support automatic "
            "client registration, so it cannot be connected here"
        )
    scope = " ".join(scopes or auth_server.scopes)
    payload: Dict[str, Any] = {
        "client_name": "Fieldnote",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        # A public client: dynamic registration hands out no secret worth
        # keeping, and PKCE is what actually protects the code.
        "token_endpoint_auth_method": "none",
    }
    if scope:
        payload["scope"] = scope
    _validate_destination(auth_server.registration_endpoint, settings)
    try:
        with http_client() as client:
            with client.stream(
                "POST", auth_server.registration_endpoint, json=payload
            ) as response:
                if response.status_code not in (200, 201):
                    raise McpOAuthError(
                        f"Client registration was refused ({response.status_code})"
                    )
                document = _read_json(response)
    except httpx.HTTPError as exc:
        raise McpOAuthError("Could not reach that authorization server") from exc
    client_id = str(document.get("client_id") or "")
    if not client_id:
        raise McpOAuthError("Client registration returned no client id")

    # Only now, with a registration in hand, is this issuer the server's — a
    # failed registration must not log everybody out of the one that still works.
    _retire_other_issuers(db, server.id, auth_server.issuer)
    row = existing or McpOAuthClient(
        workspace_id=server.workspace_id,
        server_id=server.id,
        issuer=auth_server.issuer,
    )
    row.authorization_endpoint = auth_server.authorization_endpoint
    row.token_endpoint = auth_server.token_endpoint
    row.registration_endpoint = auth_server.registration_endpoint
    row.client_id = client_id
    row.client_secret_enc = _encrypt_optional(document.get("client_secret"))
    # RFC 7592: without this we could never update or delete the registration
    # we just created, and orphaned clients accumulate at the issuer forever.
    row.registration_access_token_enc = _encrypt_optional(
        document.get("registration_access_token")
    )
    row.registration_client_uri = str(document.get("registration_client_uri") or "")
    row.redirect_uri = redirect_uri
    row.scopes = str(document.get("scope") or scope)
    if existing is None:
        db.add(row)
    db.flush()
    return row


def _encrypt_optional(value: Any) -> str:
    text = str(value or "")
    return encrypt_secret(text) if text else ""


def begin_authorization(
    db: Session, server: McpServer, user_id: str, settings: Settings
) -> str:
    """Discover, register, and return the URL to send the browser to."""
    if not settings.mcp_oauth_enabled:
        raise McpOAuthError("MCP authentication is disabled on this deployment")
    if server.transport != "http" or not server.url:
        raise McpOAuthError("Only remote MCP servers authenticate with OAuth")
    if not settings.integrations_ready:
        raise McpOAuthError(
            "Set INTEGRATIONS_ENCRYPTION_KEY before connecting an MCP account"
        )

    resource, auth_server = discover(server.url, settings)
    if auth_server.code_challenge_methods and (
        "S256" not in auth_server.code_challenge_methods
    ):
        # PKCE downgrade. An authorization server that publishes its supported
        # methods and does not list S256 will ignore the challenge we send, and
        # the code goes back to being a bearer credential — which for a public
        # client with no secret is the only thing protecting it. `plain` is not
        # an acceptable substitute, so this refuses instead of negotiating down.
        # Silence is still accepted: the MCP default-endpoint fallback publishes
        # no metadata at all and most real servers omit the field.
        raise McpOAuthError(
            "That authorization server does not offer PKCE with S256, so it "
            "cannot be connected safely"
        )
    client = register_client(db, server, auth_server, resource.scopes, settings)
    # The authorization endpoint is the one URL in this flow that is handed to
    # the *browser* instead of fetched here, and the web app assigns it to
    # location.href. Unchecked, that is not SSRF but XSS: a metadata document is
    # free to answer `javascript:…//`, and everything appended after it is a
    # comment. The same public-HTTPS guard the fetched hops use settles it.
    _validate_destination(client.authorization_endpoint, settings)

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(STATE_BYTES)[:64]
    db.execute(
        delete(OAuthState).where(
            OAuthState.workspace_id == server.workspace_id,
            OAuthState.created_at < utcnow() - STATE_TTL,
        )
    )
    db.add(
        OAuthState(
            workspace_id=server.workspace_id,
            user_id=user_id,
            provider=STATE_PROVIDER,
            state=state,
            server_id=server.id,
            pkce_verifier_enc=encrypt_secret(verifier),
            redirect_uri=client.redirect_uri,
            issuer=client.issuer,
        )
    )
    db.commit()

    query: Dict[str, str] = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": client.redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # RFC 8707. The canonical URI of the server we are about to call, never
        # the `resource` the metadata document claimed: the document is served
        # by the resource itself and may name somebody else's identifier, which
        # would have the user consent to a token minted for *that* audience and
        # us then present it to this server. The token leg already sends the
        # canonical URI, and both legs must agree or the audience check below is
        # checking a value nobody asked for.
        "resource": canonical_resource(server.url),
    }
    if client.scopes:
        query["scope"] = client.scopes
    separator = "&" if "?" in client.authorization_endpoint else "?"
    return f"{client.authorization_endpoint}{separator}{urlencode(query)}"


def _token_request(
    token_endpoint: str,
    data: Dict[str, str],
    client: McpOAuthClient,
    settings: Settings,
) -> Dict[str, Any]:
    _validate_destination(token_endpoint, settings)
    body = dict(data)
    body["client_id"] = client.client_id
    if client.client_secret_enc:
        body["client_secret"] = decrypt_secret(client.client_secret_enc)
    try:
        with http_client(TOKEN_TIMEOUT) as http:
            with http.stream(
                "POST",
                token_endpoint,
                data=body,
                headers={"Accept": "application/json"},
            ) as response:
                if response.status_code != 200:
                    # The provider's body may quote the code back at us; only
                    # the status crosses this boundary.
                    raise McpOAuthError(
                        f"The token request was refused ({response.status_code})"
                    )
                document = _read_json(response)
    except httpx.HTTPError as exc:
        raise McpOAuthError("Could not reach that authorization server") from exc
    if not str(document.get("access_token") or ""):
        raise McpOAuthError("The authorization server returned no access token")
    token_type = str(document.get("token_type") or "bearer").lower()
    if token_type != "bearer":
        raise McpOAuthError("That authorization server issued an unsupported token type")
    return document


def _store_token(
    db: Session,
    row: McpOAuthToken,
    document: Dict[str, Any],
    resource: str,
    fallback_scopes: str,
) -> McpOAuthToken:
    row.access_token_enc = encrypt_secret(str(document["access_token"]))
    refresh = str(document.get("refresh_token") or "")
    if refresh:
        # An omitted refresh_token on a refresh response means "keep using the
        # one you have"; clearing it would force the user to re-consent.
        row.refresh_token_enc = encrypt_secret(refresh)
    expires_in = document.get("expires_in")
    row.token_expires_at = (
        utcnow() + timedelta(seconds=int(expires_in))
        if isinstance(expires_in, (int, float)) and expires_in > 0
        else None
    )
    row.scopes = str(document.get("scope") or fallback_scopes)
    row.resource = resource
    row.status = "connected"
    return row


def complete_authorization(
    db: Session, state: str, code: str, settings: Settings
) -> McpOAuthToken:
    """Consume the state row, redeem the code, and store the token.

    The state row is deleted before anything else can fail, which is what makes
    it single-use: a replayed callback finds nothing and is refused, whether the
    replay comes from the browser's history or from an attacker who watched the
    redirect go past.
    """
    record = db.scalar(
        select(OAuthState).where(
            OAuthState.provider == STATE_PROVIDER, OAuthState.state == state
        )
    )
    if record is None:
        raise McpOAuthError("That authorization link is invalid or has already been used")
    expired = record.created_at < utcnow() - STATE_TTL
    server_id = record.server_id
    user_id = record.user_id
    workspace_id = record.workspace_id
    redirect_uri = record.redirect_uri
    issuer = record.issuer
    verifier = decrypt_secret(record.pkce_verifier_enc) if record.pkce_verifier_enc else ""
    db.delete(record)
    db.commit()
    if expired:
        raise McpOAuthError("That authorization link has expired; try connecting again")
    if not verifier:
        raise McpOAuthError("That authorization link is missing its proof key")
    if not code:
        raise McpOAuthError("The authorization server did not return a code")

    # Scoped by the workspace the state row recorded, not fetched by primary key
    # and checked afterwards: the filter belongs in the query so there is no
    # window in which a foreign row is in hand at all.
    server = db.scalar(
        select(McpServer).where(
            McpServer.id == server_id, McpServer.workspace_id == workspace_id
        )
    )
    if server is None:
        raise McpOAuthError("That MCP server no longer exists")
    if not issuer:
        # Minted before migration 0018, so which issuer this flow meant is not
        # recorded and cannot be inferred. States live ten minutes; the honest
        # answer is to make the user press Connect again.
        raise McpOAuthError("That authorization link is invalid or has already been used")
    client = client_for_issuer(db, server_id, issuer)
    if client is None:
        # The registration this flow was started against is gone, which is what
        # a server rotating its authorization server mid-flow looks like. Sending
        # the code and verifier to whichever issuer is current *now* would hand
        # the new one everything it needs to redeem them at the old one.
        raise McpOAuthError("That MCP server is no longer registered for authentication")
    if redirect_uri != client.redirect_uri or redirect_uri != callback_url(settings):
        # Strict equality against the value that was registered, not a prefix
        # or suffix test: a redirect URI that merely starts with ours is an
        # open redirect and hands the code to whoever chose the rest of it.
        raise McpOAuthError("The callback address did not match the registered one")

    resource = canonical_resource(server.url)
    document = _token_request(
        client.token_endpoint,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "resource": resource,
        },
        client,
        settings,
    )
    row = db.scalar(
        select(McpOAuthToken).where(
            McpOAuthToken.server_id == server_id, McpOAuthToken.user_id == user_id
        )
    )
    if row is None:
        row = McpOAuthToken(
            workspace_id=workspace_id, server_id=server_id, user_id=user_id
        )
        db.add(row)
    _store_token(db, row, document, resource, client.scopes)
    db.commit()
    return row


def access_token(
    db: Session,
    server: McpServer,
    user_id: str,
    settings: Settings,
    *,
    force_refresh: bool = False,
) -> Optional[str]:
    """This user's usable bearer token for this server, or None.

    None means "not connected" and is the caller's cue to surface a Connect
    button. Anything actively wrong — a token minted for a different resource,
    a refresh the authorization server rejected — raises instead, because
    silently returning None there would look identical to "never connected"
    and the user would consent again into the same broken state.
    """
    if not settings.mcp_oauth_enabled:
        return None
    row = db.scalar(
        select(McpOAuthToken).where(
            McpOAuthToken.server_id == server.id, McpOAuthToken.user_id == user_id
        )
    )
    if row is None or not row.access_token_enc:
        return None

    expected = canonical_resource(server.url)
    if row.resource and row.resource != expected:
        # The audience check. A token minted for server A must not be presented
        # to server B, and the recorded resource is the only evidence of which
        # one it was minted for.
        raise McpOAuthError(
            "The stored credential was issued for a different server; "
            "disconnect and connect it again"
        )

    leeway = timedelta(seconds=max(0, settings.mcp_token_refresh_leeway_seconds))
    # Proactive, not reactive: refreshing on the 401 means a long agent turn
    # fails a tool call it did not have to fail. The leeway is the width of the
    # window a turn is allowed to be.
    threshold = utcnow() + leeway
    stale = row.token_expires_at is not None and row.token_expires_at <= threshold
    if force_refresh or stale:
        refreshed = _refresh(
            db,
            server,
            row,
            expected,
            settings,
            # A forced refresh follows a 401, so the token we hold is known bad
            # however healthy its expiry looks; there is nothing to re-check.
            deadline=None if force_refresh else threshold,
        )
        if refreshed is None:
            # We needed a new token — either it is about to expire, or the
            # server just rejected the one we hold — and could not get one, so
            # what is on the row is unusable. Saying so is what turns this into
            # a Connect button rather than a retry that can never succeed.
            _mark_expired(db, row)
            return None
        return refreshed
    return decrypt_secret(row.access_token_enc)


def _mark_expired(db: Session, row: McpOAuthToken) -> None:
    """Retire a credential the authorization server will not renew.

    The token material is cleared, not just the status. `access_token` returns
    early on an empty `access_token_enc`, and without that this row stays
    "stale" forever: every later tool call would re-enter `_refresh` and make
    another token request the server has already refused — once per call, for as
    long as the row exists. It also means a grant the provider revoked stops
    being held at rest here, which is the right end state for one anyway.
    """
    if row.status != "expired" or row.access_token_enc or row.refresh_token_enc:
        row.status = "expired"
        row.access_token_enc = ""
        row.refresh_token_enc = ""
        db.commit()


def _refresh(
    db: Session,
    server: McpServer,
    row: McpOAuthToken,
    resource: str,
    settings: Settings,
    *,
    deadline: Optional[datetime] = None,
) -> Optional[str]:
    """A new access token, or None if one could not be obtained.

    Never raises: every reason this can fail — no refresh token was issued, the
    registration is gone, the authorization server refused — means the same
    thing to the caller, which is that the user has to consent again.

    `deadline` is the expiry threshold that made the caller decide the token was
    stale, or None when the caller is forcing a refresh after a 401. It is what
    the re-read below compares against, so a waiter can tell "already refreshed
    by someone else" from "still needs refreshing".
    """
    if not row.refresh_token_enc:
        return None
    # Two agent turns for the same (server, user) can cross here easily: the
    # leeway window is minutes wide by design, so this is not a narrow race.
    # OAuth 2.1 requires refresh-token rotation for public clients, and RFC 6819
    # has the authorization server treat a replayed refresh token as a breach
    # and revoke the entire grant family — so both turns spending the same token
    # silently disconnects the user, with nothing in our logs pointing at us.
    # The lock is held until this function's commit. SQLite ignores FOR UPDATE,
    # which is correct rather than merely tolerable: it serialises writers
    # already, and the deployments that run concurrently run Postgres.
    locked = db.scalar(
        select(McpOAuthToken)
        .where(McpOAuthToken.id == row.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        return None
    row = locked
    if not row.refresh_token_enc:
        # Retired while we waited — a rotated issuer, or a disconnect.
        return None
    if (
        deadline is not None
        and row.access_token_enc
        and (row.token_expires_at is None or row.token_expires_at > deadline)
    ):
        # Somebody else refreshed while we blocked. Spending our now-superseded
        # refresh token is exactly the replay described above.
        return decrypt_secret(row.access_token_enc)
    client = current_client(db, server.id)
    if client is None or not client.token_endpoint:
        return None
    try:
        document = _token_request(
            client.token_endpoint,
            {
                "grant_type": "refresh_token",
                "refresh_token": decrypt_secret(row.refresh_token_enc),
                "resource": resource,
            },
            client,
            settings,
        )
    except McpOAuthError:
        # A refused refresh is the user's consent being revoked upstream, which
        # is a state to record rather than an exception to propagate.
        return None
    _store_token(db, row, document, resource, client.scopes)
    db.commit()
    return decrypt_secret(row.access_token_enc)


def disconnect(db: Session, server: McpServer, user_id: str) -> bool:
    """Forget one user's token for one server. Other users keep theirs."""
    row = db.scalar(
        select(McpOAuthToken).where(
            McpOAuthToken.server_id == server.id, McpOAuthToken.user_id == user_id
        )
    )
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def auth_status(db: Session, server: McpServer, user_id: str) -> AuthStatus:
    """Whether this user has to connect, without touching the network."""
    if server.transport != "http":
        return AuthStatus(required=False, connected=False)
    row = db.scalar(
        select(McpOAuthToken).where(
            McpOAuthToken.server_id == server.id, McpOAuthToken.user_id == user_id
        )
    )
    client = current_client(db, server.id)
    if row is None or row.status != "connected" or not row.access_token_enc:
        # "Required" is what we know so far: a registration exists, or the last
        # connection attempt came back 401 and set the status.
        return AuthStatus(
            required=client is not None or server.status == "needs_auth",
            connected=False,
            issuer=client.issuer if client is not None else "",
        )
    remaining: Optional[int] = None
    if row.token_expires_at is not None:
        remaining = max(0, int((row.token_expires_at - utcnow()).total_seconds()))
    return AuthStatus(
        required=True,
        connected=True,
        issuer=client.issuer if client is not None else "",
        scopes=row.scopes,
        expires_in_seconds=remaining,
    )


def token_provider(
    db: Session, server: McpServer, user_id: str, settings: Settings
) -> Callable[[bool], Optional[str]]:
    """Bind (db, server, user) into the callback `client.py` retries with.

    The client layer must not know about sessions or rows; it only needs "give
    me a token" and "that one was rejected, give me a fresh one".
    """

    def provide(force_refresh: bool = False) -> Optional[str]:
        try:
            return access_token(
                db, server, user_id, settings, force_refresh=force_refresh
            )
        except EncryptionNotConfiguredError:
            return None

    return provide
