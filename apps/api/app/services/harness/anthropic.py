"""The Anthropic Messages backend as a `Harness`.

The agent loop speaks the OpenAI Responses dialect: history items are
`function_call` / `function_call_output` dicts, tools are flat
``{"type": "function", ...}`` entries, and the step's terminal response exposes
`.output` / `.output_text`. That dialect is the loop's persistence format — runs
park mid-turn with their items serialized to JSON — so this harness translates
at its own edge instead of teaching the loop a second vocabulary. Everything
Anthropic-specific lives here; the loop never learns which backend answered.

Translation map (both directions):

- seed / assistant text messages      <-> Anthropic `user`/`assistant` turns
- ``function_call`` items             <-> assistant `tool_use` content blocks
- ``function_call_output`` items      <-> user `tool_result` content blocks
- flat Responses function tools       <-> ``{name, description, input_schema}``

Item types with no Anthropic equivalent (reasoning items, hosted web-search
calls) are dropped on the way in: they are provider-private state, and a
history that crossed providers mid-run cannot replay them. The two tool-call
item types are never dropped — silently losing one would desynchronise the
tool_use/tool_result pairing the Messages API validates, so a malformed one
raises instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from anthropic import Anthropic

from ...config import Settings
from .. import usage
from ..model import ModelConfigurationError, privacy_safe_identifier
from ..retrieval import Evidence
from .base import ModelStep


def _client(settings: Settings) -> Anthropic:
    """Build the SDK client. Module-level so tests can monkeypatch it away.

    Mirrors `_openai_client`: the key guard fails loudly at the call site that
    needed a model rather than deep inside an HTTP retry, and `max_retries=1`
    matches the OpenAI client so a flaky provider costs one retry, not the SDK
    default of two.
    """
    if settings.anthropic_api_key is None or not settings.anthropic_api_key.get_secret_value():
        raise ModelConfigurationError(
            "MODEL_PROVIDER is anthropic but ANTHROPIC_API_KEY is not configured"
        )
    return Anthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        timeout=settings.anthropic_timeout_seconds,
        max_retries=1,
    )


@dataclass
class _FunctionCallItem:
    """Shaped like the Responses API's function_call output item.

    The loop reads `.type`, `.call_id`, `.name`, `.arguments` off completed
    responses and re-serialises the item into history via `vars()`, so this
    stays a plain dataclass with exactly the wire fields — same pattern as the
    scripted double, which is what guarantees the loop cannot tell them apart.
    """

    call_id: str
    name: str
    arguments: str
    type: str = "function_call"


@dataclass
class _ThinkingItem:
    """An Anthropic thinking block, carried through the loop's history.

    With extended thinking on, a tool-using turn MUST replay the assistant's
    thinking block (its `signature` proves integrity) in the resent assistant
    message, or the Messages API rejects the continuation. So unlike the
    OpenAI reasoning items this harness drops on the way in, Anthropic's
    thinking blocks are round-tripped: emitted as a JSON-safe output item
    here, translated back into a `thinking` content block by
    `_anthropic_messages`. The `type` is namespaced so no other backend ever
    mistakes it for something it can replay.
    """

    thinking: str
    signature: str
    type: str = "anthropic_thinking"


@dataclass
class _RedactedThinkingItem:
    """A redacted thinking block — same replay contract, opaque payload."""

    data: str
    type: str = "anthropic_redacted_thinking"


@dataclass
class _MessageItem:
    """Shaped like the Responses API's assistant message output item.

    Emitted alongside function calls so that when the loop extends history with
    `response.output`, the assistant's visible text re-enters the next request
    — Anthropic requires the resent assistant turn to carry the text it said
    before its tool calls, or the model loses its own words mid-turn.
    """

    content: List[Dict[str, str]]
    type: str = "message"
    role: str = "assistant"


@dataclass
class _AnthropicResponse:
    """The terminal object a step yields with ("completed", ...)."""

    output: List[Any] = field(default_factory=list)
    output_text: str = ""


@dataclass
class _UsageShim:
    """An Anthropic usage object re-shaped so `usage.token_counts` parses it.

    `token_counts` speaks the OpenAI schema: `input_tokens` is the *whole*
    prompt with cached tokens inside it (`input_tokens_details.cached_tokens`),
    and the cache discount is applied by subtraction. Anthropic reports the
    complement — `input_tokens` *excludes* cache reads and writes, which arrive
    as separate `cache_read_input_tokens` / `cache_creation_input_tokens`
    counts. Feeding the raw object through would under-bill every cached turn,
    so this shim re-adds the pieces: total input = uncached + read + written,
    cached = reads only (cache *writes* bill near full rate, so they belong in
    the uncached remainder).
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int
    input_tokens_details: Dict[str, int]


