"""Delegation: the `delegate` tool, and the read-only child loop behind it.

The opencode/Codex harnesses converged on the same shape for subagents: the
model is handed a delegation tool, the *model* decides when to fan out, and the
child runs with strictly narrower powers than its parent. This module is that
shape, sized to this codebase's guarantees:

- **A child is read-only by construction.** Its registry is the target agent's
  provisioned subset intersected with `read_only` specs, minus `force_ask`
  tools and minus `delegate` itself — so a child can never write, never park on
  an approval, and never recurse. Depth 1 is structural, not a counter.
- **A child never parks.** The two things that park a parent turn — approvals
  and the spend ceiling — cannot and must not park a child, because the parked
  state would belong to a tool call rather than a run and nothing could resume
  it. Approvals are impossible (read-only registry); the budget check aborts
  the child with an error result instead, and the parent's own loop-head check
  parks the *parent* properly on its next iteration.
- **A child writes no run events.** `run_events` carries a
  `UniqueConstraint(run_id, sequence)`, and children may execute in parallel
  threads; concurrent `append_event` on one run is an IntegrityError race. The
  delegate call itself is the observable unit — its `tool.started` /
  `tool.completed` events and `AgentToolCall` row are written serially by the
  loop, like every other tool.
- **Billing rides the ambient scope.** Child model calls go through the same
  harness chokepoints, and `usage_scope` is a ContextVar, so a child running in
  the parent's thread inherits attribution for free. A child running in a
  worker thread is started via `contextvars.copy_context()` (see
  `agent_loop._drain_pending`), which carries the same scope across.

What a child returns is one `ToolResult`: the sub-agent's final answer, plus a
compact list of the passages it read. Evidence objects are deliberately *not*
propagated into the parent's numbered evidence: the child's own ``[n]`` markers
refer to its private numbering, and splicing its passages into the parent's
list would renumber them out from under the text that cites them. Quoting the
sources inline keeps the answer and its support attached to each other.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Agent, Run
from . import budget, screen
from .audit import record_audit
from .harness import ModelStep, resolve_harness
from .llm_tools import ToolContext, ToolResult, ToolSpec, build_registry
from .retrieval import Evidence

#: A child gets fewer iterations than a parent (6): it exists to answer one
#: focused question, and a question that needs more than three tool rounds and
#: an answer belongs in the parent's own loop where a person can steer it.
MAX_CHILD_ITERATIONS = 4

#: The name, exported so the loop's parallel-batch path and the child-registry
#: exclusion cannot drift from the spec below.
DELEGATE_TOOL = "delegate"

#: How many delegate calls from one model step run concurrently. Above this the
#: rest simply wait — the cap bounds worker threads and database sessions, not
#: correctness.
MAX_PARALLEL_DELEGATES = 4

_PROMPT_LIMIT = 6000


class ChildAborted(RuntimeError):
    """The child stopped early for a reason the parent should read as an error
    string, not as an answer: cancellation, the spend ceiling, a screen hit,
    or running out of iterations.

    `flagged` carries the excerpt a screen hit stopped on, so the abort can
    hand the evidence back to the parent instead of laundering it into a
    clean sentence — see `_screen_excerpt`."""

    def __init__(self, message: str, flagged: str = "") -> None:
        super().__init__(message)
        self.flagged = flagged


def _child_step(
    settings: Settings,
    *,
    prompt: str,
    user_id: str,
    model: Optional[str],
    effort: Optional[str],
) -> ModelStep:
    """The model behind one child turn. A module-level seam, like
    `agent_loop._default_model_step`, so tests can replace the model without
    replacing the loop around it."""
    return resolve_harness(settings).build_step(
        settings,
        prompt=prompt,
        user_id=user_id,
        evidence=[],
        model=model,
        effort=effort,
    )


def _child_registry(
    db: Session, context: ToolContext, agent: Agent
) -> Dict[str, ToolSpec]:
    """What a child may see: the agent's provisioned subset, read-only half.

    `force_ask` tools are excluded even though they are read-only, because
    their authors demanded a human look at every call — a child has no way to
    ask one. `delegate` and `ask_user` are excluded by the same rule that
    excludes writes: a child that could recurse or park would re-import the
    complexity this narrowing exists to remove.
    """
    allowed: Optional[frozenset[str]] = None
    raw = agent.allowed_tools_json
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            allowed = frozenset(str(item) for item in parsed)
    full = build_registry(db, context, allowed=allowed)
    return {
        name: spec
        for name, spec in full.items()
        if spec.read_only and not spec.force_ask and name != DELEGATE_TOOL
    }


def _child_instructions(agent: Agent) -> str:
    from .model import CHAT_INSTRUCTIONS

    base = agent.instructions.strip() or CHAT_INSTRUCTIONS
    return (
        f"{base}\n\n"
        "You are running as a delegated sub-agent for another agent's turn. "
        "Only read-only tools are available; do not attempt to create, edit, "
        "or delete anything. Research the request below and return your "
        "findings as your final message — it is handed back verbatim to the "
        "delegating agent, so make it self-contained."
    )


def _render_child_output(result: ToolResult, offset: int) -> str:
    """The child-local rendering of one tool result — same shape as the parent
    loop's `_render_result`, numbered against the child's own evidence list."""
    parts: List[str] = []
    if result.content:
        parts.append(result.bounded_content())
    for index, item in enumerate(result.evidence, start=offset + 1):
        parts.append(
            f"[{index}] {item.filename}, passage {item.ordinal + 1}\n{item.excerpt}"
        )
    return "\n\n".join(parts) if parts else "(empty result)"


#: How much of a flagged passage rides back to the parent. Enough for the
#: parent's own classifier to re-detect it (and for a reader to see what
#: happened), small enough not to crowd the delegate's result.
_FLAGGED_EXCERPT_CHARS = 1000


def _screen_excerpt(text: str, settings: Settings) -> Optional[str]:
    """Classify one child-ingested string; return a flagged excerpt, or None.

    The child cannot write `run_events` (it may be running in a worker thread
    and the table is unique on (run_id, sequence)), so it cannot record the
    flag itself. Instead the *evidence* travels: the caller folds the returned
    excerpt into the delegate's result content, and the parent's serial
    screening of that content re-classifies it and writes the flag, the audit
    row, and — in enforce — the per-turn ASK_ALL escalation, exactly as if the
    parent had read the passage directly. A hit that returned only a clean
    error sentence would be a detection with no record and no escalation.
    Fail-closed like `_screen`: a backend error counts as a hit.
    """
    if not settings.screen_enabled or not text.strip():
        return None
    try:
        verdict = screen.classify(text, kind="tool_output", settings=settings)
        flagged = verdict.label == "injection"
    except screen.ScreenError:
        flagged = True
    if not flagged:
        return None
    return text[:_FLAGGED_EXCERPT_CHARS]


def run_child_agent(
    db: Session,
    context: ToolContext,
    *,
    agent: Agent,
    prompt: str,
    settings: Optional[Settings] = None,
    step: Optional[ModelStep] = None,
) -> ToolResult:
    """One delegated turn: a bounded, read-only, non-parking agent loop.

    `db` may be a worker thread's own session — everything here must stay on
    it. Raises nothing: every failure becomes an error `ToolResult` the parent
    model can read and route around.
    """
    settings = settings or get_settings()
    run = db.get(Run, context.run_id) if context.run_id else None
    if run is None or run.workspace_id != context.workspace_id:
        return ToolResult(content="Error: delegation requires a live run.")

    from .agent_loop import _serialize_item, policy_scope_for_run

    scope_unattended = policy_scope_for_run(db, run) == "workflow"
    registry = _child_registry(db, context, agent)
    instructions = _child_instructions(agent)
    tools_payload: List[Dict[str, Any]] = [
        {
            "type": "function",
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        }
        for spec in registry.values()
    ]
    step = step or _child_step(
        settings,
        prompt=prompt,
        user_id=context.user_id,
        model=run.requested_model or None,
        effort=run.requested_effort or None,
    )
    input_items: List[Any] = [{"role": "user", "content": prompt}]
    evidence: List[Evidence] = []
    #: Passages the screen flagged in shadow mode. Shadow means "record, never
    #: enforce", and the record is made by handing the excerpts back to the
    #: parent's serial screening — dropping them here would make shadow mode
    #: blind to exactly the source class it exists to measure.
    shadow_hits: List[str] = []

    try:
        for iteration in range(MAX_CHILD_ITERATIONS):
            db.refresh(run)
            if run.cancel_requested:
                raise ChildAborted("the run was cancelled")
            verdict = budget.evaluate(
                db,
                workspace_id=run.workspace_id,
                unattended=scope_unattended,
                settings=settings,
            )
            if not verdict.allowed:
                # The parent's own loop-head check parks the parent properly on
                # its next iteration; the child just declines to spend more.
                raise ChildAborted(
                    "the workspace spend limit was reached, so the delegated "
                    "agent stopped early"
                )
            final_round = iteration == MAX_CHILD_ITERATIONS - 1
            response: Any = None
            text_parts: List[str] = []
            for kind, value in step(
                input_items, [] if final_round else tools_payload, instructions
            ):
                if kind == "delta":
                    text_parts.append(str(value))
                elif kind == "completed":
                    response = value
            if response is None:
                raise ChildAborted("the delegated agent's model stream ended early")
            calls = [
                item
                for item in (response.output or [])
                if getattr(item, "type", None) == "function_call"
            ]
            if not calls:
                answer = ("".join(text_parts) or response.output_text or "").strip()
                if not answer:
                    raise ChildAborted("the delegated agent returned nothing")
                return _answer_result(answer, evidence, agent, shadow_hits)
            input_items.extend(_serialize_item(item) for item in response.output)
            for call in calls:
                name = str(getattr(call, "name", "") or "")
                raw_arguments = getattr(call, "arguments", "") or "{}"
                result = _execute_child_call(
                    db, context, registry, name=name, raw_arguments=str(raw_arguments)
                )
                record_audit(
                    db,
                    workspace_id=run.workspace_id,
                    actor_id=run.created_by,
                    action="delegate_tool.executed",
                    resource_type="run",
                    resource_id=run.id,
                    detail={"tool": name, "agent": agent.name},
                )
                db.commit()
                excerpt = _screen_excerpt(
                    "\n\n".join(
                        [result.content or "", *(item.excerpt for item in result.evidence)]
                    ),
                    settings,
                )
                if excerpt is not None:
                    if settings.screen_mode == "enforce":
                        raise ChildAborted(
                            "the delegated agent read content that failed the "
                            "safety screen, so it was stopped before answering",
                            flagged=excerpt,
                        )
                    shadow_hits.append(excerpt)
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(getattr(call, "call_id", "") or ""),
                        "output": _render_child_output(result, offset=len(evidence)),
                    }
                )
                evidence.extend(result.evidence)
        raise ChildAborted(
            "the delegated agent ran out of iterations without answering"
        )
    except ChildAborted as exc:
        # An abort still owes the record. A hit that stopped the child (enforce)
        # rides `exc.flagged`; hits seen earlier under shadow are in
        # `shadow_hits`. Both must reach the parent's serial screen, and an
        # abort caught before the loop even started leaves both empty, which is
        # correct — there is nothing to record.
        hits = list(shadow_hits)
        if exc.flagged:
            hits.append(exc.flagged)
        return ToolResult(content=_notice_first(f"Error: {exc}.", hits))


def _notice_first(body: str, shadow_hits: List[str]) -> str:
    """Put the safety-screen notice at the FRONT of a delegate result.

    The parent re-screens only `result.content` (delegate results carry no
    evidence), and `_combined_attempts` clips each attempt to a budget — so a
    flagged excerpt placed after a long answer can be truncated away before the
    parent's serial screen ever re-classifies it, losing the shadow record and,
    in enforce, the ASK_ALL escalation. Leading with the notice means the one
    thing that must survive clipping is the one thing clipping keeps.
    """
    if not shadow_hits:
        return body
    notice = (
        "Safety screen notice — these passages the sub-agent read were "
        "flagged:\n" + "\n---\n".join(hit for hit in shadow_hits[:3])
    )
    return f"{notice}\n\n{body}"


def _execute_child_call(
    db: Session,
    context: ToolContext,
    registry: Dict[str, ToolSpec],
    *,
    name: str,
    raw_arguments: str,
) -> ToolResult:
    """One child tool call. Mirrors `execute_agent_tool_call`'s error surface —
    bugs become model-visible strings — without its bookkeeping, which belongs
    to the delegate call as a whole."""
    spec = registry.get(name)
    if spec is None:
        return ToolResult(content=f"Error: unknown tool “{name}”.")
    try:
        arguments = json.loads(raw_arguments) if raw_arguments else {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
    except (ValueError, TypeError):
        return ToolResult(content="Error: tool arguments were not valid JSON.")
    try:
        return spec.executor(db, context, arguments)
    except Exception as exc:
        db.rollback()
        return ToolResult(content=f"Error: tool failed: {str(exc)[:300]}")


def _answer_result(
    answer: str,
    evidence: List[Evidence],
    agent: Agent,
    shadow_hits: Optional[List[str]] = None,
) -> ToolResult:
    """The child's findings, with its sources quoted inline rather than
    renumbered into the parent's evidence list (see module docstring)."""
    parts = [f"Sub-agent “{agent.name}” answered:\n\n{answer}"]
    if evidence:
        quoted = "\n".join(
            f"- [{index}] {item.filename}, passage {item.ordinal + 1}: "
            f"{item.excerpt[:300]}"
            for index, item in enumerate(evidence, start=1)
        )
        parts.append(f"Passages the sub-agent read (its [n] markers):\n{quoted}")
    # Shadow-mode hits lead the content (see `_notice_first`): the parent's
    # serial screen re-classifies `result.content`, and best-of-N clipping
    # keeps the front, so the record survives both.
    return ToolResult(content=_notice_first("\n\n".join(parts), list(shadow_hits or [])))


