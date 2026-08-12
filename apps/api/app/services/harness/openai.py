from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

from ...config import Settings
from ..model import _openai_client, stream_agent_response
from ..retrieval import Evidence
from .base import ModelStep


class OpenAIHarness:
    """The OpenAI Responses backend as a `Harness`.

    The client is built once in `build_step`, not per step invocation, so a turn
    that streams several rounds reuses one connection pool — the same guarantee the
    inlined provider branch gave before this moved out of the loop.
    """

    name = "openai"

    def build_step(
        self,
        settings: Settings,
        *,
        prompt: str,
        user_id: str,
        evidence: List[Evidence],
    ) -> ModelStep:
        client = _openai_client(settings)

        def step(
            input_items: List[Any], tools: List[Dict[str, Any]], instructions: str
        ) -> Iterable[Tuple[str, Any]]:
            return stream_agent_response(
                client,
                settings,
                user_id=user_id,
                input_items=input_items,
                tools=tools,
                instructions=instructions,
            )

        return step