def _shim_usage(raw: object) -> Optional[_UsageShim]:
    """Adapt one Anthropic usage object; None when there is nothing to record.

    None (not a zero row) for a missing usage, matching `token_counts`'s own
    contract that silence must stay distinguishable from a free call.
    """
    if raw is None:
        return None

    def count(name: str) -> int:
        value = getattr(raw, name, None)
        if isinstance(value, bool) or value is None:
            return 0
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    uncached = count("input_tokens")
    cache_read = count("cache_read_input_tokens")
    cache_written = count("cache_creation_input_tokens")
    output_tokens = count("output_tokens")
    input_tokens = uncached + cache_read + cache_written
    return _UsageShim(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_tokens_details={"cached_tokens": cache_read},
    )


def _get(item: Any, key: str) -> Any:
    """One history item field, whether the item is a dict or an object.

    Items normally arrive as plain dicts (they survive the JSON round trip a
    parked run takes), but a same-process turn can hand back the dataclasses
    this module itself produced; reading both shapes here means the translator
    never cares which path the item took.
    """
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _text_of(content: Any) -> str:
    """The plain text of a message item's content, whichever shape it took.

    Seed messages carry a bare string; replayed Responses message items carry a
    list of parts whose text lives under `text`. Unknown part types contribute
    nothing rather than failing the turn — they are formatting, not meaning.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            text = _get(part, "text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "".join(parts)
    return ""


def _anthropic_messages(input_items: List[Any]) -> List[Dict[str, Any]]:
    """Translate loop history into Anthropic `messages`.

    Content blocks are folded into as few messages as possible: consecutive
    blocks with the same role share one message. That is not a tidiness pass —
    the Messages API requires every `tool_result` for a parallel tool round to
    arrive in a *single* user message, and an assistant turn's text must sit in
    the same message as the `tool_use` blocks that followed it. Folding by role
    produces both groupings without tracking tool rounds explicitly.
    """
    messages: List[Dict[str, Any]] = []

    def append(role: str, block: Dict[str, Any]) -> None:
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].append(block)
        else:
            messages.append({"role": role, "content": [block]})

    for item in input_items:
        item_type = _get(item, "type")
        if item_type == "anthropic_thinking":
            append(
                "assistant",
                {
                    "type": "thinking",
                    "thinking": str(_get(item, "thinking") or ""),
                    "signature": str(_get(item, "signature") or ""),
                },
            )
        elif item_type == "anthropic_redacted_thinking":
            append(
                "assistant",
                {
                    "type": "redacted_thinking",
                    "data": str(_get(item, "data") or ""),
                },
            )
        elif item_type == "function_call":
            call_id = _get(item, "call_id")
            name = _get(item, "name")
            raw_arguments = _get(item, "arguments")
            if not call_id or not name:
                # Never silently dropped: a lost call desynchronises the
                # tool_use/tool_result pairing for the whole rest of the turn.
                raise ValueError("function_call item is missing call_id or name")
            try:
                arguments = json.loads(raw_arguments) if raw_arguments else {}
            except (TypeError, ValueError) as error:
                raise ValueError(
                    f"function_call {call_id} has non-JSON arguments"
                ) from error
            append(
                "assistant",
                {"type": "tool_use", "id": call_id, "name": name, "input": arguments},
            )
        elif item_type == "function_call_output":
            call_id = _get(item, "call_id")
            if not call_id:
                raise ValueError("function_call_output item is missing call_id")
            append(
                "user",
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": str(_get(item, "output") or ""),
                },
            )
        elif item_type in (None, "message"):
            role = _get(item, "role")
            if role not in ("user", "assistant"):
                continue  # system text travels via `instructions`, not history
            text = _text_of(_get(item, "content"))
            if text:
                append(str(role), {"type": "text", "text": text})
        # Anything else (reasoning items, hosted tool calls, ...) has no
        # Anthropic equivalent and is provider-private: skipped by design.
    return messages


def _anthropic_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Translate flat Responses function tools into Anthropic tool params.

    Non-function entries (the hosted `web_search` tool) are dropped rather than
    refused: hosted tools are a per-provider capability, and a turn configured
    for OpenAI's search should degrade to "no search" here, not fail.
    """
    translated: List[Dict[str, Any]] = []
    for tool in tools:
        if _get(tool, "type") != "function" or not _get(tool, "name"):
            continue
        translated.append(
            {
                "name": _get(tool, "name"),
                "description": _get(tool, "description") or "",
                "input_schema": _get(tool, "parameters") or {"type": "object"},
            }
        )
    return translated


