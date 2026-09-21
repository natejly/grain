"""Plan-then-execute mode: off by default, and byte-identical when it is.

THE PIN THAT GUARDS EVERYTHING ELSE is the first one. This mode adds two tools,
a directive block and six extra iterations, and every one of those is a change
to what the model sees. With the flag off, the registry, the instruction string
and the iteration arithmetic must be exactly what they were — which is why the
splice lives in `run_agent_turn` next to `_plan_narrowed` and NEVER in
`resolve_directives`, where it would break the `== CHAT_INSTRUCTIONS` equality
asserts in three other modules.

The second pin is that the plan state is derived from run events alone. A turn
that parks on an approval resumes in another process with a fresh session; a
plan held in a loop local would be gone, and one held in a column would be a
second copy of a truth the events already carry.
"""
from __future__ import annotations

import json

from conftest import create_identity

from app.database import SessionLocal
from app.models import Conversation, Run, RunEvent
from app.services import agent_loop, step_plan, subjects
from app.services.agent_loop import MAX_ITERATIONS, resolve_directives
from app.services.llm_tools import MAX_RESULT_CHARS, ToolContext
from app.services.model import CHAT_INSTRUCTIONS


def _thread(*, plan: bool, approval_mode: str = "auto_writes") -> tuple[str, str]:
    """(run_id, workspace_id) for a plain thread with the flag set either way."""
    identity = create_identity(name="Planner", workspace_name="Step plan")
    db = SessionLocal()
    try:
        conversation = Conversation(
            workspace_id=identity.workspace_id,
            created_by=identity.user_id,
            approval_mode=approval_mode,
        )
        db.add(conversation)
        db.flush()
        run = Run(
            workspace_id=identity.workspace_id,
            conversation_id=conversation.id,
            agent_id="",
            created_by=identity.user_id,
            status="running",
            prompt="Why did the migration slip?",
            step_plan=plan,
        )
        db.add(run)
        db.commit()
        return run.id, identity.workspace_id
    finally:
        db.close()


def _narrowed(run_id: str):
    """The registry and instructions a turn would be built with, through the
    same two seams `run_agent_turn` uses and in the same order."""
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        settings = agent_loop.get_settings()
        subject = subjects.resolve(db, run)
        context = subjects.tool_context(run, subject, space_id="")
        directives = resolve_directives(db, run)
        registry = agent_loop._registry_for(
            db, context, subject, directives, settings, run
        )
        scope = agent_loop.policy_scope_for_run(db, run)
        registry, instructions = agent_loop._plan_narrowed(
            registry,
            directives.instructions,
            agent_loop.approval_mode_for_run(db, run, scope=scope, settings=settings),
        )
        return step_plan.narrowed(registry, instructions, run)
    finally:
        db.close()


def _context(run_id: str, workspace_id: str) -> ToolContext:
    return ToolContext(
        workspace_id=workspace_id, user_id="tester", conversation_id="", run_id=run_id
    )


def _events(run_id: str, event_type: str) -> list[dict]:
    db = SessionLocal()
    try:
        rows = (
            db.query(RunEvent)
            .filter(RunEvent.run_id == run_id, RunEvent.event_type == event_type)
            .order_by(RunEvent.sequence)
            .all()
        )
        return [json.loads(row.payload_json or "{}") for row in rows]
    finally:
        db.close()


# --- (a)-(c) the flag, both ways ---------------------------------------------


def test_the_mode_is_absent_by_default(client):
    run_id, _workspace = _thread(plan=False)
    registry, instructions = _narrowed(run_id)
    assert step_plan.SUBMIT_PLAN not in registry
    assert step_plan.COMPLETE_STEP not in registry
    assert instructions == CHAT_INSTRUCTIONS
    db = SessionLocal()
    try:
        assert step_plan.iteration_budget(db.get(Run, run_id)) == MAX_ITERATIONS
    finally:
        db.close()


def test_the_mode_adds_two_tools_a_block_and_six_iterations(client):
    run_id, _workspace = _thread(plan=True)
    registry, instructions = _narrowed(run_id)
    assert step_plan.SUBMIT_PLAN in registry
    assert step_plan.COMPLETE_STEP in registry
    assert instructions.endswith(step_plan.STEP_PLAN_INSTRUCTIONS)
    assert instructions.startswith(CHAT_INSTRUCTIONS)
    db = SessionLocal()
    try:
        assert step_plan.iteration_budget(db.get(Run, run_id)) == 12
    finally:
        db.close()


def test_the_block_reads_last_even_behind_plan_mode(client):
    """Plan mode's block first, this turn's mode last: the most turn-specific
    instruction is the one the model should read closest to the question."""
    run_id, _workspace = _thread(plan=True, approval_mode="plan")
    registry, instructions = _narrowed(run_id)
    assert agent_loop.PLAN_MODE_INSTRUCTIONS in instructions
    assert instructions.endswith(step_plan.STEP_PLAN_INSTRUCTIONS)
    assert instructions.index(agent_loop.PLAN_MODE_INSTRUCTIONS) < instructions.index(
        step_plan.STEP_PLAN_INSTRUCTIONS
    )
    # Plan mode still strips writes; the two mode tools are read-only and stay.
    assert step_plan.SUBMIT_PLAN in registry
    assert agent_loop.EXIT_PLAN_MODE in registry


