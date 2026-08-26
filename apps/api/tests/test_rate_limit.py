"""The general API rate limiter: the dependencies, and the routes they cover.

The engine itself (`RateLimiter`) is exercised by the auth-limiter tests; these
assert the wiring — that the per-identity, per-token, and per-IP dependencies
count and refuse correctly, that the master switch disables them, that separate
identities and IPs get separate budgets, and (the tripwire) that the high-cost
endpoints still carry a limiter so a future route cannot quietly ship without
one.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import ratelimit
from app.api.ratelimit import (
    api_rate_limiter,
    public_rate_limit,
    rate_limit,
    token_rate_limit,
)
from app.auth import Actor
from app.config import Settings
from app.main import app


def _settings(**overrides) -> Settings:
    base = dict(
        app_env="test",
        rate_limit_enabled=True,
        rate_limit_heavy_attempts=3,
        rate_limit_heavy_window_seconds=60,
        rate_limit_public_attempts=3,
        rate_limit_public_window_seconds=60,
    )
    base.update(overrides)
    return Settings(**base)


def _actor(workspace_id="w1", user_id="u1") -> Actor:
    return Actor(
        user_id=user_id,
        user_name="Tester",
        workspace_id=workspace_id,
        workspace_name="WS",
        role="owner",
    )


def _request(ip: str):
    return SimpleNamespace(client=SimpleNamespace(host=ip))


@pytest.fixture(autouse=True)
def _clean_limiter():
    api_rate_limiter.reset()
    yield
    api_rate_limiter.reset()


def test_per_identity_limit_refuses_after_the_budget():
    dep = rate_limit("unit-heavy", tier="heavy")
    settings = _settings()
    actor = _actor()
    for _ in range(3):
        assert dep(actor=actor, settings=settings) is actor
    with pytest.raises(HTTPException) as caught:
        dep(actor=actor, settings=settings)
    assert caught.value.status_code == 429
    assert caught.value.headers["Retry-After"] == "60"


def test_separate_identities_have_separate_budgets():
    dep = rate_limit("unit-heavy", tier="heavy")
    settings = _settings()
    a, b = _actor(user_id="a"), _actor(user_id="b")
    for _ in range(3):
        dep(actor=a, settings=settings)
    # b's budget is untouched by a spending theirs.
    for _ in range(3):
        assert dep(actor=b, settings=settings) is b
    with pytest.raises(HTTPException):
        dep(actor=b, settings=settings)


def test_the_master_switch_disables_the_limiter():
    dep = rate_limit("unit-heavy", tier="heavy")
    settings = _settings(rate_limit_enabled=False)
    actor = _actor()
    for _ in range(50):
        assert dep(actor=actor, settings=settings) is actor


def test_public_limit_keys_per_ip():
    dep = public_rate_limit("unit-public")
    settings = _settings()
    for _ in range(3):
        assert dep(request=_request("10.0.0.1"), settings=settings) is None
    with pytest.raises(HTTPException) as caught:
        dep(request=_request("10.0.0.1"), settings=settings)
    assert caught.value.status_code == 429
    # A different source address is a different bucket.
    assert dep(request=_request("10.0.0.2"), settings=settings) is None


def test_token_limit_keys_per_identity():
    dep = token_rate_limit("unit-token", tier="heavy")
    settings = _settings()
    actor = _actor()
    for _ in range(3):
        assert dep(actor=actor, settings=settings) is actor
    with pytest.raises(HTTPException) as caught:
        dep(actor=actor, settings=settings)
    assert caught.value.status_code == 429


# --- Coverage tripwire ------------------------------------------------------

# The paths whose abuse cost (LLM runs, docker/LaTeX compiles, codegen, whole-
# workspace rebuilds, credential minting, unauthenticated reads) makes a missing
# limiter a real hole. If a route here loses its limiter, this test fails.
_MUST_BE_LIMITED = {
    ("POST", "/api/latex/compile"),
    ("POST", "/api/conversations/{conversation_id}/messages"),
    ("POST", "/api/conversations/{conversation_id}/messages/{message_id}/edit"),
    ("POST", "/api/workflows/{workflow_id}/run"),
    ("POST", "/api/workflows/compile"),
    ("POST", "/api/workflows/tick"),
    ("POST", "/api/graph/rebuild"),
    ("POST", "/api/apps/{app_id}/generate"),
    ("POST", "/api/sandbox"),
    ("POST", "/api/sandbox/{session_id}/run"),
    ("POST", "/api/sources"),
    ("POST", "/api/api-tokens"),
    ("POST", "/api/share-links"),
    ("GET", "/shared/{token}"),
    ("GET", "/published/apps/{slug}"),
    ("GET", "/published/apps/{slug}/frame"),
    ("POST", "/api/hooks/workflows/{workflow_id}/trigger"),
    ("POST", "/api/hooks/conversations/{conversation_id}/messages"),
    ("POST", "/api/hooks/email/inbound"),
    ("POST", "/api/mcp"),
}


def _has_rate_limit_dependency(route) -> bool:
    """True if any dependency in the route's tree comes from the limiter module."""
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        call = getattr(dep, "call", None)
        if getattr(call, "__module__", "") == ratelimit.__name__:
            return True
        stack.extend(dep.dependencies)
    return False


def _iter_api_routes(routes):
    """Yield every leaf APIRoute, recursing through FastAPI's included-router
    wrappers (`_IncludedRouter`), which do not flatten into `app.routes`."""
    for route in routes:
        if hasattr(route, "dependant") and getattr(route, "methods", None):
            yield route
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _iter_api_routes(nested.routes)


def test_high_cost_routes_carry_a_rate_limiter():
    by_key = {}
    for route in _iter_api_routes(app.routes):
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or not methods:
            continue
        for method in methods:
            by_key.setdefault((method, path), route)

    missing = []
    for method, path in sorted(_MUST_BE_LIMITED):
        route = by_key.get((method, path))
        if route is None:
            missing.append(f"{method} {path} (route not found)")
        elif not _has_rate_limit_dependency(route):
            missing.append(f"{method} {path} (no limiter dependency)")
    assert not missing, "high-cost routes missing a rate limiter: " + ", ".join(missing)
