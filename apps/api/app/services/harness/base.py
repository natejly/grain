from __future__ import annotations

from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Literal,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from ...config import Settings
from ..retrieval import Evidence

#: What a round may do about tools. "auto" is the model's own choice; "none"
#: leaves the tools in the request and forbids calling one.
ToolChoice = Literal["auto", "none"]


@runtime_checkable
class ModelStep(Protocol):
    """One round of one turn.

    Takes (input_items, tools, instructions) and returns an iterable of
    ("delta", text) events followed by ("completed", response), where response
    exposes .output (function calls have .type == "function_call", .name,
    .call_id, .arguments) and .output_text. Injectable so tests can script a
    model offline.

    A Protocol rather than the `Callable` alias it replaces, for exactly one
    reason: `tool_choice` has to be keyword-only with a default, and a
    `Callable[...]` alias cannot say that. The default is what keeps every
    existing three-argument step — every test double in the suite — a valid
    `ModelStep`.

    `tool_choice="none"` is the loop's LAST round. The honest way to say "stop
    calling tools and answer" is to leave the tools array exactly where it was
    and forbid a call; sending `tools=[]` instead says something different and
    more expensive — it changes the request prefix, which is precisely the
    bytes the prompt cache is keyed on, so the cheapest round of the turn
    became the one guaranteed to miss the cache.
    """

    def __call__(
        self,
        input_items: List[Any],
        tools: List[Dict[str, Any]],
        instructions: str,
        *,
        tool_choice: ToolChoice = "auto",
    ) -> Iterable[Tuple[str, Any]]:
        ...


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
        workspace_id: str = "",
        run_id: str = "",
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

        `workspace_id` and `run_id` are the turn's IDENTITY, and they are here
        because the signature carrying none was a structural blocker rather than
        an omission: anything the provider request wants to vary per workspace —
        a prompt cache key today, a per-workspace routing or plan-mode toggle
        tomorrow — had nowhere to come from. Both default to "" (unknown, not
        "none"), so a caller that passes neither sends exactly the request it
        sent before, and a harness must treat "" as "do not vary".

        They are IDs, never rows: the same rule as `user_id`. A harness that
        wanted the `Run` itself would be a harness that could query, and the
        point of this seam is that it cannot.
        """
        ...