def test_the_directive_resolver_never_learns_about_the_mode(client):
    """THE EQUALITY ASSERT. `test_agent_directives`, `test_space_directives` and
    `test_style_directives` all compare `resolve_directives(...).instructions`
    to `CHAT_INSTRUCTIONS`; a splice that drifted into the resolver would fail
    all three. This asserts it here too, where the reason is written down."""
    run_id, _workspace = _thread(plan=True)
    db = SessionLocal()
    try:
        assert resolve_directives(db, db.get(Run, run_id)).instructions == (
            CHAT_INSTRUCTIONS
        )
    finally:
        db.close()


# --- (d)-(f) submitting a plan -----------------------------------------------


def _submit(run_id: str, workspace_id: str, steps):
    db = SessionLocal()
    try:
        return step_plan._submit_plan(
            db, _context(run_id, workspace_id), {"steps": steps}
        )
    finally:
        db.close()


def test_a_malformed_plan_errors_and_writes_nothing(client):
    run_id, workspace = _thread(plan=True)
    good_step = {"question": "What slipped?", "queries": ["migration slip"]}
    cases = [
        [],
        [good_step] * 6,
        [{"question": "No queries", "queries": []}],
        [{"question": "Too many", "queries": ["a", "b", "c", "d", "e"]}],
        "not a list",
        [{"question": "x" * 400, "queries": ["a"]}],
        [{"queries": ["a"]}],
    ]
    for steps in cases:
        result = _submit(run_id, workspace, steps)
        assert result.content.startswith("Error: "), steps
    assert _events(run_id, step_plan.PLAN_SUBMITTED) == []


def test_a_valid_plan_writes_one_event_and_names_step_one(client):
    run_id, workspace = _thread(plan=True)
    result = _submit(
        run_id,
        workspace,
        [
            {"question": "What changed?", "queries": ["migration diff"]},
            {"question": "When did it slip?", "queries": ["timeline", "dates"]},
        ],
    )
    payload = json.loads(result.content)
    assert payload["next_step"] == 1
    assert payload["of"] == 2
    assert payload["question"] == "What changed?"
    events = _events(run_id, step_plan.PLAN_SUBMITTED)
    assert len(events) == 1
    assert [step["index"] for step in events[0]["steps"]] == [1, 2]


def test_a_second_submission_is_refused_and_writes_nothing(client):
    run_id, workspace = _thread(plan=True)
    _submit(run_id, workspace, [{"question": "One", "queries": ["a"]}])
    again = _submit(run_id, workspace, [{"question": "Two", "queries": ["b"]}])
    assert again.content.startswith("Error: a plan was already submitted")
    assert "complete_step for step 1" in again.content
    assert len(_events(run_id, step_plan.PLAN_SUBMITTED)) == 1


# --- (g)-(i) working the steps -----------------------------------------------


def _complete(run_id: str, workspace_id: str, **args):
    db = SessionLocal()
    try:
        return step_plan._complete_step(db, _context(run_id, workspace_id), args)
    finally:
        db.close()


def test_steps_complete_in_order_and_the_last_one_says_to_answer(client):
    run_id, workspace = _thread(plan=True)
    _submit(
        run_id,
        workspace,
        [
            {"question": "What changed?", "queries": ["migration diff"]},
            {"question": "When?", "queries": ["timeline"]},
        ],
    )
    wrong = _complete(run_id, workspace, step=2, summary="Out of order.")
    assert wrong.content == "Error: step 1 is the one to complete next."
    assert _events(run_id, step_plan.PLAN_STEP) == []

    first = json.loads(
        _complete(run_id, workspace, step=1, summary="The index moved.").content
    )
    assert first["next_step"] == 2
    assert first["question"] == "When?"

    blank = _complete(run_id, workspace, step=2, summary="   ")
    assert blank.content.startswith("Error: `summary` is required")

    last = json.loads(
        _complete(
            run_id,
            workspace,
            step=2,
            summary="It slipped in week three.",
            chunk_ids=["c1", "c2"],
        ).content
    )
    assert last["status"] == "complete"
    assert "Write the final answer now" in last["instruction"]

    steps = _events(run_id, step_plan.PLAN_STEP)
    assert [event["index"] for event in steps] == [1, 2]
    assert steps[1]["chunk_ids"] == ["c1", "c2"]
    assert _complete(run_id, workspace, step=3, summary="extra").content.startswith(
        "Error: every step is already complete"
    )


