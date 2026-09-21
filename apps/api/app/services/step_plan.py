"""Plan-then-execute: an opt-in turn mode, spliced in beside plan mode.

MODE MACHINERY, not a registry family — modelled exactly on
`agent_loop.PLAN_MODE_INSTRUCTIONS` + `_plan_narrowed` + `exit_plan_mode_spec`.
The two tools are added at narrowing time rather than shipped by
`registry_families`, for that machinery's own two reasons: an agent's
provisioned subset must not be able to strip them (a plan mode with no way to
submit a plan is a locked room), and no other mode should ever be offered them.

WHY A DIRECTIVE BLOCK AND STRUCTURED TOOLS, and not one delegate per
sub-question — the alternative the brief also allows:

* A delegated child sees nothing of the conversation (`delegation.py`'s own
  docstring), so every step would have to re-derive context the parent already
  holds.
* A child writes NO run events, deliberately: the module's `UniqueConstraint(
  run_id, sequence)` argument says children must not race the parent's stream.
  A plan whose steps left no trace could not be rendered live, resumed, or
  triaged afterwards.
* A child gets four iterations. A step that needs five is simply lost.

None of those three can carry a visible, resumable, per-step-logged plan.
Delegation stays available to the model INSIDE a step and is unchanged.

With the flag off, `narrowed()` returns its arguments unchanged and
`iteration_budget()` returns `MAX_ITERATIONS`: a default turn's registry,
instructions and arithmetic are byte-identical to what they were before this
module existed.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Run, RunEvent
from .events import append_event
from .llm_tools import MAX_RESULT_CHARS, ToolContext, ToolResult, ToolSpec

logger = logging.getLogger(__name__)

SUBMIT_PLAN = "submit_plan"
COMPLETE_STEP = "complete_step"

#: A plan is a shape for ONE turn, not a project schedule. Five steps at four
#: queries each is already twenty retrievals inside a twelve-iteration budget;
#: past that the model is describing work it cannot finish here.
MAX_STEPS = 5
MAX_QUERIES_PER_STEP = 4
MAX_QUESTION_CHARS = 300
MAX_QUERY_CHARS = 200
#: What the next step sees of this one. A summary is a handoff, not a transcript
#: — the passages themselves are already in the turn's evidence.
MAX_SUMMARY_CHARS = 1200
#: Ids one step may say it leaned on, for later triage.
MAX_STEP_CHUNK_IDS = 20

#: Both fit `RunEvent.event_type`'s VARCHAR(40), which is the constraint that
#: decides event names in this codebase.
PLAN_SUBMITTED = "plan.submitted"
PLAN_STEP = "plan.step"

#: A step-plan turn's iteration ceiling. Double the default because a plan of
#: five steps spends one round submitting, one searching and one completing per
#: step, and then needs a round to answer in. Named HERE, next to the reason,
#: rather than raised globally: this mode's worst-case spend is twice a normal
#: turn's, and it is reachable only from an explicit toggle or a preset the
#: user picked. The per-iteration `budget.evaluate` ceiling still bounds it.
STEP_PLAN_MAX_ITERATIONS = 12

#: Short on purpose. The measured fact behind this mode is that planner prompts
#: work better brief: a long block spends the attention the plan itself needs.
STEP_PLAN_INSTRUCTIONS = (
    "This turn runs in plan-then-execute mode. Before anything else, call "
    "`submit_plan` with at most 5 steps, each a single question and at most 4 "
    "retrieval queries that would answer it. Then work the steps in order: "
    "issue that step's queries as one batch of `search_sources` calls, read "
    "what comes back, and call `complete_step` with a short summary of what "
    "you learned. That summary is all the next step is told, so put the "
    "findings in it, not the process. Write the final answer only after the "
    "last `complete_step`, citing supplied passages with [n]."
)


@dataclass(frozen=True)
class PlanStep:
    index: int
    question: str
    queries: Tuple[str, ...]


@dataclass(frozen=True)
class PlanState:
    """What this turn's plan is, rebuilt from its run events.

    Derived state, with no column and no `LoopState` field of its own, so it
    survives a park and a resume in another process for free — the events are
    already the durable record, and a second copy of the truth is a second
    thing to get out of step.
    """

    steps: Tuple[PlanStep, ...] = ()
    completed: int = 0

    @property
    def submitted(self) -> bool:
        return bool(self.steps)

    @property
    def next_index(self) -> int:
        """The 1-based step to work next; 0 when every step is done."""
        if not self.steps or self.completed >= len(self.steps):
            return 0
        return self.completed + 1


def state_for(db: Session, workspace_id: str, run_id: str) -> PlanState:
    """This run's plan, read back from `plan.submitted` / `plan.step` alone."""
    if not run_id:
        return PlanState()
    rows = db.execute(
        select(RunEvent.event_type, RunEvent.payload_json)
        .where(
            RunEvent.workspace_id == workspace_id,
            RunEvent.run_id == run_id,
            RunEvent.event_type.in_((PLAN_SUBMITTED, PLAN_STEP)),
        )
        .order_by(RunEvent.sequence)
    ).all()
    steps: Tuple[PlanStep, ...] = ()
    completed = 0
    for event_type, payload_json in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except ValueError:
            continue
        if event_type == PLAN_SUBMITTED and not steps:
            steps = tuple(
                PlanStep(
                    index=int(item.get("index") or 0),
                    question=str(item.get("question") or ""),
                    queries=tuple(str(query) for query in item.get("queries") or ()),
                )
                for item in payload.get("steps") or ()
            )
        elif event_type == PLAN_STEP:
            completed += 1
    return PlanState(steps=steps, completed=min(completed, len(steps)))