#: Best-of-N ceiling — same bound as the batch pool, for the same reason: it
#: caps threads and sessions, not correctness.
MAX_ATTEMPTS = 4


def _attempt_prompt(prompt: str, index: int, total: int) -> str:
    if total <= 1:
        return prompt
    return (
        f"{prompt}\n\n(This is attempt {index} of {total} running in "
        "parallel; take a genuinely distinct approach from the other attempts.)"
    )


def _combined_attempts(results: List[ToolResult], agent: Agent) -> ToolResult:
    """All attempts, labelled, for the PARENT model to judge.

    Deliberately no separate judge model: the delegating agent already holds
    the conversation's full context, which is exactly what picking the best
    answer needs — a judge with less context than the caller is a worse judge.
    Each attempt is clipped so N attempts share the result budget instead of
    the last ones falling off the end of `bounded_content`.
    """
    per_attempt = max(800, 3600 // max(1, len(results)))
    sections = [
        f"=== Attempt {index} ===\n{(result.content or '(empty)')[:per_attempt]}"
        for index, result in enumerate(results, start=1)
    ]
    return ToolResult(
        content=(
            f"Sub-agent “{agent.name}” ran {len(results)} parallel attempts. "
            "Judge them yourself, use the strongest (or combine them), and say "
            "which you relied on:\n\n" + "\n\n".join(sections)
        )
    )


def _delegate(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    agent_name = str(args.get("agent") or "").strip()
    prompt = str(args.get("prompt") or "").strip()[:_PROMPT_LIMIT]
    if not agent_name or not prompt:
        return ToolResult(content="Error: both `agent` and `prompt` are required.")
    try:
        attempts = int(args.get("attempts") or 1)
    except (TypeError, ValueError):
        attempts = 1
    attempts = min(max(attempts, 1), MAX_ATTEMPTS)
    agent = db.scalar(
        select(Agent).where(
            Agent.workspace_id == context.workspace_id,
            Agent.name == agent_name,
            Agent.enabled.is_(True),
        )
    )
    if agent is None:
        return ToolResult(
            content=(
                f"Error: no enabled agent named “{agent_name}” in this "
                "workspace. Call delegate with one of the agent names from the "
                "tool description."
            )
        )
    if attempts == 1:
        return run_child_agent(db, context, agent=agent, prompt=prompt)

    # Best-of-N: the children fan out exactly like a parallel batch of
    # delegate calls — each worker gets its own session (`db` is not
    # thread-safe) and a `contextvars` copy so its model calls bill to the
    # turn. Failures degrade per attempt: one blown attempt is an error
    # SECTION the parent reads past, never a lost fan-out.
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    from ..database import SessionLocal

    agent_id = agent.id

    def _attempt(index: int) -> ToolResult:
        session = SessionLocal()
        try:
            child_agent = session.get(Agent, agent_id)
            if child_agent is None:
                return ToolResult(content="Error: the agent was retired mid-call.")
            return run_child_agent(
                session,
                context,
                agent=child_agent,
                prompt=_attempt_prompt(prompt, index, attempts),
            )
        except Exception as exc:
            session.rollback()
            return ToolResult(content=f"Error: attempt failed: {str(exc)[:300]}")
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        futures = [
            pool.submit(contextvars.copy_context().run, _attempt, index)
            for index in range(1, attempts + 1)
        ]
        results = [future.result() for future in futures]
    return _combined_attempts(results, agent)


def _preview_delegate(db: Session, context: ToolContext, args: Dict[str, Any]) -> str:
    agent_name = str(args.get("agent") or "").strip()
    prompt = str(args.get("prompt") or "").strip()
    return f"Ask sub-agent “{agent_name}” (read-only) to:\n\n{prompt[:2000]}"


def delegation_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """The delegation family: one tool, described with the live agent roster so
    the model can name a target without a listing call first (the same
    human-addressing rule `edit_document` follows with titles)."""
    names = list(
        db.scalars(
            select(Agent.name)
            .where(
                Agent.workspace_id == context.workspace_id,
                Agent.enabled.is_(True),
            )
            .order_by(Agent.created_at, Agent.id)
        )
    )
    roster = ", ".join(f"“{name}”" for name in names) or "(none configured)"
    return {
        DELEGATE_TOOL: ToolSpec(
            name=DELEGATE_TOOL,
            description=(
                "Delegate a focused research question to another agent in this "
                "workspace. The sub-agent runs with read-only tools only and "
                "returns its findings as text; it cannot change anything. Use "
                "it to investigate independent sub-questions — several "
                "delegate calls made together run in parallel. For a single "
                "hard question, set `attempts` to run best-of-N parallel "
                "tries and judge the answers yourself. Available "
                f"agents: {roster}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "The name of the agent to delegate to.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": (
                            "The self-contained question or task. The "
                            "sub-agent sees nothing of this conversation, so "
                            "include the context it needs."
                        ),
                    },
                    "attempts": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 4,
                        "description": (
                            "Run the same task this many times in parallel "
                            "(best-of-N) and receive every labelled answer "
                            "to judge. Default 1."
                        ),
                    },
                },
                "required": ["agent", "prompt"],
            },
            executor=_delegate,
            # Literally true — the child registry is read-only by construction —
            # and load-bearing: it is what lets delegate calls run unattended
            # under ask_writes and survive plan-mode narrowing (research is
            # what plan mode is for).
            read_only=True,
            preview=_preview_delegate,
        )
    }
