"""Choosing a driver.

One function, and everything above it depends only on the Protocol. The cache is
keyed on the three settings that actually change a client's identity — provider,
key, template — rather than on the `Settings` object, which is unhashable and
whose unrelated fields change constantly. A test that swaps the provider gets a
new instance; a request handler calling this per tool call does not rebuild one.

The cache is also what makes `FakeProvider` usable: its sandboxes live in the
instance, so a session created by one call has to still exist for the next.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Tuple

from ...config import Settings
from .policy import sandbox_env
from .types import SandboxError, SandboxProvider


@lru_cache(maxsize=8)
def _build(
    provider: str,
    api_key: str,
    template: str,
    workdir: str,
    image: str,
    docker: str,
    python_binary: str,
    memory_mb: int,
    cpus: float,
    pids: int,
    env: Tuple[Tuple[str, str], ...],
) -> SandboxProvider:
    # Imported inside so that a deployment pays only for the driver it uses:
    # `fake` and the local drivers never pull in the e2b SDK, and nothing pulls
    # in anything when SANDBOX_ENABLED=0.
    if provider == "fake":
        from .fake import FakeProvider

        return FakeProvider()
    if provider == "subprocess":
        from .subprocess_provider import SubprocessProvider

        return SubprocessProvider(
            workdir=Path(workdir),
            env=dict(env),
            python_binary=python_binary or sys.executable,
            memory_mb=memory_mb,
            pids_limit=pids,
        )
    if provider == "container":
        from .container_provider import ContainerProvider

        return ContainerProvider(
            workdir=Path(workdir),
            env=dict(env),
            image=image,
            docker_binary=docker,
            memory_mb=memory_mb,
            cpus=cpus,
            pids_limit=pids,
        )
    from .e2b_provider import E2BProvider

    return E2BProvider(api_key=api_key, template=template)


def get_provider(settings: Settings) -> SandboxProvider:
    """The configured driver, or a legible refusal.

    Refusing here rather than returning a provider that fails on first use keeps
    the "sandbox is switched off" message distinct from "the sandbox broke",
    which are different problems for whoever reads it.
    """
    if not settings.sandbox_ready:
        if not settings.sandbox_enabled:
            raise SandboxError(
                "Code execution is turned off for this deployment "
                "(SANDBOX_ENABLED is not set)."
            )
        raise SandboxError(
            "Code execution is not configured: the sandbox provider has no API key."
        )
    key = settings.sandbox_api_key
    return _build(
        settings.sandbox_provider,
        key.get_secret_value() if key is not None else "",
        settings.sandbox_template,
        str(settings.sandbox_workdir),
        settings.sandbox_container_image,
        settings.sandbox_docker_binary,
        settings.sandbox_python_binary,
        settings.sandbox_memory_mb,
        settings.sandbox_cpus,
        settings.sandbox_pids_limit,
        # The environment the sandbox sees, resolved here so the drivers never
        # touch Settings and cannot accidentally reach a secret through it.
        tuple(sorted(sandbox_env(settings).items())),
    )


def reset_provider_cache() -> None:
    """Drop cached drivers. For tests, and for a config reload — a `FakeProvider`
    that outlives a test carries its sandboxes into the next one."""
    _build.cache_clear()