@dataclass(frozen=True)
class CompletedStep:
    """One step the turn actually closed, as `complete_step` recorded it."""

    index: int
    question: str
    summary: str
    #: The passages the model said this step leaned on. Unresolved ids — the
    #: reader checks them against something it trusts.
    chunk_ids: Tuple[str, ...]


def completed_steps(
    db: Session, workspace_id: str, run_id: str
) -> List[CompletedStep]:
    """Every `plan.step` this run wrote, in order.

    A public reader beside `state_for`, which answers "how far along is this
    turn" and deliberately keeps no payload. The coverage ledger
    (`runs._record_coverage`) needs the other half: which sub-question each
    step posed and which passages it said answered it. Reading the events here
    rather than re-parsing them in runs.py keeps the payload shape this
    module writes owned by this module.
    """
    if not run_id:
        return []
    rows = db.execute(
        select(RunEvent.payload_json)
        .where(
            RunEvent.workspace_id == workspace_id,
            RunEvent.run_id == run_id,
            RunEvent.event_type == PLAN_STEP,
        )
        .order_by(RunEvent.sequence)
    ).all()
    steps: List[CompletedStep] = []
    for (payload_json,) in rows:
        try:
            payload = json.loads(payload_json or "{}")
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        steps.append(
            CompletedStep(
                index=int(payload.get("index") or 0),
                question=str(payload.get("question") or ""),
                summary=str(payload.get("summary") or ""),
                chunk_ids=tuple(
                    str(value) for value in payload.get("chunk_ids") or () if str(value)
                ),
            )
        )
    return steps


def _next_instruction(state: PlanState) -> Dict[str, Any]:
    """What to tell the model after a plan or a step landed.

    One shape for both executors, so "what happens next" is written down once.
    """
    index = state.next_index
    if not index:
        return {
            "status": "complete",
            "instruction": (
                "All steps are complete. Write the final answer now, citing "
                "supplied passages with [n]."
            ),
        }
    step = state.steps[index - 1]
    return {
        "next_step": index,
        "of": len(state.steps),
        "question": step.question,
        "queries": list(step.queries),
        "instruction": (
            "Run these queries with search_sources, then call complete_step "
            f"with step={index} and a short summary of what you found."
        ),
    }


def _parse_steps(raw: Any) -> Tuple[List[PlanStep], str]:
    """A submitted plan's shape, or a sentence saying what is wrong with it."""
    if not isinstance(raw, list) or not raw:
        return [], "Error: `steps` must be a list of 1 to 5 step objects."
    if len(raw) > MAX_STEPS:
        return [], f"Error: a plan may have at most {MAX_STEPS} steps."
    steps: List[PlanStep] = []
    for position, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            return [], f"Error: step {position} must be an object with a question and queries."
        question = str(item.get("question") or "").strip()
        if not question:
            return [], f"Error: step {position} needs a non-empty `question`."
        if len(question) > MAX_QUESTION_CHARS:
            return [], (
                f"Error: step {position}'s question is longer than "
                f"{MAX_QUESTION_CHARS} characters."
            )
        raw_queries = item.get("queries")
        if not isinstance(raw_queries, list) or not raw_queries:
            return [], f"Error: step {position} needs 1 to {MAX_QUERIES_PER_STEP} queries."
        if len(raw_queries) > MAX_QUERIES_PER_STEP:
            return [], (
                f"Error: step {position} may have at most "
                f"{MAX_QUERIES_PER_STEP} queries."
            )
        queries: List[str] = []
        for query in raw_queries:
            text = str(query or "").strip()[:MAX_QUERY_CHARS]
            if not text:
                return [], f"Error: step {position} has an empty query."
            queries.append(text)
        steps.append(PlanStep(index=position, question=question, queries=tuple(queries)))
    return steps, ""


def _result(payload: Dict[str, Any]) -> ToolResult:
    """A structured result, asserted to fit the bounded-content budget.

    The payload-must-fit invariant the graph tools already carry: every field
    here is clipped at parse time, so a result that overflowed would be a bug
    in a bound, not a big plan.
    """
    content = json.dumps(payload)
    assert len(content) <= MAX_RESULT_CHARS, "step-plan result exceeded the tool budget"
    return ToolResult(content=content)


