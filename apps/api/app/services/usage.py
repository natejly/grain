"""Token and cost accounting for every model call this app makes.

Two halves that meet at one function.

*Attribution* is ambient. A model call happens six frames below whoever knew
which workspace was paying — `recall` embeds a query, `situate_chunk` runs
inside a thread pool, the agent loop streams from a step function that has no
session. Threading a workspace id through all of that would mean editing every
signature between here and there, and the one call site that forgot would record
an orphan row. So the caller that *does* know binds a `usage_scope`, and the
recorder reads it out of a `ContextVar`. Scopes nest and merge: an outer scope
that knows the run and an inner one that only knows the workspace compose into
one attribution rather than overwriting each other.

*Capture* is a chokepoint. `services/model.call_responses` and
`services/embeddings.embed_texts` are the only places an OpenAI response is
turned into a row, so a new model call site inherits accounting by construction
instead of by remembering.

Three rules hold everywhere below.

**Accounting never breaks a turn.** Every path out of `record_model_usage`
returns; nothing raises. A missing usage object records nothing, a malformed one
records the fields that parsed, and a database that will not take the insert
costs a ledger row and a log line. A run that failed because it could not bill
itself would be a strictly worse product than one with a gap in its ledger.

**Counts and identifiers only.** No prompt text, no completion text, no tool
arguments reach this module.

**A price is never invented.** With no configured rate the tokens are recorded
and `cost_usd` stays null. Zero would read as "free" and a neighbouring model's
rate would read as measured.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Iterator, Optional, Tuple

from ..config import ModelPrice, Settings, get_settings
from ..database import SessionLocal
from ..models import ModelUsage

logger = logging.getLogger(__name__)

# What caused the spend. Kept as constants rather than an enum so the column can
# accept an unknown value from a future caller without a migration, while every
# call site in this repo still spells the same string.
CHAT = "chat"
WORKFLOW_NODE = "workflow_node"
EMBEDDING = "embedding"
CODEGEN = "codegen"
CONTEXT_BLURB = "context_blurb"
MEMORY_EXTRACTION = "memory_extraction"
GRAPH_EXTRACTION = "graph_extraction"
WORKFLOW_COMPILE = "workflow_compile"
# The prompt-injection screen's builtin classifier. Billed like every other
# small-model call so an operator sees what screening the ingested content costs.
SCREEN = "screen"

OPERATIONS = (
    CHAT,
    WORKFLOW_NODE,
    EMBEDDING,
    CODEGEN,
    CONTEXT_BLURB,
    MEMORY_EXTRACTION,
    GRAPH_EXTRACTION,
    WORKFLOW_COMPILE,
    SCREEN,
)

TOKENS_PER_MILLION = 1_000_000


@dataclass(frozen=True)
class Attribution:
    """Who a model call is billed to. Empty strings mean "not known here"."""

    workspace_id: str = ""
    run_id: str = ""
    conversation_id: str = ""
    user_id: str = ""
    # How an *agent turn* under this scope is billed. The one operation the
    # chokepoint cannot name for itself: `stream_agent_response` is the same call
    # whether a person is typing or a scheduled workflow is executing, and which
    # of those it is depends on what started the run — a fact only the binder
    # has. Every other call site states its own operation and never reads this.
    operation: str = ""


UNATTRIBUTED = Attribution()

# Defaulted to None rather than to UNATTRIBUTED so the default is unmistakably
# immutable; `current_attribution` maps it back to the empty attribution.
_ATTRIBUTION: ContextVar[Optional[Attribution]] = ContextVar(
    "model_usage_attribution", default=None
)


@contextmanager
def usage_scope(
    *,
    workspace_id: str = "",
    run_id: str = "",
    conversation_id: str = "",
    user_id: str = "",
    operation: str = "",
) -> Iterator[Attribution]:
    """Bind what this frame knows about who is paying, for the calls beneath it.

    Fields left blank are *inherited*, not cleared. That is what lets the two
    natural binding points coexist without either having to know about the
    other: `write_conversation_memory` binds workspace + run + conversation +
    user, and `_embed_pending` beneath it re-binds only the workspace it can see
    on the rows — and the run survives.

    A `ContextVar` rather than a thread local so the binding travels with the
    task, not with whichever thread happens to serve it: Starlette runs this
    app's sync endpoints through `anyio.to_thread`, which copies the calling
    context into the worker. What it does *not* survive is a bare
    `ThreadPoolExecutor`, whose threads start with a fresh context — so a fan-out
    that spends tokens has to open its scope inside the worker (see
    `ingestion.contextualize_chunks`).

    It is reset on exit, and that is load-bearing rather than tidy: a scope left
    bound leaks onto the next task to reuse the thread, and the failure there is
    not a missing row, it is a row billed to the wrong tenant.
    """
    current = current_attribution()
    merged = replace(
        current,
        workspace_id=workspace_id or current.workspace_id,
        run_id=run_id or current.run_id,
        conversation_id=conversation_id or current.conversation_id,
        user_id=user_id or current.user_id,
        operation=operation or current.operation,
    )
    token = _ATTRIBUTION.set(merged)
    try:
        yield merged
    finally:
        _ATTRIBUTION.reset(token)


def current_attribution() -> Attribution:
    """Who the model call about to happen is billed to. Never None."""
    return _ATTRIBUTION.get() or UNATTRIBUTED


@dataclass(frozen=True)
class TokenCounts:
    """One call's token counts, already coerced to sane non-negative integers.

    `cached_input_tokens` is a subset of `input_tokens` and `reasoning_tokens` a
    subset of `output_tokens`, matching how the provider reports them.
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    @property
    def uncached_input_tokens(self) -> int:
        # A provider that reports more cached tokens than input tokens is
        # malformed, not a reason to bill a negative amount.
        return max(0, self.input_tokens - self.cached_input_tokens)


