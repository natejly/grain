# 0006 — Authenticating to MCP servers: dynamic registration, per-user tokens

## Status

Accepted. Extends the MCP client introduced alongside ADR 0003's tool layer.

## Context

The MCP client passed a static header dict and nothing else — no 401 handling,
no metadata discovery, no PKCE, no client registration, no refresh. That is the
difference between "we have an MCP client" and "we can reach MCP servers".
Roughly half the public registry is remote rather than stdio, most of those
authenticate, and a good number will not even *list* their tools to an anonymous
caller. Every connector a user would actually name — Slack, Notion, Linear,
Jira, GitHub, Sentry — sits behind that gate. Without OAuth the feature is a
local-subprocess curiosity.

Three properties of this problem drive everything below, and none of them
resemble our existing Google connector:

1. **There is no console step.** A user pastes a URL for a server we have never
   heard of and expects it to work. Nobody is going to register an OAuth client
   with that provider and paste a client id into our settings. So registration
   has to happen at connect time, automatically, per server.
2. **The server is the adversary.** A connector we ship has a URL we chose. An
   MCP server has a URL a *user* typed, and every document it serves — the 401
   challenge, the protected-resource metadata, the authorization-server metadata
   — is attacker-controlled input that we then fetch or navigate to.
3. **The credential belongs to a person, not to a workspace.** An MCP server
   authorises a human. Two colleagues sharing a workspace must not share one
   Linear account.

## Decision

### Dynamic client registration (RFC 7591), as a public client

We register ourselves with each authorization server at connect time and persist
the result in `mcp_oauth_clients`, keyed on `(server_id, issuer)`. There is no
alternative that preserves property 1: the only other options are an operator
console step per server (which defeats the feature) or a shared pre-registered
client (which no third-party authorization server would issue us).

We register with `token_endpoint_auth_method: none` — a public client — because
there is nowhere to keep a per-server secret that the server itself did not just
hand us. That makes **PKCE the only thing protecting the authorization code**,
which is why S256 is mandatory and `plain` is refused outright rather than
negotiated down. An authorization server that publishes
`code_challenge_methods_supported` without S256 is refused before any state row
is minted. Absence of the field is still accepted: the MCP spec's default-endpoint
fallback publishes no metadata at all, and refusing on silence would break every
self-hosted server.

### Tokens are per (server, user), never per workspace

`mcp_oauth_tokens` is unique on `(server_id, user_id)`. The agent loop resolves a
token from `run.created_by`, so a turn uses the credential of the person who
typed the prompt, not of whoever set the server up. `disconnect` removes one
user's row and leaves their colleagues' alone.

### The host allowlist is relaxed — deliberately, and only that

`TOOL_HOST_ALLOWLIST` exists to stop the tool layer fetching arbitrary URLs.
Arbitrary URLs are precisely the feature here, so discovery relaxes it, by
`settings.model_copy(update={"tool_host_allowlist": <host of the URL in hand>})`.

**Everything else in `validate_public_https_url` stays in force**: HTTPS only,
DNS resolution, and refusal of private, loopback, link-local, multicast,
reserved and unspecified addresses. `169.254.169.254` and `10.0.0.0/8` are still
refused. The guard runs on *every* hop — the initialize probe, protected-resource
metadata, authorization-server metadata, registration, token — and also on the
`authorization_endpoint`, which is not fetched but handed to the browser, where
an unchecked `javascript:` URL is XSS in our own origin rather than SSRF.

Redirects are not followed (`follow_redirects=False`): a 302 is a second
attacker-chosen URL that would arrive after the guard had already run.

### The bindings that make the flow unambiguous

Three separate things were, or could have been, resolved by "most recent wins".
Each is now bound explicitly, because each is an audience- or issuer-confusion
attack:

- **Resource indicators (RFC 8707)** are sent on both the authorize and token
  legs as `canonical_resource(server.url)` — the URL we know — never the
  `resource` the server's own metadata document claimed. A server that declares
  someone else's resource identifier would otherwise have the authorization
  server mint a token audienced for *that* API and hand it to us.
- **The authorization server is checked against the issuer** the well-known URL
  was built from (RFC 8414 §3.3), so a metadata document cannot claim to be
  Google's while pointing its endpoints at the attacker's host.
- **`oauth_states.issuer`** (migration 0018) records which registration the
  authorize URL was built from, and the callback resolves the client by that
  issuer or refuses. Without it, a server that rotates its advertised
  authorization server mid-flow gets the victim's authorization code *and* PKCE
  verifier posted to it — everything needed to redeem them at the honest issuer.
  This is the mix-up RFC 9207's `iss` parameter exists to answer; binding it
  server-side achieves the same thing without depending on the authorization
  server implementing 9207.

### The callback is unauthenticated, and the state row is the credential

The browser arrives from the authorization server as a top-level navigation, so
there is no session cookie to read. The single-use state row carries the user it
belongs to; taking the identity from anything in the query string would let an
attacker attach their account to somebody else's server. The row is deleted and
committed before anything that can fail, which is what makes it single-use.

## Consequences

- **We now hold third-party credentials at rest.** Access tokens, refresh
  tokens, client secrets and RFC 7592 registration tokens are Fernet-encrypted
  via `services/crypto.py` and gated on `integrations_ready`. `resource`,
  `scopes` and `issuer` are the only plaintext columns and none is a credential.
  This is a genuine escalation of what a database compromise is worth, and
  `THREAT_MODEL.md` records it as such.
- **A user-supplied URL now reaches an HTTP client on the server.** The
  allowlist relaxation is the single control we gave up, and the SSRF guard is
  load-bearing rather than defence in depth. Its residual weakness is DNS
  rebinding: `_validate_destination` resolves and validates, then httpx resolves
  again at connect time. Multi-A-record answers are handled (every address is
  checked); a TTL-0 rebind between the two resolutions is not. Closing it needs
  a pinned-IP transport, which is tracked rather than done.
- **Registration accumulates.** Each server the user connects mints a client
  registration with a third party. A server that rotates its issuer causes a
  re-registration and retires the old one, which also voids every user's token
  for that server — correct, and visible as everyone being asked to reconnect.
- **Disconnect is local.** No `revocation_endpoint` is called, so a disconnected
  token stays live at the provider until it expires. The button promises a
  little more than it delivers.
- **Loopback MCP servers cannot use OAuth.** `http://127.0.0.1` is a permitted
  *server* URL but discovery refuses non-HTTPS, so local servers stay on the
  no-auth path. Intentional.
