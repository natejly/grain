"""Guardian: a cheap-model reviewer over parked write-capable tool calls.

The approval modes park a write behind a human decision. That is correct and
also slow, and for a large class of calls — appending the row the user just
dictated, writing the file the prompt named — the human click is a formality.
The guardian is the *only* mechanism allowed to skip it, and only in one
direction: it may convert "ask a human" into "execute now" when a small model is
confident the call is a routine, clearly-safe application of what the user asked
for. It may never convert anything into a denial, a retry, or a different call —
every outcome that is not a clean approval is a defer, and a defer is exactly
the park the loop was about to do anyway.

Fail-closed is the whole contract, so it is structural rather than aspirational:

- `review` never raises. Any exception inside it — client construction, the
  network, a provider outage, a parsing surprise — becomes a defer verdict with
  the error clipped into the reason. The loop needs no try/except; a guardian
  that is down is a guardian that asks the human, which is the pre-guardian
  behavior.
- Parsing is allow-listed, not denial-listed: only a JSON object whose
  ``decision`` is exactly the string ``"approve"`` approves. Malformed JSON,
  missing keys, a different casing, a chatty preamble the extractor cannot see
  through — all defer.
- Only the two real providers may approve. The scripted double defers
  unconditionally — a test that did not explicitly fake a verdict must never
  see an approval.

The model call mirrors `screen.py`'s builtin backend: the deployment's cheap
context model (`openai_context_model` / `anthropic_context_model`), bounded
inputs, one call, billed under its own operation string so an operator can see
what auto-approval costs. The call itself lives behind a module-level seam
(`_call_model`) so tests drive the parsing and fail-closed properties without a
network.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..config import Settings

#: Usage-ledger operation string for guardian model calls. Deliberately its own
#: operation rather than piggybacking on `usage.SCREEN`: an operator deciding
#: whether auto-approval pays for itself needs its spend legible on its own line.
GUARDIAN_OPERATION = "guardian"

#: Ceiling on guardian approvals in one turn. Exported for the agent loop to
#: enforce — this module reviews one call at a time and holds no turn state. The
#: cap bounds the blast radius of a run of over-confident verdicts: past it,
#: every write parks for a human no matter what the guardian says.
MAX_GUARDIAN_APPROVALS_PER_TURN = 5

#: Input bounds. Arguments and preview mirror what the park itself persists
#: (`arguments_json[:4000]`, preview `[:20000]`) but tighter — the guardian is a
#: cheap-model triage, not an audit, and bounding its input bounds both the
#: spend and what an adversarial tool argument can splice into the prompt.
MAX_GUARDIAN_PROMPT_CHARS = 2_000
MAX_GUARDIAN_ARGUMENTS_CHARS = 2_000
MAX_GUARDIAN_PREVIEW_CHARS = 4_000

#: A verdict reason is surfaced on the approval card / audit trail; clip it so a
#: model that rambles (or an exception message carrying a payload) stays legible.
MAX_GUARDIAN_REASON_CHARS = 300

GUARDIAN_INSTRUCTIONS = (
    "You are a cautious reviewer inside an AI assistant. The assistant wants to "
    "run a write-capable tool call that would normally wait for the human user "
    "to approve it. Your ONLY power is to skip that wait when the call is a "
    "routine, clearly-safe, direct application of what the user asked for in "
    "their prompt. If the call is surprising, destructive, ambiguous, touches "
    "credentials, money, permissions, or mass deletion, or is not plainly what "
    "the user asked for, you must defer to the human. When in any doubt, defer. "
    'Reply with strict JSON, nothing else: {"decision": "approve", "reason": '
    '"..."} or {"decision": "defer", "reason": "..."}.'
)


@dataclass(frozen=True)
class GuardianVerdict:
    """What the guardian concluded about one parked tool call.

    ``approve=True`` is the only verdict with power: it lets the loop execute
    the call instead of parking it. Everything else — a defer, a parse failure,
    a provider error — is ``approve=False`` and lands on the exact park the loop
    would have done without a guardian. `reason` is prose for the audit trail,
    never machine-read.
    """

    approve: bool
    reason: str


def _clip(text: str, limit: int) -> str:
    """Head of `text`, bounded. Empty-safe; the guardian never sees None."""
    return (text or "")[:limit]


def _call_model(*, payload: str, settings: Settings) -> str:
    """The one model call, isolated as the test seam, per provider.

    OpenAI mirrors `screen._classify_builtin` — the cheap context model through
    the `call_responses` chokepoint, so the spend is recorded under
    `GUARDIAN_OPERATION` whether or not a caller thought about billing.
    Anthropic goes through the harness's own client seam and records usage
    explicitly, same operation, `provider="anthropic"`. Imports are lazy for
    the same reason screen's are — this module stays importable without either
    SDK's service graph.

    Anything this raises is caught by `review`; it makes no fail-closed promises
    of its own.
    """
    if settings.active_model_provider == "anthropic":
        return _call_model_anthropic(payload=payload, settings=settings)
    from .model import _openai_client, call_responses

    client = _openai_client(settings)
    response = call_responses(
        client,
        operation=GUARDIAN_OPERATION,
        settings=settings,
        model=settings.openai_context_model,
        instructions=GUARDIAN_INSTRUCTIONS,
        input=payload,
        # "minimal", not "none": the small models reject "none" outright.
        reasoning={"effort": "minimal"},
        text={"verbosity": "low"},
        # Room for the JSON envelope plus a sentence of reason; a truncated
        # reply fails JSON parsing and therefore defers, which is safe.
        max_output_tokens=200,
        store=False,
    )
    return str(response.output_text)


def _call_model_anthropic(*, payload: str, settings: Settings) -> str:
    """The Anthropic leg of `_call_model`: one bounded Messages call.

    Reuses the harness's client seam (`harness.anthropic._client`) so a test
    that fakes the harness's SDK fakes this too, and its usage shim so the
    ledger row carries the same corrected token arithmetic every other
    Anthropic call gets. Thinking is disabled explicitly: a 200-token triage
    must spend its budget on the verdict, not on deliberation the parser
    never sees.
    """
    from . import usage
    from .harness.anthropic import _client, _shim_usage

    client = _client(settings)
    message = client.messages.create(
        model=settings.anthropic_context_model,
        max_tokens=200,
        system=GUARDIAN_INSTRUCTIONS,
        messages=[{"role": "user", "content": payload}],
        thinking={"type": "disabled"},
    )
    usage.record_model_usage(
        model=settings.anthropic_context_model,
        operation=GUARDIAN_OPERATION,
        usage=_shim_usage(getattr(message, "usage", None)),
        provider="anthropic",
        settings=settings,
    )
    parts = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", "") or "")
    return "".join(parts)


def _extract_json_object(raw: str) -> object:
    """Best-effort JSON object from a model reply. Raises ValueError otherwise.

    Small models fence or preface their JSON often enough that a strict
    `json.loads` alone would defer calls the model plainly approved. The
    extraction is still conservative: one attempt on the stripped text, one on
    the outermost ``{...}`` span, and any failure raises — the caller maps that
    to a defer, never to an approval.
    """
    text = (raw or "").strip()
    # Strip a Markdown code fence if the whole reply is one.
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fenced is not None:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except ValueError:
        span = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if span is None:
            raise
        return json.loads(span.group())


def _parse_verdict(raw: str) -> GuardianVerdict:
    """The model's reply as a verdict. Only an exact approval approves.

    The allow-list is the point: ``decision`` must be exactly the string
    ``"approve"`` inside a JSON object. ``"Approve"``, ``"approved"``, a bare
    ``true``, the right word under the wrong key — every one of those is a model
    that did not follow the contract, and a model that cannot follow the output
    contract has not earned the power to skip a human.
    """
    try:
        data = _extract_json_object(raw)
    except ValueError:
        return GuardianVerdict(False, "guardian returned unparseable output")
    if not isinstance(data, dict):
        return GuardianVerdict(False, "guardian returned a non-object reply")
    decision = data.get("decision")
    reason = _clip(str(data.get("reason") or ""), MAX_GUARDIAN_REASON_CHARS)
    if decision == "approve":
        return GuardianVerdict(True, reason or "guardian approved")
    if decision == "defer":
        return GuardianVerdict(False, reason or "guardian deferred")
    return GuardianVerdict(False, "guardian returned an unrecognized decision")


def review(
    *,
    name: str,
    arguments_json: str,
    preview: str,
    prompt: str,
    settings: Settings,
) -> GuardianVerdict:
    """Triage one parked write-capable tool call. Never raises.

    Returns ``approve=True`` only when the cheap model answered the strict JSON
    contract with an exact approval. Every other path — a non-OpenAI provider, a
    provider error, malformed output, an explicit defer — returns
    ``approve=False``, which the loop treats as the ordinary human park. The
    inputs are clipped before they reach the model, so neither a huge argument
    blob nor an enormous preview can inflate the spend or crowd out the framing.
    """
    try:
        if settings.active_model_provider not in ("openai", "anthropic"):
            # The scripted double. Not an error: a provider without a guardian
            # is simply a deployment where every write asks a human, which is
            # the fail-safe default this feature layers on top of.
            return GuardianVerdict(False, "guardian requires a real model provider")
        payload = (
            "User's prompt for this turn:\n"
            + _clip(prompt, MAX_GUARDIAN_PROMPT_CHARS)
            + "\n\nProposed tool call:\nname: "
            + _clip(name, 200)
            + "\narguments: "
            + _clip(arguments_json, MAX_GUARDIAN_ARGUMENTS_CHARS)
            + "\n\nHuman-readable preview of the change:\n"
            + _clip(preview, MAX_GUARDIAN_PREVIEW_CHARS)
        )
        return _parse_verdict(_call_model(payload=payload, settings=settings))
    except Exception as exc:  # noqa: BLE001 - any failure defers to the human park
        return GuardianVerdict(False, _clip(f"guardian_error: {exc}", MAX_GUARDIAN_REASON_CHARS))