def _field(source: object, *names: str) -> Any:
    """First present attribute or mapping key among `names`, else None."""
    for name in names:
        if isinstance(source, dict):
            if name in source:
                return source[name]
        elif hasattr(source, name):
            return getattr(source, name)
    return None


def _count(source: object, *names: str) -> int:
    """A token count from an untrusted response object; 0 for anything unusable.

    Every value here crosses a network boundary from a provider that is free to
    change its schema, and a `None` or a string where an int was expected must
    cost a field rather than the whole row.
    """
    value = _field(source, *names)
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def token_counts(usage: object) -> Optional[TokenCounts]:
    """Parse a provider usage object, or None when there is nothing to record.

    Handles both shapes the OpenAI SDK returns, because both are in use here:
    the Responses API reports `input_tokens` / `output_tokens` with
    `*_tokens_details`, and the embeddings API reports `prompt_tokens` /
    `total_tokens` with no completion half at all.

    None means the call reported no usage — the scripted test double, or an SDK
    object that predates the field. That is silence, not a zero-cost call, and
    writing a row of zeros for it would make the ledger claim a measurement it
    never took.
    """
    if usage is None:
        return None
    input_tokens = _count(usage, "input_tokens", "prompt_tokens")
    output_tokens = _count(usage, "output_tokens", "completion_tokens")
    total = _count(usage, "total_tokens")
    input_details = _field(usage, "input_tokens_details", "prompt_tokens_details")
    output_details = _field(usage, "output_tokens_details", "completion_tokens_details")
    cached = _count(input_details, "cached_tokens") if input_details is not None else 0
    reasoning = (
        _count(output_details, "reasoning_tokens") if output_details is not None else 0
    )
    if not (input_tokens or output_tokens or total or cached or reasoning):
        # An object that answered nothing to every name we know is indistinguishable
        # from no usage at all — most often a test double built with SimpleNamespace.
        return None
    return TokenCounts(
        input_tokens=input_tokens,
        cached_input_tokens=min(cached, input_tokens) if input_tokens else cached,
        output_tokens=output_tokens,
        reasoning_tokens=min(reasoning, output_tokens) if output_tokens else reasoning,
        # Fall back to the sum only when the provider did not state a total, so a
        # provider that counts differently than we would is not silently corrected.
        total_tokens=total or (input_tokens + output_tokens),
    )


def compute_cost(
    counts: TokenCounts, price: Optional[ModelPrice]
) -> Tuple[Optional[float], Optional[ModelPrice]]:
    """(cost in USD, the rate that produced it). (None, None) with no rate.

    Cached input is billed at its own rate and the remainder at the full one;
    reasoning tokens are *already inside* `output_tokens` and are therefore not
    charged again — counting them twice would inflate every reasoning-heavy turn.
    """
    if price is None:
        return None, None
    cost = (
        counts.uncached_input_tokens * price.input
        + counts.cached_input_tokens * price.effective_cached_input
        + counts.output_tokens * price.output
    ) / TOKENS_PER_MILLION
    return cost, price


def record_model_usage(
    *,
    model: str,
    operation: str,
    usage: object,
    provider: str = "openai",
    settings: Optional[Settings] = None,
) -> Optional[str]:
    """Persist one call's usage. Returns the row id, or None when nothing landed.

    The single writer, and the reason it swallows everything: it is called from
    inside a streaming agent turn, an ingest, and a thread pool, none of which
    have any business failing because a ledger insert did. Callers do not check
    the return value; it exists for tests.

    Its own session, committed immediately, rather than the caller's. A ledger
    row must survive a turn that later rolls back — the tokens were spent either
    way — and an accounting error must never poison a transaction that is in the
    middle of writing someone's answer.
    """
    try:
        counts = token_counts(usage)
        if counts is None:
            # Not an error: the scripted provider has no usage to report, and a
            # provider that stopped sending one should degrade to a gap in the
            # ledger rather than to a wrong number in it.
            logger.debug("model call reported no usage (operation=%s)", operation)
            return None
        attribution = current_attribution()
        if not attribution.workspace_id:
            # Everything in this schema is workspace-scoped, so a row with no
            # tenant could be neither shown nor scoped. Loud, because the fix is
            # a missing `usage_scope` at a new call site and nothing else will
            # ever surface it.
            logger.warning(
                "model usage not attributed to a workspace; dropping the row "
                "(operation=%s, model=%s). A caller is missing usage_scope().",
                operation,
                model,
            )
            return None
        settings = settings or get_settings()
        cost, rate = compute_cost(counts, settings.price_for(model))
        row = ModelUsage(
            workspace_id=attribution.workspace_id,
            run_id=attribution.run_id,
            conversation_id=attribution.conversation_id,
            user_id=attribution.user_id,
            operation=operation,
            provider=provider,
            model=model[:120],
            input_tokens=counts.input_tokens,
            cached_input_tokens=counts.cached_input_tokens,
            output_tokens=counts.output_tokens,
            reasoning_tokens=counts.reasoning_tokens,
            total_tokens=counts.total_tokens,
            cost_usd=cost,
            input_rate_usd_per_mtok=None if rate is None else rate.input,
            cached_input_rate_usd_per_mtok=(
                None if rate is None else rate.effective_cached_input
            ),
            output_rate_usd_per_mtok=None if rate is None else rate.output,
        )
        db = SessionLocal()
        try:
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()
    except Exception:
        logger.warning(
            "model usage accounting failed (operation=%s, model=%s)",
            operation,
            model,
            exc_info=True,
        )
        return None
