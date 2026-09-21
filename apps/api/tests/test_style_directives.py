"""A member's response style reaches the turn — and 'normal' means NOTHING does.

The load-bearing pin is byte identity: a fresh member's run must resolve to
instructions == CHAT_INSTRUCTIONS, byte-equal, because that is what keeps
every existing `== CHAT_INSTRUCTIONS` equality assert (agent, space, per-turn
and provider suites) green with zero edits. The fallback matrix matters more
than the happy path — an unknown preset, blank custom text, a memberless run
must all degrade to "no injection", never a failed turn.

Same seam as test_space_directives.py: the third argument to a scripted
`ModelStep` *is* the system prompt, so the end-to-end case reads it directly.
"""
from __future__ import annotations

from typing import Any, List, Tuple

from conftest import create_identity
from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    Agent,
    Conversation,
    Membership,
    Run,
    Skill,
    Space,
    User,
    Workflow,
    WorkflowRun,
)
from app.services.agent_loop import resolve_directives, run_agent_turn
from app.services.model import CHAT_INSTRUCTIONS
from app.services.styles import STYLE_DIRECTIVES, style_block

STYLE_MARKER = "Response style for this member:"


class FakeResponse:
    def __init__(self, output=None, output_text=""):
        self.output = output or []
        self.output_text = output_text


def _capture_step(seen: List[Tuple[Any, str]], *, output_text="done"):
    def model_step(input_items, tools, instructions):
        seen.append((tools, instructions))
        return [("completed", FakeResponse(output_text=output_text))]

    return model_step


def _tenant() -> tuple[str, str]:
    identity = create_identity(name="Style setter", workspace_name="Style directives")
    return identity.workspace_id, identity.user_id


def _set_style(workspace_id: str, user_id: str, preset: str, custom: str = "") -> None:
    """Stamp the columns directly — including values the route would refuse,
    because the fallback contract is exactly about rows the API never wrote."""
    db = SessionLocal()
    try:
        membership = db.scalar(
            select(Membership).where(
                Membership.workspace_id == workspace_id,
                Membership.user_id == user_id,
            )
        )
        assert membership is not None
        membership.style_preset = preset
        membership.custom_style_text = custom
        db.commit()
    finally:
        db.close()


