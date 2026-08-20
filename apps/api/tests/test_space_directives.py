"""A space's standing instructions reach the turn — and only its own turns.

Same seam as test_agent_directives.py: the third argument to a scripted
`ModelStep` *is* the system prompt, so the end-to-end case reads it directly.
The fallback matrix matters more than the happy path — a deleted space, a
blank block, a cross-workspace id must all degrade to "no injection", never
to a failed turn.
"""
from __future__ import annotations

from typing import Any, List, Tuple

from conftest import create_identity

from app.database import SessionLocal
from app.models import Agent, Conversation, Run, Space
from app.services.agent_loop import resolve_directives, run_agent_turn
from app.services.model import CHAT_INSTRUCTIONS


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
    identity = create_identity(name="Space captain", workspace_name="Space directives")
    return identity.workspace_id, identity.user_id


def _space(workspace_id: str, instructions: str, name: str = "Research") -> str:
    db = SessionLocal()
    try:
        space = Space(workspace_id=workspace_id, name=name, instructions=instructions)
        db.add(space)
        db.commit()
        return space.id
    finally:
        db.close()


def _run_in_space(
    workspace_id: str, user_id: str, space_id: str, *, agent_id: str = ""
) -> str:
    db = SessionLocal()
    try:
        conversation = Conversation(
            workspace_id=workspace_id, created_by=user_id, space_id=space_id
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


def test_space_instructions_are_appended_to_the_stock_prompt() -> None:
    workspace_id, user_id = _tenant()
    space_id = _space(workspace_id, "Cite primary sources only.")
    text = _instructions_for(_run_in_space(workspace_id, user_id, space_id))
    assert text.startswith(CHAT_INSTRUCTIONS)
    assert "Cite primary sources only." in text
    assert "Research" in text  # the block names the space


def test_space_instructions_compose_with_an_agent_not_replace_it() -> None:
    workspace_id, user_id = _tenant()
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
    space_id = _space(workspace_id, "Cite primary sources only.")
    text = _instructions_for(
        _run_in_space(workspace_id, user_id, space_id, agent_id=agent_id)
    )
    assert text.startswith("Answer as Fern.")
    assert "Cite primary sources only." in text
    assert CHAT_INSTRUCTIONS not in text  # the agent still replaces the base


def test_a_thread_outside_any_space_gets_no_block() -> None:
    workspace_id, user_id = _tenant()
    _space(workspace_id, "Cite primary sources only.")
    text = _instructions_for(_run_in_space(workspace_id, user_id, ""))
    assert text == CHAT_INSTRUCTIONS


def test_blank_instructions_and_a_deleted_space_degrade_to_nothing() -> None:
    workspace_id, user_id = _tenant()
    blank = _space(workspace_id, "   ")
    assert (
        _instructions_for(_run_in_space(workspace_id, user_id, blank))
        == CHAT_INSTRUCTIONS
    )
    # A conversation still pointing at a space that is gone: the turn proceeds
    # bare rather than failing — the same total-fallback contract as agents.
    doomed = _space(workspace_id, "Soon gone.")
    run_id = _run_in_space(workspace_id, user_id, doomed)
    db = SessionLocal()
    try:
        db.delete(db.get(Space, doomed))
        db.commit()
    finally:
        db.close()
    assert _instructions_for(run_id) == CHAT_INSTRUCTIONS


def test_another_workspaces_space_id_injects_nothing() -> None:
    workspace_id, user_id = _tenant()
    other_workspace, _ = _tenant()
    foreign = _space(other_workspace, "The other tenant's standing orders.")
    # Stamped directly in the database — the API already refuses this id, so
    # this is the belt-and-braces layer being tested on its own.
    run_id = _run_in_space(workspace_id, user_id, foreign)
    text = _instructions_for(run_id)
    assert "standing orders" not in text
    assert text == CHAT_INSTRUCTIONS


def test_the_block_reaches_the_model_step_end_to_end() -> None:
    workspace_id, user_id = _tenant()
    space_id = _space(workspace_id, "Cite primary sources only.")
    run_id = _run_in_space(workspace_id, user_id, space_id)
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
    assert "Cite primary sources only." in seen[0][1]
