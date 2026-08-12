from __future__ import annotations

from typing import List, Optional

from ...config import Settings
from ..retrieval import Evidence
from .base import ModelStep


class ScriptedHarness:
    """The offline test double as a `Harness`.

    `scripted_model` imports back from `agent_loop`, so `scripted_model_step` is
    imported lazily inside the method to keep the module graph acyclic — the same
    deferred-import discipline the inlined provider branch used.
    """

    name = "scripted"

    def build_step(
        self,
        settings: Settings,
        *,
        prompt: str,
        user_id: str,
        evidence: List[Evidence],
        model: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> ModelStep:
        # The double talks to no provider, so a per-turn model or effort override
        # has nothing to apply to — it accepts both to satisfy the Protocol and
        # ignores them.
        from ..scripted_model import scripted_model_step

        return scripted_model_step(settings, prompt=prompt, evidence=evidence)
