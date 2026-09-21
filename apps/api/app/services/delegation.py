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
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Agent, Run
from . import budget, grounded, provenance, screen
from .audit import record_audit
from .citations import grade_grounding, split_sentences, validate_citations
from .harness import ModelStep, resolve_harness
from .llm_tools import (
    FROZEN_WITHHELD,
    MAX_RESULT_CHARS,
    ToolContext,
    ToolResult,
    ToolSpec,
    build_registry,
)

# Module-level names on purpose: the council's "retrieve exactly once" property
# is only testable if a test can count the calls, and it can only count them if
# they go through a name it can replace.
from .retrieval import Evidence, budget_for, search_evidence

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
    workspace_id: str = "",
    run_id: str = "",
) -> ModelStep:
    """The model behind one child turn. A module-level seam, like
    `agent_loop._default_model_step`, so tests can replace the model without
    replacing the loop around it.

    A child carries the PARENT's workspace and run, which is the truth: it
    bills to the parent's usage scope and runs inside the parent's turn. That
    also puts it on the parent's prompt cache shard, where its instructions and
    tool payload — drawn from the same workspace's agents — actually share a
    prefix with the parent's.
    """
    return resolve_harness(settings).build_step(
        settings,
        prompt=prompt,
        user_id=user_id,
        evidence=[],
        model=model,
        effort=effort,
        workspace_id=workspace_id,
        run_id=run_id,
    )


