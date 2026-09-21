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
        thinking: bool = False,
        workspace_id: str = "",
        run_id: str = "",
    ) -> ModelStep:
        # The double talks to no provider, so a per-turn model, effort, or
        # thinking override has nothing to apply to — and neither has the
        # turn's identity, which exists for provider request fields. All five
        # are accepted to satisfy the Protocol and ignored. (`tool_choice` is
        # per ROUND, so it is accepted and ignored one level down, on the step
        # `scripted_model_step` returns.)
        from ..scripted_model import scripted_model_step

        return scripted_model_step(settings, prompt=prompt, evidence=evidence)
