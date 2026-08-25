from __future__ import annotations

from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from ...config import Settings
from ..retrieval import Evidence

# A model step takes (input_items, tools, instructions) and returns an iterable of
# ("delta", text) events followed by ("completed", response), where response
# exposes .output (function calls have .type == "function_call", .name, .call_id,
# .arguments) and .output_text. Injectable so tests can script a model offline.
ModelStep = Callable[[List[Any], List[Dict[str, Any]], str], Iterable[Tuple[str, Any]]]


@runtime_checkable
class Harness(Protocol):
    """One model backend behind a turn.

    This is the whole contract the agent loop depends on to run one model step:
    given a turn's prompt, requester and evidence, hand back the `ModelStep` the
    loop drives. The loop never learns which backend answered — that is the point.
    A second backend that forked the loop instead of conforming here is the exact
    failure this abstraction prevents: streaming, tool handling and citation
    harvesting would drift between copies. A third backend (Anthropic, OpenRouter)
    becomes a drop-in by implementing this one method and registering a name.

    Usage and cost accounting is the harness's own responsibility, recorded inside
    the returned step exactly as `stream_agent_response` does today. Nothing about
    the signature lets a caller add a backend that silently escapes billing:
    `build_step` takes no `operation` argument, so attribution stays carried by the
    `usage_scope` the loop has already opened.
    """

    name: str

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
    ) -> ModelStep:
        """Return the `ModelStep` for one turn.

        Only primitives cross this boundary — never a `Run` — so a harness stays
        free of DB models and captures exactly the fields the loop reads off a run
        (`prompt`, `created_by`, and the per-turn overrides). `evidence` is
        consumed only by the scripted double as its unscripted-answer fallback; a
        real backend receives the same passages through the prompt and ignores it
        here.

        `model` and `effort` are the per-turn overrides; each defaults to None,
        meaning "use the deployment default", so a caller that passes neither gets
        exactly the pre-override behaviour. A double with no provider ignores both.
        """
        ...