def _submit_plan(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    state = state_for(db, context.workspace_id, context.run_id)
    if state.submitted:
        expected = state.next_index
        return ToolResult(
            content=(
                "Error: a plan was already submitted for this turn; call "
                f"complete_step for step {expected}."
                if expected
                else "Error: a plan was already submitted for this turn and every "
                "step is complete; write the final answer now."
            )
        )
    steps, error = _parse_steps(args.get("steps"))
    if error:
        return ToolResult(content=error)
    append_event(
        db,
        workspace_id=context.workspace_id,
        run_id=context.run_id,
        event_type=PLAN_SUBMITTED,
        payload={
            "steps": [
                {"index": step.index, "question": step.question, "queries": list(step.queries)}
                for step in steps
            ]
        },
    )
    db.commit()
    return _result(_next_instruction(PlanState(steps=tuple(steps), completed=0)))


def _complete_step(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    state = state_for(db, context.workspace_id, context.run_id)
    if not state.submitted:
        return ToolResult(content="Error: submit_plan has not been called for this turn yet.")
    expected = state.next_index
    if not expected:
        return ToolResult(
            content=(
                "Error: every step is already complete; write the final answer now."
            )
        )
    try:
        claimed = int(str(args.get("step")))
    except (TypeError, ValueError):
        claimed = 0
    if claimed != expected:
        return ToolResult(content=f"Error: step {expected} is the one to complete next.")
    summary = str(args.get("summary") or "").strip()[:MAX_SUMMARY_CHARS]
    if not summary:
        return ToolResult(content="Error: `summary` is required — it is all the next step sees.")
    raw_ids = args.get("chunk_ids")
    chunk_ids = [
        str(value)
        for value in (raw_ids if isinstance(raw_ids, list) else [])
        if str(value).strip()
    ][:MAX_STEP_CHUNK_IDS]
    step = state.steps[expected - 1]
    append_event(
        db,
        workspace_id=context.workspace_id,
        run_id=context.run_id,
        event_type=PLAN_STEP,
        payload={
            "index": expected,
            "question": step.question,
            "summary": summary,
            "chunk_ids": chunk_ids,
        },
    )
    db.commit()
    return _result(
        _next_instruction(PlanState(steps=state.steps, completed=state.completed + 1))
    )


def plan_tools() -> Dict[str, ToolSpec]:
    """The two mode tools. Both read-only: they record a plan, they do not act."""
    return {
        SUBMIT_PLAN: ToolSpec(
            name=SUBMIT_PLAN,
            description=(
                "Submit this turn's plan before doing any research. At most 5 "
                "steps; each is one question and the retrieval queries that "
                "would answer it. Call it exactly once, first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": MAX_STEPS,
                        "items": {
                            "type": "object",
                            "properties": {
                                "question": {
                                    "type": "string",
                                    "description": "The one thing this step establishes.",
                                },
                                "queries": {
                                    "type": "array",
                                    "minItems": 1,
                                    "maxItems": MAX_QUERIES_PER_STEP,
                                    "items": {"type": "string"},
                                    "description": (
                                        "Search terms for this step, as you would "
                                        "pass them to search_sources."
                                    ),
                                },
                            },
                            "required": ["question", "queries"],
                        },
                    }
                },
                "required": ["steps"],
            },
            executor=_submit_plan,
        ),
        COMPLETE_STEP: ToolSpec(
            name=COMPLETE_STEP,
            description=(
                "Close the current plan step with what it established. The "
                "summary is the only thing carried into the next step, so put "
                "the findings in it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "step": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_STEPS,
                        "description": "The 1-based step you are completing.",
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "What this step established, in a few sentences. "
                            f"Clipped at {MAX_SUMMARY_CHARS} characters."
                        ),
                    },
                    "chunk_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": MAX_STEP_CHUNK_IDS,
                        "description": (
                            "The ids of the passages this step actually used. "
                            "The coverage report under the answer is built "
                            "from these, so a step that names none is recorded "
                            "as having found nothing."
                        ),
                    },
                },
                "required": ["step", "summary"],
            },
            executor=_complete_step,
        ),
    }


def narrowed(
    registry: Dict[str, ToolSpec], instructions: str, run: Optional[Run]
) -> Tuple[Dict[str, ToolSpec], str]:
    """The turn's registry and instructions, adjusted for plan-then-execute mode.

    Returns both arguments UNCHANGED when the flag is off. That identity is the
    whole compatibility guarantee: a default turn's tool payload and instruction
    text are the same bytes they were before this module existed.
    """
    if run is None or not run.step_plan:
        return registry, instructions
    extended = dict(registry)
    extended.update(plan_tools())
    return extended, f"{instructions}\n\n{STEP_PLAN_INSTRUCTIONS}"


def iteration_budget(run: Optional[Run]) -> int:
    """How many rounds this turn gets.

    Defined here rather than in the loop so the two numbers sit next to the
    reason for the difference between them.
    """
    from .agent_loop import MAX_ITERATIONS

    return STEP_PLAN_MAX_ITERATIONS if run is not None and run.step_plan else MAX_ITERATIONS