def _run_for(
    workspace_id: str,
    user_id: str,
    *,
    space_id: str = "",
    agent_id: str = "",
    skill_id: str = "",
    cron_id: str = "",
) -> str:
    db = SessionLocal()
    try:
        conversation = Conversation(
            workspace_id=workspace_id, created_by=user_id or "", space_id=space_id
        )
        db.add(conversation)
        db.flush()
        run = Run(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            agent_id=agent_id,
            created_by=user_id,
            status="running",
            prompt="hello",
            skill_id=skill_id,
            cron_id=cron_id,
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _instructions_for(run_id: str) -> str:
    db = SessionLocal()
    try:
        return resolve_directives(db, db.get(Run, run_id)).instructions
    finally:
        db.close()


def test_a_fresh_member_resolves_the_stock_prompt_byte_for_byte() -> None:
    # THE RISK PIN. create_identity's membership defaults to 'normal', and
    # 'normal' must append no block at all — a stray newline here breaks four
    # other test files' equality asserts.
    workspace_id, user_id = _tenant()
    text = _instructions_for(_run_for(workspace_id, user_id))
    assert text == CHAT_INSTRUCTIONS


def test_each_fixed_preset_appends_exactly_one_block() -> None:
    for preset, directive in STYLE_DIRECTIVES.items():
        workspace_id, user_id = _tenant()
        _set_style(workspace_id, user_id, preset)
        text = _instructions_for(_run_for(workspace_id, user_id))
        assert text.startswith(CHAT_INSTRUCTIONS)
        assert text.count(STYLE_MARKER) == 1
        assert directive in text


def test_the_block_sits_after_the_space_and_before_the_skill() -> None:
    # The task-pinned order: space -> style -> skill ("most turn-specific
    # last" — a style is who is reading, a skill is what this turn does).
    workspace_id, user_id = _tenant()
    _set_style(workspace_id, user_id, "concise")
    db = SessionLocal()
    try:
        space = Space(
            workspace_id=workspace_id,
            name="Research",
            instructions="Cite primary sources only.",
        )
        skill = Skill(
            workspace_id=workspace_id,
            name="triage",
            title="Triage",
            body="Sort the findings by severity.",
        )
        db.add_all([space, skill])
        db.commit()
        space_id, skill_id = space.id, skill.id
    finally:
        db.close()
    text = _instructions_for(
        _run_for(workspace_id, user_id, space_id=space_id, skill_id=skill_id)
    )
    space_at = text.index("Cite primary sources only.")
    style_at = text.index(STYLE_MARKER)
    skill_at = text.index("Sort the findings by severity.")
    assert space_at < style_at < skill_at


def test_custom_injects_the_members_text() -> None:
    workspace_id, user_id = _tenant()
    _set_style(workspace_id, user_id, "custom", "Always answer in haiku.")
    text = _instructions_for(_run_for(workspace_id, user_id))
    assert text.startswith(CHAT_INSTRUCTIONS)
    assert "Always answer in haiku." in text


def test_custom_with_blank_text_degrades_to_the_stock_prompt() -> None:
    # Stamped directly in the DB, past the route's 422: an empty custom block
    # must render nothing rather than a headed block with no body.
    workspace_id, user_id = _tenant()
    _set_style(workspace_id, user_id, "custom", "   ")
    assert _instructions_for(_run_for(workspace_id, user_id)) == CHAT_INSTRUCTIONS


def test_an_unknown_preset_degrades_to_no_block() -> None:
    workspace_id, user_id = _tenant()
    _set_style(workspace_id, user_id, "sarcastic")
    assert _instructions_for(_run_for(workspace_id, user_id)) == CHAT_INSTRUCTIONS


def test_a_memberless_run_gets_no_block() -> None:
    # Blank created_by: belt over braces for any truly memberless row. Real
    # cron and workflow runs carry their creator's id — the two tests below
    # pin THOSE shapes, which the blank-id guard alone never fires for.
    workspace_id, user_id = _tenant()
    _set_style(workspace_id, user_id, "concise")
    assert _instructions_for(_run_for(workspace_id, "")) == CHAT_INSTRUCTIONS


def test_a_cron_task_run_never_inherits_its_creators_style() -> None:
    # The real cron shape (crons._start_task_run): created_by IS the cron's
    # creator, and `cron_id` is what says nobody is watching — the same
    # marker policy_scope_for_run reads. A member's style must not restyle
    # their nightly report, nor a run-now by a colleague.
    workspace_id, user_id = _tenant()
    _set_style(workspace_id, user_id, "concise")
    run_id = _run_for(workspace_id, user_id, cron_id="cron-nightly-report")
    assert _instructions_for(run_id) == CHAT_INSTRUCTIONS


def test_a_workflow_backing_run_never_inherits_its_creators_style() -> None:
    # The real workflow shape (workflows/executor): the backing Run carries
    # created_by=workflow_run.created_by and no cron_id; the WorkflowRun row
    # pointing at it is the automation marker. Node outputs are parsed by
    # downstream nodes, so a haiku directive must never reach them.
    workspace_id, user_id = _tenant()
    _set_style(workspace_id, user_id, "custom", "Answer only in haiku.")
    run_id = _run_for(workspace_id, user_id)
    db = SessionLocal()
    try:
        workflow = Workflow(
            workspace_id=workspace_id,
            created_by=user_id,
            name="Nightly digest",
            graph_json="{}",
        )
        db.add(workflow)
        db.flush()
        db.add(
            WorkflowRun(
                workspace_id=workspace_id,
                workflow_id=workflow.id,
                created_by=user_id,
                run_id=run_id,
            )
        )
        db.commit()
    finally:
        db.close()
    assert _instructions_for(run_id) == CHAT_INSTRUCTIONS


def test_one_members_style_never_reaches_a_colleagues_run() -> None:
    workspace_id, user_id = _tenant()
    _set_style(workspace_id, user_id, "concise")
    db = SessionLocal()
    try:
        colleague = User(email="colleague-style@example.com", name="Colleague")
        db.add(colleague)
        db.flush()
        db.add(
            Membership(
                workspace_id=workspace_id, user_id=colleague.id, role="member"
            )
        )
        db.commit()
        colleague_id = colleague.id
    finally:
        db.close()
    text = _instructions_for(_run_for(workspace_id, colleague_id))
    assert text == CHAT_INSTRUCTIONS


def test_the_block_reaches_the_model_step_end_to_end() -> None:
    workspace_id, user_id = _tenant()
    _set_style(workspace_id, user_id, "formal")
    run_id = _run_for(workspace_id, user_id)
    seen: List[Tuple[Any, str]] = []
    db = SessionLocal()
    try:
        run_agent_turn(
            db,
            db.get(Run, run_id),
            evidence=[],
            model_step=_capture_step(seen),
        )
    finally:
        db.close()
    assert len(seen) == 1
    assert STYLE_DIRECTIVES["formal"] in seen[0][1]


def test_style_composes_with_an_agent_rather_than_replacing_it() -> None:
    workspace_id, user_id = _tenant()
    _set_style(workspace_id, user_id, "concise")
    db = SessionLocal()
    try:
        agent = Agent(
            workspace_id=workspace_id, name="Fern", instructions="Answer as Fern."
        )
        db.add(agent)
        db.commit()
        agent_id = agent.id
    finally:
        db.close()
    text = _instructions_for(_run_for(workspace_id, user_id, agent_id=agent_id))
    assert text.startswith("Answer as Fern.")
    assert STYLE_MARKER in text
    assert CHAT_INSTRUCTIONS not in text


def test_style_block_is_pure_and_total() -> None:
    # The unit half of the byte-identity pin: every "nothing to say" input is
    # exactly "", not a headed block or a stray newline.
    assert style_block("normal", "") == ""
    assert style_block("normal", "ignored") == ""
    assert style_block("", "") == ""
    assert style_block("unknown", "") == ""
    assert style_block("custom", "   ") == ""
    assert style_block("custom", " be brief ") == (
        f"{STYLE_MARKER}\n\nbe brief"
    )
    for preset, directive in STYLE_DIRECTIVES.items():
        assert style_block(preset, "") == f"{STYLE_MARKER}\n\n{directive}"
