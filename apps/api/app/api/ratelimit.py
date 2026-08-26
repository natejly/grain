"""General request rate limiting for the non-auth API surface.

`app/api/auth.py` already throttles the unauthenticated credential endpoints
with `auth_rate_limiter`. This is the sibling for everything else: a blunt
ceiling on how much *expensive* work one caller can buy per window — an LLM run,
a docker/LaTeX compile, a codegen pass, a whole-workspace rebuild — plus a
per-IP ceiling on the public and bearer-token doors, which have no session to
key on and today hand an attacker unlimited free work (and unlimited invalid-
token guesses, each one a DB lookup).

It shares the same engine and the same honest scope as the auth limiter: an
in-process fixed window that resets on restart (see
`services/auth/ratelimit.py`). That is a blunt instrument, not a billing system;
a multi-replica deployment that needs exactness wants a shared counter behind
it. Two dependencies are exposed:

* `rate_limit(bucket, tier=...)` keys on the resolved `(workspace, user)`, so it
  runs *after* `get_actor` and one noisy member cannot spend another's budget.
* `public_rate_limit(bucket, tier=...)` keys on the source IP and takes no
  identity dependency, so it runs on unauthenticated and bearer routes and
  fires before an invalid token is ever looked up.

Attaching either as a route dependency is enough; the value it returns is
unused. `RATE_LIMITED_ROUTES` records where each is applied so
`tests/test_rate_limit.py` can assert the high-cost endpoints stay covered.
"""
from __future__ import annotations

from typing import Callable, Literal

from fastapi import Depends, HTTPException, Request, status

from ..auth import Actor, get_actor, get_token_actor
from ..config import Settings, get_settings
from ..services.auth.ratelimit import RateLimiter

# A separate instance from `auth_rate_limiter` so the two budgets never bleed
# into each other; the bucket string still namespaces every endpoint within it.
api_rate_limiter = RateLimiter()

Tier = Literal["heavy", "mint", "public"]


def _client_ip(request: Request) -> str:
    """The peer address, never an X-Forwarded-For header.

    Identical reasoning to `api/auth.py._client_ip`: a forwarded header is
    client-controlled, so honouring it lets an attacker spend everyone else's
    budget or dodge their own. Behind a proxy this collapses to the proxy's
    address and the per-IP tier becomes global — acceptable, because the
    per-identity tier is the one that bites for authenticated abuse.
    """
    return request.client.host if request.client else "unknown"


def _tier_budget(settings: Settings, tier: Tier) -> tuple[int, int]:
    if tier == "heavy":
        return (
            settings.rate_limit_heavy_attempts,
            settings.rate_limit_heavy_window_seconds,
        )
    if tier == "mint":
        return (
            settings.rate_limit_mint_attempts,
            settings.rate_limit_mint_window_seconds,
        )
    return (
        settings.rate_limit_public_attempts,
        settings.rate_limit_public_window_seconds,
    )


def _too_many(window_seconds: int) -> HTTPException:
    # Retry-After lets a well-behaved client back off instead of hammering;
    # the window is the longest it could need to wait for a slot to free.
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="Too many requests. Try again later.",
        headers={"Retry-After": str(window_seconds)},
    )


def rate_limit(bucket: str, *, tier: Tier = "heavy") -> Callable[..., Actor]:
    """A per-identity limiter dependency, keyed on `(workspace, user)`.

    Depends on `get_actor`, so it composes with — and reuses the same
    per-request-cached resolution as — the endpoint's own actor dependency: no
    second cookie/DB round trip. Returns the Actor so an endpoint may take this
    in place of its `Depends(get_actor)` and keep the identity.
    """

    def dependency(
        actor: Actor = Depends(get_actor),
        settings: Settings = Depends(get_settings),
    ) -> Actor:
        if settings.rate_limit_enabled:
            limit, window = _tier_budget(settings, tier)
            key = f"{bucket}:{actor.workspace_id}:{actor.user_id}"
            if not api_rate_limiter.allow(key, limit=limit, window_seconds=window):
                raise _too_many(window)
        return actor

    return dependency


def token_rate_limit(bucket: str, *, tier: Tier = "heavy") -> Callable[..., Actor]:
    """The bearer-door counterpart to `rate_limit`, keyed on the token's actor.

    Depends on `get_token_actor` rather than `get_actor`, so it fits the machine
    routes (hooks, MCP) that authenticate with `Authorization: Bearer grain_…`
    instead of a cookie. Keys on the resolved `(workspace, user)`, so one leaked
    or looping token cannot start unbounded LLM runs. Pair it with a
    `public_rate_limit` listed first when invalid-token flooding also matters.
    """

    def dependency(
        actor: Actor = Depends(get_token_actor),
        settings: Settings = Depends(get_settings),
    ) -> Actor:
        if settings.rate_limit_enabled:
            limit, window = _tier_budget(settings, tier)
            key = f"{bucket}:{actor.workspace_id}:{actor.user_id}"
            if not api_rate_limiter.allow(key, limit=limit, window_seconds=window):
                raise _too_many(window)
        return actor

    return dependency


def public_rate_limit(bucket: str, *, tier: Tier = "public") -> Callable[..., None]:
    """A per-IP limiter dependency for unauthenticated and bearer routes.

    Takes no identity dependency on purpose: list it *before* the route's token
    resolver and it throttles invalid-token guessing too, since a rejected
    bearer never reaches an Actor to key on.
    """

    def dependency(
        request: Request,
        settings: Settings = Depends(get_settings),
    ) -> None:
        if settings.rate_limit_enabled:
            limit, window = _tier_budget(settings, tier)
            key = f"{bucket}:{_client_ip(request)}"
            if not api_rate_limiter.allow(key, limit=limit, window_seconds=window):
                raise _too_many(window)

    return dependency