def _thinking_kwargs(effort: Optional[str]) -> Dict[str, Any]:
    """The request's thinking configuration for one effort level.

    Not a guessed equivalence: the Messages API's own `output_config.effort`
    ladder is literally {"low","medium","high","xhigh","max"} — the same
    strings this deployment's `ReasoningEffort` uses — so those five map to
    adaptive thinking steered by that knob. "none" maps to thinking disabled,
    and an absent effort sends no configuration at all, leaving the model's
    own default — the same "unset means deployment default" convention every
    other per-turn override follows.
    """
    if not effort:
        return {}
    if effort == "none":
        return {"thinking": {"type": "disabled"}}
    if effort in ("low", "medium", "high", "xhigh", "max"):
        return {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }
    return {}


class AnthropicHarness:
    """The Anthropic Messages backend behind the loop's `Harness` seam.

    The client is built once in `build_step`, not per step invocation, so a
    turn that streams several rounds reuses one connection pool — the same
    guarantee the OpenAI harness gives.

    `effort` maps onto Anthropic's own effort ladder (`_thinking_kwargs`),
    with `settings.openai_reasoning_effort` as the unset fallback — the
    setting predates the second harness and is, despite its name, the
    deployment's reasoning default. Thinking blocks the model emits are
    round-tripped through the loop's history (`_ThinkingItem`), because a
    tool-using turn with thinking on must replay them or the API refuses the
    continuation.
    """

    name = "anthropic"

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
        client = _client(settings)
        # Captured once so the usage record is keyed on the model actually
        # requested; a per-turn override must not be priced as the default.
        chosen_model = model or settings.anthropic_model
        chosen_effort = effort or settings.openai_reasoning_effort

        def step(
            input_items: List[Any], tools: List[Dict[str, Any]], instructions: str
        ) -> Iterator[Tuple[str, Any]]:
            kwargs: Dict[str, Any] = {
                "model": chosen_model,
                "max_tokens": settings.anthropic_max_output_tokens,
                "messages": _anthropic_messages(input_items),
                "metadata": {"user_id": privacy_safe_identifier(user_id)},
                **_thinking_kwargs(chosen_effort),
            }
            if instructions:
                kwargs["system"] = instructions
            translated_tools = _anthropic_tools(tools)
            if translated_tools:
                kwargs["tools"] = translated_tools
            with client.messages.stream(**kwargs) as stream:
                for event in stream:
                    if getattr(event, "type", "") == "text":
                        yield "delta", getattr(event, "text", "") or ""
                message = stream.get_final_message()
            thinking_items: List[Any] = []
            output: List[Any] = []
            text_parts: List[str] = []
            for block in getattr(message, "content", []) or []:
                block_type = getattr(block, "type", "")
                if block_type == "text":
                    text_parts.append(getattr(block, "text", "") or "")
                elif block_type == "tool_use":
                    output.append(
                        _FunctionCallItem(
                            call_id=getattr(block, "id", "") or "",
                            name=getattr(block, "name", "") or "",
                            arguments=json.dumps(getattr(block, "input", None) or {}),
                        )
                    )
                elif block_type == "thinking":
                    thinking_items.append(
                        _ThinkingItem(
                            thinking=getattr(block, "thinking", "") or "",
                            signature=getattr(block, "signature", "") or "",
                        )
                    )
                elif block_type == "redacted_thinking":
                    thinking_items.append(
                        _RedactedThinkingItem(data=getattr(block, "data", "") or "")
                    )
                # Other server blocks carry no history the loop can replay.
            output_text = "".join(text_parts)
            if output_text:
                # Ahead of the calls, mirroring Responses ordering: the loop
                # replays `.output` in order, and the translator above needs
                # the text to land in the same assistant message as the
                # tool_use blocks that follow it.
                output.insert(
                    0, _MessageItem(content=[{"type": "output_text", "text": output_text}])
                )
            # Thinking first of all: the API requires an assistant message's
            # blocks in [thinking, text, tool_use] order on replay, and the
            # translator folds items into messages in list order.
            output = [*thinking_items, *output]
            # Recorded before the terminal yield, exactly like the OpenAI
            # streaming path: a consumer that stops reading at "completed" must
            # not be able to skip the ledger write.
            usage.record_model_usage(
                model=chosen_model,
                operation=(usage.current_attribution().operation or usage.CHAT),
                usage=_shim_usage(getattr(message, "usage", None)),
                provider="anthropic",
                settings=settings,
            )
            yield "completed", _AnthropicResponse(output=output, output_text=output_text)

        return step
