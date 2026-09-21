from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from ...config import Settings
from ..model import _openai_client, privacy_safe_identifier, stream_agent_response
from ..retrieval import Evidence
from .base import ModelStep, ToolChoice


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
        model: Optional[str] = None,
        effort: Optional[str] = None,
        thinking: bool = False,
        workspace_id: str = "",
        run_id: str = "",
    ) -> ModelStep:
        client = _openai_client(settings)
        # The prompt cache routes on this key, so it has to be the thing whose
        # prefix actually repeats: every turn of one workspace shares an agent
        # voice, a space block and a tool payload, while two workspaces share
        # nothing but the stock instructions. A per-RUN key would never hit
        # (each run is a new key) and a global one would spray every workspace
        # across one cache shard.
        #
        # Hashed rather than raw, via the same derivation `safety_identifier`
        # uses: cache routing needs the key to be STABLE, not meaningful, and a
        # workspace id is an internal identifier with no reason to be exported.
        # "" means the caller did not know the workspace — send no key at all
        # rather than a key that collides across every such turn.
        cache_key = privacy_safe_identifier(workspace_id) if workspace_id else ""

        def step(
            input_items: List[Any],
            tools: List[Dict[str, Any]],
            instructions: str,
            *,
            tool_choice: ToolChoice = "auto",
        ) -> Iterable[Tuple[str, Any]]:
            return stream_agent_response(
                client,
                settings,
                user_id=user_id,
                input_items=input_items,
                tools=tools,
                instructions=instructions,
                model=model,
                effort=effort,
                thinking=thinking,
                prompt_cache_key=cache_key,
                # The Responses API takes the same two words this contract
                # uses, so nothing is translated here.
                tool_choice=tool_choice,
            )

        return step
