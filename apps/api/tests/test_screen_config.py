"""`config._guard_screen`: the prompt-injection screen refuses an incoherent boot.

The guard is the same structural gate as `_guard_sandbox`: a screen the
deployment cannot actually run is worse than no screen, because an operator who
switched on enforce mode believes untrusted content is being gated when it is
silently passing through. So a proxy backend with no reachable, allowlisted
HTTPS URL fails at *startup*, not on the first turn that ingests a source.

These build a real `Settings` (the validators run only on construction), so they
depend on config/env — not a `--noconftest` unit test. The classifier's own pure
tests live in `test_screen.py`.
"""
from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings

# api.github.com is the default TOOL_HOST_ALLOWLIST, so a proxy URL on that host
# is the one the SSRF guard would actually permit.
ALLOWLISTED_PROXY = "https://api.github.com/screen"


def _base(**overrides: object) -> Settings:
    """A Settings that boots (openai + key), with the screen fields overridden.

    Mirrors `test_model_provider`: `_env_file=None` and an explicit key so the
    only thing under test is the screen guard, not the provider guard.
    """
    return Settings(
        _env_file=None,
        model_provider="openai",
        openai_api_key=SecretStr("test-key"),
        **overrides,
    )


def test_enforce_proxy_without_a_url_refuses_to_boot() -> None:
    """The core claim: enforce + proxy backend + empty URL fails at startup."""
    with pytest.raises(ValidationError, match="SCREEN_PROXY_URL"):
        _base(
            screen_enabled=True,
            screen_mode="enforce",
            screen_backend="proxy",
            screen_proxy_url="",
        )


def test_proxy_with_a_non_https_url_refuses_to_boot() -> None:
    with pytest.raises(ValidationError, match="https"):
        _base(
            screen_enabled=True,
            screen_backend="proxy",
            screen_proxy_url="http://api.github.com/screen",
        )


def test_proxy_off_the_host_allowlist_refuses_to_boot() -> None:
    with pytest.raises(ValidationError, match="TOOL_HOST_ALLOWLIST"):
        _base(
            screen_enabled=True,
            screen_backend="proxy",
            screen_proxy_url="https://evil.example.net/screen",
        )


def test_a_coherent_proxy_config_boots() -> None:
    """The guard is specific, not a blanket refusal: a valid proxy config boots.

    Pins that the validator does not over-reject — enforce + proxy + an
    allowlisted https URL is exactly the intended configuration.
    """
    settings = _base(
        screen_enabled=True,
        screen_mode="enforce",
        screen_backend="proxy",
        screen_proxy_url=ALLOWLISTED_PROXY,
    )
    assert settings.screen_enabled is True
    assert settings.screen_proxy_url == ALLOWLISTED_PROXY


def test_disabled_screen_ignores_an_incoherent_proxy_config() -> None:
    """Opt-in and additive: with the screen off, a stale proxy config is inert.

    An operator can leave screen fields half-configured while the screen is
    disabled; the guard only bites once `screen_enabled` is true.
    """
    settings = _base(
        screen_enabled=False,
        screen_mode="enforce",
        screen_backend="proxy",
        screen_proxy_url="",
    )
    assert settings.screen_enabled is False