def _child_registry(
    db: Session, context: ToolContext, agent: Agent, *, run: Optional[Run] = None
) -> Dict[str, ToolSpec]:
    """What a child may see: the agent's provisioned subset, read-only half,
    narrowed by the turn's preset.

    `force_ask` tools are excluded even though they are read-only, because
    their authors demanded a human look at every call — a child has no way to
    ask one. `delegate` and `ask_user` are excluded by the same rule that
    excludes writes: a child that could recurse or park would re-import the
    complexity this narrowing exists to remove.

    `run` carries the preset, and the preset has to reach here for `Run.preset`
    to mean anything: `deep-research` pins `families={core, graph, delegation,
    memory}` — `web` deliberately absent, `delegation` deliberately present —
    so without this the parent has no `web_fetch` and its child does, and the
    run row records a tool policy the turn did not run under. `None` keeps the
    old behaviour for a call with no run in hand.
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
    if run is not None and run.preset:
        # Imported here rather than at module scope: run_presets imports
        # llm_tools, which this module also imports, and the cycle only stays
        # broken if the import stays local (the `from .agent_loop import ...`
        # pattern below, for the same reason).
        from . import run_presets, subjects

        allowed = subjects.narrow(
            allowed, run_presets.allowed_tools_for_preset(db, context, run.preset)
        )
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
    frozen_evidence: Optional[List[Evidence]] = None,
) -> ToolResult:
    """One delegated turn: a bounded, read-only, non-parking agent loop.

    `db` may be a worker thread's own session — everything here must stay on
    it. Raises nothing: every failure becomes an error `ToolResult` the parent
    model can read and route around.

    `frozen_evidence`, when non-empty, makes this a COUNCIL child: the parent
    retrieved once for every candidate, spliced the numbered block into this
    child's prompt, and withholds `search_sources` here. Seeding the child's
    own evidence list with those passages is what keeps its ``[n]`` markers
    pointing at the block it was given — a child that numbered from 1 against
    an empty list would cite passage 1 meaning something else entirely.
    """
    settings = settings or get_settings()
    run = db.get(Run, context.run_id) if context.run_id else None
    if run is None or run.workspace_id != context.workspace_id:
        return ToolResult(content="Error: delegation requires a live run.")

    from .agent_loop import _serialize_item, policy_scope_for_run

    scope_unattended = policy_scope_for_run(db, run) == "workflow"
    registry = _child_registry(db, context, agent, run=run)
    if frozen_evidence:
        # What makes "frozen" true rather than aspirational: candidates that
        # read different passages are not candidates, they are separate
        # answers. EVERY retrieval tool goes, not just `search_sources` —
        # `grounded_answer` runs its own search and `list_sources` hands the
        # child ids to aim one with, and both are read-only, so both survived
        # the old one-name filter. Withheld HERE rather than inside
        # `_child_registry` so that function stays about the agent's own
        # provisioned subset — and the removal is conditional on a NON-EMPTY
        # list, so an empty corpus degrades to today's independent attempts
        # instead of to N children with no way to learn anything at all.
        registry = {
            name: spec
            for name, spec in registry.items()
            if name not in FROZEN_WITHHELD
        }
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
        workspace_id=run.workspace_id,
        run_id=run.id,
    )
    input_items: List[Any] = [{"role": "user", "content": prompt}]
    # Pre-seeded for a council child (see the docstring): the frozen block is
    # already [1..n] in its prompt, so anything it retrieves — it cannot —
    # or is quoted back would number after it.
    evidence: List[Evidence] = list(frozen_evidence or [])
    #: Passages the screen flagged in shadow mode. Shadow means "record, never
    #: enforce", and the record is made by handing the excerpts back to the
    #: parent's serial screening — dropping them here would make shadow mode
    #: blind to exactly the source class it exists to measure.
    shadow_hits: List[str] = []
    #: Provenance classes this child actually pulled in — every executed tool's
    #: own class, plus the class of every evidence item it ends up quoting. A
    #: child writes NO run events by construction (delegation.py:126-130), so
    #: this set is the parent's only way to learn that its sub-agent read a web
    #: page or an MCP result. It rides home on `ToolResult.provenance`; without
    #: it, delegation is a hole straight through the taint gate.
    classes: set[str] = {provenance.classify_evidence(item) for item in evidence}

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
                return _answer_result(
                    answer, evidence, agent, shadow_hits, sorted(classes)
                )
            input_items.extend(_serialize_item(item) for item in response.output)
            for call in calls:
                name = str(getattr(call, "name", "") or "")
                raw_arguments = getattr(call, "arguments", "") or "{}"
                result = _execute_child_call(
                    db, context, registry, name=name, raw_arguments=str(raw_arguments)
                )
                child_spec = registry.get(name)
                if child_spec is not None:
                    classes.add(child_spec.provenance)
                classes.update(result.provenance)
                classes.update(
                    provenance.classify_evidence(item) for item in result.evidence
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
                if not frozen_evidence:
                    evidence.extend(result.evidence)
                # Belt and braces behind the withheld registry above: with a
                # frozen block, the child's [n] space IS the block, and a tool
                # that somehow returned passages must not be able to extend it.
                # `classes` above still records what it read, so the taint gate
                # is unaffected by the numbering staying pinned.
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
        # An abort still reports what it read. A child stopped mid-way has
        # already folded untrusted content into its own transcript, and the
        # parent's gate has to hear about it whether or not an answer came back.
        return ToolResult(
            content=_notice_first(f"Error: {exc}.", hits),
            provenance=sorted(classes),
        )


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
    classes: Optional[List[str]] = None,
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
    return ToolResult(
        content=_notice_first("\n\n".join(parts), list(shadow_hits or [])),
        provenance=list(classes or []),
        # The answer on its own, for anything that SCORES this result. The
        # quoted-passage block below it is the child's sources, not its claims;
        # grading it measures the quoting. See `ToolResult.answer_text`.
        answer_text=answer,
    )


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


#: Prepended to the frozen block in every council child's prompt. The block is
#: the child's whole world for this question — it has no `search_sources` — so
#: the instruction has to say what to do about a gap in it, or the model fills
#: the gap from memory and the council compares two answers and one invention.
COUNCIL_EVIDENCE_HEADER = (
    "These numbered passages are the ONLY evidence available to you. Cite them "
    "as [n]. Do not assert anything they do not support; say so instead.\n\n"
)

#: Below this a candidate is ordered last and labelled. A floor, not a filter:
#: the parent still sees every candidate, because the judge with the full
#: conversation is better placed than this number to decide what an
#: unsupported-heavy answer was right about.
DEMOTION_FLOOR = 0.5

#: The council's retrieval budget. Deliberately the widest one: it is retrieved
#: ONCE for N children, so the per-candidate cost of breadth is 1/N of a normal
#: turn's, and a council is asked for exactly when the question is hard.
COUNCIL_BUDGET = "high"


def _evidence_block(evidence: Sequence[Evidence]) -> str:
    """The frozen passages, numbered exactly as `_render_child_output` numbers
    a tool result's — so a council child's citation habit is the same habit it
    has in every other turn."""
    return "\n\n".join(
        f"[{index}] {item.filename}, passage {item.ordinal + 1}\n{item.excerpt}"
        for index, item in enumerate(evidence, start=1)
    )


def _citation_proxy(answer: str, evidence: Sequence[Evidence]) -> float:
    """COVERAGE, not entailment: the share of sentences carrying a usable [n].

    The deterministic fallback for `_grounding_score`. It says how much of an
    answer bothered to cite, and nothing whatsoever about whether the citation
    supports the sentence — which is why it is the fallback and not the
    measure. An answer with a fabricated or malformed marker scores 0: a
    citation that points nowhere is worse evidence of grounding than none.
    """
    if not evidence or not answer.strip():
        return 0.0
    report = validate_citations(answer, evidence)
    if report.out_of_range or report.malformed:
        return 0.0
    spans = split_sentences(answer)
    if not spans:
        return 0.0
    cited = 0
    for start, end, _text in spans:
        if any(
            not marker.malformed
            and marker.start >= start
            and marker.end <= end
            and any(1 <= number <= len(evidence) for number in marker.numbers)
            for marker in report.markers
        ):
            cited += 1
    return cited / len(spans)


def _grounding_score(
    answer: str, evidence: Sequence[Evidence], *, floor: float
) -> float:
    """How much of a candidate its own frozen passages support, 0..1.

    `citations.grade_grounding` is the real measure — a lexical support test,
    sentence by sentence, against the passages each sentence named. It is used
    whenever it has anything to grade.

    DIVERGENCE FROM THE SPEC, on purpose: the spec named an adapter over a
    `services/grounding.py::score_answer` that does not exist. What landed is
    `citations.grade_grounding(answer, passages, floor=...) -> GroundingReport`,
    and calling the thing that exists beats importing the thing that was
    planned. The fallback below survives for the case the report itself calls
    out — `scored == 0` means "no claim here to check", which is not a score of
    zero — and for any failure at all, because a council must not be able to
    fail on the way to ranking its own candidates.

    `floor` is the DEPLOYMENT's `grounding_floor`, passed rather than
    defaulted. Defaulting was invisible only because the library default and
    the config default are both 0.6 today: raise GROUNDING_FLOOR to 0.85 and
    identical text scores 1.00 here and 0.00 on every other surface, and the
    stored `council.scored` event records the number without the scale.
    """
    if not evidence or not answer.strip():
        return 0.0
    try:
        report = grade_grounding(
            answer,
            [item.excerpt for item in evidence],
            floor=floor,
            checkable=grounded.checkable_flags(evidence),
        )
        if report.scored:
            return float(report.score)
    except Exception:  # noqa: BLE001 - scoring must never fail the council
        pass
    return _citation_proxy(answer, evidence)


def _convergence(cited: Sequence[Tuple[int, Tuple[int, ...]]], total: int) -> str:
    """Which passages the candidates agreed on, contested, or found alone.

    Pure set arithmetic over what each candidate cited — no model call, so the
    parent's synthesis headings are grounded in the evidence rather than in the
    judge's impression of consensus. `unique` names the candidate, because "one
    of them found this" is only useful if you can go read that one.
    """
    if not cited:
        return ""
    sets = [set(numbers) for _index, numbers in cited]
    everything = sorted(set().union(*sets)) if sets else []
    unanimous = sorted(set.intersection(*sets)) if sets else []
    unique: List[str] = []
    for number in everything:
        holders = [index for index, numbers in cited if number in set(numbers)]
        if len(holders) == 1:
            unique.append(f"[{number}] (candidate {holders[0]})")
    contested = [
        f"[{number}]"
        for number in everything
        if number not in unanimous
        and not any(part.startswith(f"[{number}] ") for part in unique)
    ]
    lines = [
        f"Citation convergence across {total} candidates:",
        "- Cited by all: "
        + (", ".join(f"[{number}]" for number in unanimous) or "none"),
        "- Cited by some: " + (", ".join(contested) or "none"),
        "- Cited by one: " + (", ".join(unique) or "none"),
    ]
    return "\n".join(lines)


@dataclass(frozen=True)
class Candidate:
    """One council candidate, scored. Computed once and read by both the result
    the parent sees and the event a triager reads later — two numbers that
    could disagree would be two different stories about the same council."""

    index: int
    result: ToolResult
    grounding: float
    cited: Tuple[int, ...]
    demoted: bool


def _scored_text(result: ToolResult) -> str:
    """What a candidate actually CLAIMED, for grading and citation extraction.

    `_answer_result` wraps a child's answer in a header plus one quoted line
    per passage it read ("- [3] handbook.md, passage 4: …"). Those lines are
    the child's SOURCES, and grading them measures the quoting: `split_sentences`
    breaks on every newline, so each quoted passage becomes a graded sentence
    citing its own [n] and stating the numeral "passage N" that its excerpt
    does not contain — CITED_UNSUPPORTED, every time, for every candidate. With
    the council's own `high` budget (ten passages) a perfectly grounded answer
    envelope scores about 0.21, below `DEMOTION_FLOOR`, so demotion never fires
    and `validate_citations` reports every frozen marker for every candidate,
    which is why the convergence table read "Cited by all" unanimously whatever
    the candidates said.
    """
    return result.answer_text or result.content or ""


def _score_candidates(
    results: List[ToolResult], frozen: List[Evidence], *, floor: float
) -> List[Candidate]:
    """Every candidate's grounding score, citations and demotion, deterministic.

    Never hand the judge an empty council: when EVERY candidate is below the
    floor, none is demoted. Labelling the whole field "the bad ones" tells the
    parent nothing it can act on, and the scores are shown either way.
    """
    scores = [
        (index, result, _grounding_score(_scored_text(result), frozen, floor=floor))
        for index, result in enumerate(results, start=1)
    ]
    demote_any = any(score >= DEMOTION_FLOOR for _index, _result, score in scores)
    return [
        Candidate(
            index=index,
            result=result,
            grounding=score,
            cited=validate_citations(_scored_text(result), frozen).cited,
            demoted=demote_any and score < DEMOTION_FLOOR,
        )
        for index, result, score in scores
    ]


def _council_result(
    results: List[ToolResult],
    agent: Agent,
    frozen: List[Evidence],
    *,
    floor: float,
) -> ToolResult:
    """The council's candidates, scored, ordered and tabulated for the parent.

    Everything here is deterministic. The scores order the sections and label
    the weak ones; the convergence table says what the candidates actually
    agreed on; the parent — which holds the conversation, and is therefore the
    better judge — does the judging under headings the table can support.
    """
    candidates = _score_candidates(results, frozen, floor=floor)
    ordered = sorted(
        candidates,
        key=lambda row: (row.demoted, -row.grounding, row.index),
    )
    cited = [(candidate.index, candidate.cited) for candidate in candidates]
    header = (
        f"Council: sub-agent “{agent.name}” ran {len(results)} candidates over "
        f"the same {len(frozen)} frozen passages."
    )
    directive = (
        "Write your answer under exactly three headings — Agreed, Disagreed, "
        "Unique findings — and say which candidates you relied on. Grounding "
        "scores are lexical support against the frozen passages, not a verdict "
        "on which answer is right."
    )
    table = _convergence(cited, len(results))
    labels: List[str] = []
    for candidate in ordered:
        label = f"=== Candidate {candidate.index} (grounding {candidate.grounding:.2f}"
        if candidate.demoted:
            label += " — demoted, unsupported-heavy"
        labels.append(label + ") ===")
    fixed = [part for part in (header, table, directive) if part]
    # The candidates share what the framing leaves, rather than a guessed
    # constant: header, table, labels and separators are all known here, so the
    # budget below is arithmetic and the assert at the end cannot fire.
    overhead = sum(len(part) + 2 for part in (*fixed, *labels)) + len(labels)
    per_attempt = max(300, (MAX_RESULT_CHARS - overhead) // max(1, len(results)))
    sections = [
        f"{label}\n{(candidate.result.content or '(empty)')[:per_attempt]}"
        for label, candidate in zip(labels, ordered, strict=True)
    ]
    parts = [header]
    if table:
        parts.append(table)
    parts.extend(sections)
    parts.append(directive)
    content = "\n\n".join(parts)
    # The payload-must-fit invariant: every section above is clipped by
    # `per_attempt`, so an overflow here would be a bug in that bound.
    assert len(content) <= MAX_RESULT_CHARS, "council result exceeded the tool budget"
    return ToolResult(content=content, provenance=_merged_provenance(results))


def _merged_provenance(results: List[ToolResult]) -> List[str]:
    """Every class the attempts between them pulled in.

    A union, never a vote: one of four council children reading a web page is
    enough to taint the turn, and an aggregate that reported only what the
    majority read would launder exactly the attempt worth gating on.
    """
    merged: set[str] = set()
    for result in results:
        merged.update(result.provenance)
    return sorted(merged)


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
        ),
        provenance=_merged_provenance(results),
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

    # THE COUNCIL. Everything down to the fan-out happens here, in the parent
    # thread on the parent session, and the order is the design:
    #
    # 1. Retrieve ONCE. Candidates that read different passages are not
    #    candidates, they are separate answers, and comparing them says
    #    nothing. One retrieval for N children is also why the widest budget
    #    is affordable here.
    # 2. Screen the block ONCE, serially, BEFORE it is multiplied. A frozen
    #    block is spliced into N prompts in N parallel threads that cannot
    #    write the flag themselves — classify it while it is still one string
    #    and one writer, never after the fan-out for latency.
    settings = get_settings()
    council_budget = budget_for(COUNCIL_BUDGET)
    frozen: List[Evidence] = list(
        search_evidence(
            db,
            workspace_id=context.workspace_id,
            query=prompt,
            space_id=context.space_id,
            conversation_id=context.conversation_id,
            limit=council_budget.limit,
            token_budget=council_budget.token_budget,
            per_passage_tokens=council_budget.per_passage_tokens,
        )
    )
    shared_hits: List[str] = []
    if frozen:
        flagged = _screen_excerpt(
            "\n\n".join(item.excerpt for item in frozen), settings
        )
        if flagged is not None:
            if settings.screen_mode == "enforce":
                return ToolResult(
                    content=_notice_first(
                        "Error: the council could not run: its shared evidence "
                        "failed the safety screen.",
                        [flagged],
                    )
                )
            shared_hits.append(flagged)
    # With an empty corpus there is nothing to freeze, so the children keep
    # their own retrieval and this degrades to today's independent attempts.
    block = (
        f"{COUNCIL_EVIDENCE_HEADER}{_evidence_block(frozen)}" if frozen else ""
    )

    # The children fan out exactly like a parallel batch of delegate calls —
    # each worker gets its own session (`db` is not thread-safe) and a
    # `contextvars` copy so its model calls bill to the turn. Failures degrade
    # per attempt: one blown attempt is an error SECTION the parent reads past,
    # never a lost fan-out.
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
            attempt_prompt = _attempt_prompt(prompt, index, attempts)
            return run_child_agent(
                session,
                context,
                agent=child_agent,
                prompt=f"{attempt_prompt}\n\n{block}" if block else attempt_prompt,
                frozen_evidence=list(frozen),
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
    if not frozen:
        return _combined_attempts(results, agent)
    result = _council_result(results, agent, frozen, floor=settings.grounding_floor)
    # COMPUTED here, WRITTEN by the coordinator. `_delegate` runs on a worker
    # thread whenever the model issues several delegate calls in one round
    # (`agent_loop._delegate_parallel_batch`), and that function's stated
    # invariant is that workers write no events — two councils in one round
    # otherwise race `run_events`' unique (run_id, sequence). The record rides
    # home on the result, exactly as a screen hit and a child's provenance do.
    result.deferred_events.extend(
        _council_events(
            agent=agent,
            results=results,
            frozen=frozen,
            floor=settings.grounding_floor,
        )
    )
    if shared_hits:
        # Shadow-mode hits ride the parent's own serial screen of this content,
        # exactly as a child's do (see `_notice_first`). The rebuild carries
        # `provenance` forward: dropping it here would silently untaint a
        # council whose evidence the screen had just flagged.
        return ToolResult(
            content=_notice_first(result.content, shared_hits),
            provenance=result.provenance,
            deferred_events=result.deferred_events,
        )
    return result


def _council_events(
    *,
    agent: Agent,
    results: List[ToolResult],
    frozen: List[Evidence],
    floor: float,
) -> List[Dict[str, Any]]:
    """The `council.scored` record, as data for someone else to append.

    NOT written here, and the distinction is the whole point. `_delegate` runs
    on a ThreadPoolExecutor worker whenever the model issues several delegate
    calls in one round, and `agent_loop._delegate_parallel_batch` states the
    rule that makes that safe: "`run_events` is unique on (run_id, sequence),
    so worker threads write NO events — every row and event is written serially
    on the parent session." Appending from here put two councils in one round
    in a race for the next sequence, which `append_event`'s retry hid as
    contention rather than as the broken invariant it was. The event rides home
    on `ToolResult.deferred_events` and the coordinator writes it beside
    `tool.completed`, in queue order.

    Pure, so it cannot fail the turn: the council already has its answer, and a
    triage record that could swallow one would be a bad trade.
    """
    return [
        {
            "event_type": "council.scored",
            "payload": {
                "agent": agent.name,
                "frozen_chunk_ids": [item.chunk_id for item in frozen],
                # The scale the scores were measured at, beside them, for the
                # same reason `GroundingReport.floor` exists: a stored verdict
                # that does not say what it was measured at cannot be compared
                # with one measured later under a different setting.
                "floor": floor,
                "candidates": [
                    {
                        "index": candidate.index,
                        "grounding": round(candidate.grounding, 4),
                        "demoted": candidate.demoted,
                        "cited": list(candidate.cited),
                    }
                    for candidate in _score_candidates(results, frozen, floor=floor)
                ],
            },
        }
    ]


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
                "hard question, set `attempts` to convene a council: the "
                "attempts all read one shared, frozen set of passages and come "
                "back with grounding scores and a citation-convergence table "
                "for you to judge. Available "
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
                            "Convene a council of this many parallel attempts "
                            "over one frozen evidence set, and receive every "
                            "labelled answer with its grounding score. "
                            "Default 1 (a single ordinary delegation)."
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
            # Explicit, and the DEFAULT is the deliberate answer: a child's
            # summary is a tool's output. What the child actually *read* rides
            # home on `ToolResult.provenance` instead, because a spec cannot
            # know that in advance — it depends on which tools the child chose.
            provenance=provenance.TOOL_RESULT,
        )
    }
