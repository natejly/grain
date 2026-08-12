"""Named model backends behind one agent turn.

The agent loop drives a single per-turn `ModelStep`; which backend produces that
step is resolved here from the already-validated provider setting, not branched on
inside the loop. This package is the seam a third backend plugs into: adding
Anthropic or an OpenRouter router is a new `Harness` class plus one `HARNESSES`
entry — the loop and the `ModelStep` contract do not change. (At the config level
that new backend also widens the `model_provider` Literal and adds its startup
guard in `config._guard_model_provider`, and ships its SDK; those are config and
dependency changes, deliberately outside this seam, so this is a drop-in for the
*loop*, not a zero-config change to the app.)
"""

from __future__ import annotations

from typing import Dict

from ...config import Settings
from .base import Harness, ModelStep
from .openai import OpenAIHarness
from .scripted import ScriptedHarness

# Harness instances are stateless singletons — all configuration arrives per call
# through `build_step(settings, ...)` — so module-level instantiation is safe.
HARNESSES: Dict[str, Harness] = {
    "openai": OpenAIHarness(),
    "scripted": ScriptedHarness(),
}


def resolve_harness(settings: Settings) -> Harness:
    """The backend for this process.

    Keyed on the already-validated `active_model_provider`, so in normal operation
    it never resolves a backend `Settings` refused to boot: the startup guard in
    `config._guard_model_provider` is the gate (openai requires a key, scripted is
    dev/test only). Registering a third entry is the whole cost of adding a backend
    to the loop.

    The explicit membership check is not redundant with that gate: it names the one
    seam the gate does *not* cover — a provider that widens the `model_provider`
    Literal and adds its config guard but forgets a `HARNESSES` entry. Without this
    that omission surfaces as a bare `KeyError` on the first turn; with it, it says
    exactly what is missing.
    """
    provider = settings.active_model_provider
    harness = HARNESSES.get(provider)
    if harness is None:
        raise ValueError(
            f"No harness is registered for model provider “{provider}”. Add a "
            f"Harness implementation to services/harness/ and register it in "
            f"HARNESSES (known: {', '.join(sorted(HARNESSES))})."
        )
    return harness


__all__ = ["Harness", "ModelStep", "HARNESSES", "resolve_harness"]
