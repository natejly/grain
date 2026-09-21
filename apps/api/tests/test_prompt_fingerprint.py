"""`Run.prompt_fingerprint`: which prompt configuration a turn actually ran under.

Instructions are assembled at run time from an agent's voice, a space's
standing block, a member's style, a skill's body and a coworking digest, and
none of it is stored on the run — so two runs that answered differently were,
until this column, not comparable at all. The fingerprint is the cheapest thing
that answers "same prompt or not".

What is pinned here: the digest's inputs (it moves with instructions, model and
effort and with nothing else), that it is written where the instructions FINISH
assembling rather than before the plan-mode splice, that a resume re-stamps,
and that it can never fail the turn it describes.
"""
from __future__ import annotations

from typing import Any, List, Tuple

from conftest import create_identity
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import Agent, Conversation, Run
from app.services import agent_loop
from app.services.agent_loop import (
    PROMPT_FINGERPRINT_SCHEME,
    prompt_fingerprint,
    run_agent_turn,
)


class FakeResponse:
    def __init__(self, output=None, output_text="done"):
        self.output = output or []
        self.output_text = output_text


def _step(seen: List[Tuple[Any, str]]):
    def model_step(input_items, tools, instructions):
        seen.append((tools, instructions))
        return [("completed", FakeResponse())]

    return model_step


def _run(approval_mode: str = "ask_writes", **run_kwargs: Any) -> str:
    identity = create_identity(name="FP owner", workspace_name="Fingerprints")
    db = SessionLocal()
    try:
        agent_id = db.scalar(
            select(Agent.id).where(Agent.workspace_id == identity.workspace_id)
        )
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
            agent_id=agent_id,
            created_by=identity.user_id,
            status="running",
            prompt="who owns the launch?",
            **run_kwargs,
        )
        db.add(run)
        db.commit()
        return run.id
    finally:
        db.close()


def _turn(run_id: str) -> Tuple[str, str]:
    """Run one turn; return (the instructions sent, the fingerprint stored)."""
    seen: List[Tuple[Any, str]] = []
    db = SessionLocal()
    try:
        run_agent_turn(db, db.get(Run, run_id), evidence=[], model_step=_step(seen))
    finally:
        db.close()
    db = SessionLocal()
    try:
        return seen[-1][1], db.get(Run, run_id).prompt_fingerprint
    finally:
        db.close()


# --- the digest itself -------------------------------------------------------


def test_the_digest_is_scheme_tagged_and_fits_the_column():
    stamp = prompt_fingerprint("instructions", model="m", effort="low")
    scheme, _, digest = stamp.partition(":")
    assert scheme == PROMPT_FINGERPRINT_SCHEME
    assert len(digest) == 64
    # `Run.prompt_fingerprint` is String(80): the tag has to fit beside the hash
    # or the first scheme bump becomes a migration.
    assert len(stamp) <= 80


def test_every_input_moves_the_digest():
    base = prompt_fingerprint("a", model="m", effort="low")
    assert prompt_fingerprint("b", model="m", effort="low") != base
    assert prompt_fingerprint("a", model="n", effort="low") != base
    assert prompt_fingerprint("a", model="m", effort="high") != base


def test_the_digest_is_stable_across_calls():
    assert prompt_fingerprint("a", model="m", effort="low") == prompt_fingerprint(
        "a", model="m", effort="low"
    )


def test_the_fields_cannot_be_confused_for_each_other():
    """Canonical JSON, not concatenation: without it, moving a character across
    a field boundary would leave the digest unchanged."""
    assert prompt_fingerprint("ab", model="c", effort="d") != prompt_fingerprint(
        "a", model="bc", effort="d"
    )


# --- where it is written -----------------------------------------------------


def test_a_turn_stamps_the_instructions_it_actually_sent():
    run_id = _run()
    instructions, stamp = _turn(run_id)
    settings = get_settings()
    assert stamp == prompt_fingerprint(
        instructions,
        model=settings.default_model,
        effort=settings.openai_reasoning_effort,
    )


def test_plan_mode_fingerprints_differently_from_the_same_agent_off_plan():
    """The splice happens AFTER `resolve_directives`, so a fingerprint taken
    there would call two materially different prompts the same one."""
    _, plain = _turn(_run())
    _, planning = _turn(_run(approval_mode="plan"))
    assert plain != planning


def test_a_per_turn_model_override_changes_the_stamp():
    _, default_model = _turn(_run())
    _, overridden = _turn(_run(requested_model="gpt-5-nano"))
    assert default_model != overridden


def test_a_historical_run_reads_as_unattributable_not_as_a_match():
    """"" is the honest value for a row written before the column: it must not
    collide with any real fingerprint."""
    run_id = _run()
    db = SessionLocal()
    try:
        assert db.get(Run, run_id).prompt_fingerprint == ""
    finally:
        db.close()
    _, stamp = _turn(run_id)
    assert stamp != ""


def test_a_failed_stamp_never_fails_the_turn(monkeypatch):
    """Observability must not be able to break the thing it observes."""
    def boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("hash backend exploded")

    monkeypatch.setattr(agent_loop, "prompt_fingerprint", boom)
    run_id = _run()
    seen: List[Tuple[Any, str]] = []
    db = SessionLocal()
    try:
        result = run_agent_turn(
            db, db.get(Run, run_id), evidence=[], model_step=_step(seen)
        )
    finally:
        db.close()
    assert result is not None and result.answer == "done"