def test_the_state_survives_a_closed_session(client):
    """THE PARK/RESUME PIN. The state is rebuilt from the events alone, so a
    turn that parks for an hour and resumes in another process comes back to
    the step it was on with no column and no `LoopState` field."""
    run_id, workspace = _thread(plan=True)
    _submit(
        run_id,
        workspace,
        [
            {"question": "One", "queries": ["a"]},
            {"question": "Two", "queries": ["b"]},
            {"question": "Three", "queries": ["c"]},
        ],
    )
    _complete(run_id, workspace, step=1, summary="Done one.")
    db = SessionLocal()
    try:
        state = step_plan.state_for(db, workspace, run_id)
        assert state.submitted is True
        assert len(state.steps) == 3
        assert state.next_index == 2
    finally:
        db.close()


def test_every_result_fits_the_tool_budget(client):
    """The payload-must-fit invariant. Every field is clipped at parse time, so
    a maximal plan with maximal questions still fits."""
    run_id, workspace = _thread(plan=True)
    result = _submit(
        run_id,
        workspace,
        [
            {"question": "q" * 300, "queries": ["r" * 200] * 4}
            for _ in range(step_plan.MAX_STEPS)
        ],
    )
    assert len(result.content) <= MAX_RESULT_CHARS
    completed = _complete(run_id, workspace, step=1, summary="s" * 4000)
    assert len(completed.content) <= MAX_RESULT_CHARS
    # Clipped at parse time, which is why the result above could not overflow.
    stored = _events(run_id, step_plan.PLAN_STEP)[0]["summary"]
    assert len(stored) == step_plan.MAX_SUMMARY_CHARS


def test_the_instruction_block_stays_short(client):
    """A planner prompt works better brief — a long block spends the attention
    the plan itself needs. The bound is the design, so it is asserted."""
    assert len(step_plan.STEP_PLAN_INSTRUCTIONS) <= 900
    assert len(step_plan.PLAN_SUBMITTED) <= 40
    assert len(step_plan.PLAN_STEP) <= 40


def test_complete_step_before_a_plan_is_an_error(client):
    run_id, workspace = _thread(plan=True)
    result = _complete(run_id, workspace, step=1, summary="Nothing planned yet.")
    assert result.content.startswith("Error: submit_plan has not been called")
    assert _events(run_id, step_plan.PLAN_STEP) == []


# --- the whole loop, with the flag on ----------------------------------------


def test_a_planned_turn_runs_end_to_end_inside_the_larger_budget(client):
    """The mode actually works: a two-step plan is submitted, both steps are
    completed, and the turn answers — all inside the twelve iterations the flag
    buys. Every assertion above tests a seam; this one tests the mode."""
    from types import SimpleNamespace

    from app.services.agent_loop import run_agent_turn

    run_id, workspace = _thread(plan=True)
    plan = [
        {"question": "What changed?", "queries": ["migration diff"]},
        {"question": "When did it slip?", "queries": ["timeline"]},
    ]

    def _completed(output=None, output_text=""):
        return [
            (
                "completed",
                SimpleNamespace(output=output or [], output_text=output_text),
            )
        ]

    def _call(name: str, arguments: dict, call_id: str):
        return SimpleNamespace(
            type="function_call",
            name=name,
            call_id=call_id,
            arguments=json.dumps(arguments),
        )

    rounds: list[int] = []

    def model_step(input_items, tools, instructions):
        # The mode's own tools are on offer, every round. `.get` because a
        # hosted provider tool joins the payload without a `name`.
        names = {tool.get("name") for tool in tools}
        if tools:
            assert step_plan.SUBMIT_PLAN in names
            assert step_plan.COMPLETE_STEP in names
        rounds.append(len(rounds) + 1)
        step = len(rounds)
        if step == 1:
            return _completed(
                output=[_call(step_plan.SUBMIT_PLAN, {"steps": plan}, "c1")]
            )
        if step == 2:
            return _completed(
                output=[
                    _call(
                        step_plan.COMPLETE_STEP,
                        {"step": 1, "summary": "The index moved in week two."},
                        "c2",
                    )
                ]
            )
        if step == 3:
            return _completed(
                output=[
                    _call(
                        step_plan.COMPLETE_STEP,
                        {"step": 2, "summary": "It slipped in week three."},
                        "c3",
                    )
                ]
            )
        return _completed(output_text="It slipped in week three because the index moved.")

    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        result = run_agent_turn(db, run, evidence=[], model_step=model_step)
        assert result is not None
        assert "week three" in result.answer
    finally:
        db.close()
    assert len(rounds) <= step_plan.STEP_PLAN_MAX_ITERATIONS
    db = SessionLocal()
    try:
        kinds = [
            row.event_type
            for row in db.query(RunEvent)
            .filter(
                RunEvent.run_id == run_id,
                RunEvent.event_type.in_(
                    (step_plan.PLAN_SUBMITTED, step_plan.PLAN_STEP)
                ),
            )
            .order_by(RunEvent.sequence)
            .all()
        ]
        assert kinds == [
            step_plan.PLAN_SUBMITTED,
            step_plan.PLAN_STEP,
            step_plan.PLAN_STEP,
        ]
    finally:
        db.close()
