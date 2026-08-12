from __future__ import annotations

from typing import List

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
    ) -> ModelStep:
        from ..scripted_model import scripted_model_step

        return scripted_model_step(settings, prompt=prompt, evidence=evidence)
