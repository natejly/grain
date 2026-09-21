"""The two-tier transcript a turn is handed.

The window has always been the last N messages; message N+1 simply stopped
existing for every turn after it, which read to a user as the assistant
forgetting what it had been told. The summary row that fixes it already existed
in `conversation_chunks` — refreshed every ten messages, at somebody's expense
— and nothing on the run path had ever read it.

The second half of this file is the clip. `[:600]` cut mid-word, and a fragment
is indistinguishable from a complete quote, so a model reads an invented word
as a fact.
"""
from __future__ import annotations

import os

from conftest import create_identity
from sqlalchemy import select

from app.clock import utcnow
from app.config import get_settings
from app.database import SessionLocal
from app.models import Agent, Conversation, ConversationChunk, Message, Run
from app.services.runs import ELISION, MAX_TRANSCRIPT_MESSAGE_CHARS, _clip, _transcript


def _thread(
    message_count: int, *, summary: str = "", summary_count: int | None = None
) -> tuple[str, str]:
    """A workspace with one conversation holding `message_count` messages plus a
    fresh run. Returns (run_id, workspace_id)."""
    identity = create_identity(
        name="Digest owner", workspace_name="Digest " + os.urandom(3).hex()
    )
    db = SessionLocal()
    try:
        agent_id = db.scalar(
            select(Agent.id).where(Agent.workspace_id == identity.workspace_id)
        )
        conversation = Conversation(
            workspace_id=identity.workspace_id, created_by=identity.user_id
        )
        db.add(conversation)
        db.flush()
        run = Run(
            workspace_id=identity.workspace_id,
            conversation_id=conversation.id,
            agent_id=agent_id,
            created_by=identity.user_id,
            status="running",
            prompt="and then?",
        )
        db.add(run)
        db.flush()
        for index in range(message_count):
            db.add(
                Message(
                    workspace_id=identity.workspace_id,
                    conversation_id=conversation.id,
                    run_id="older-" + str(index),
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"message {index}",
                )
            )
            db.flush()
        if summary:
            db.add(
                ConversationChunk(
                    workspace_id=identity.workspace_id,
                    conversation_id=conversation.id,
                    kind="summary",
                    ordinal=0,
                    content=summary,
                    message_count=(
                        message_count if summary_count is None else summary_count
                    ),
                    last_message_at=utcnow(),
                )
            )
        db.commit()
        return run.id, identity.workspace_id
    finally:
        db.close()


def _digest(run_id: str) -> list[tuple[str, str]]:
    db = SessionLocal()
    try:
        return _transcript(db, db.get(Run, run_id))
    finally:
        db.close()


# --- the clip ----------------------------------------------------------------


def test_a_short_message_is_untouched_apart_from_whitespace():
    assert _clip("  two   words  ", 100) == "two words"


def test_a_clip_lands_on_a_word_boundary_and_says_it_happened():
    clipped = _clip("alpha beta gamma delta", 14)
    assert clipped.endswith(ELISION)
    # Never mid-word: "gam" would read as a complete word nobody wrote.
    assert clipped == "alpha beta" + ELISION


def test_a_single_unbreakable_token_is_cut_where_it_must_be():
    """A 700-character token is not prose, and losing all of it loses the
    message — so this one case cuts where it can."""
    clipped = _clip("x" * 50, 10)
    assert clipped == "x" * 10 + ELISION


def test_the_window_clips_each_message_at_the_budget():
    run_id, _ = _thread(2)
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        message = db.scalar(
            select(Message).where(Message.conversation_id == run.conversation_id)
        )
        message.content = "long " * 400
        db.commit()
    finally:
        db.close()
    roles_and_text = _digest(run_id)
    body = next(text for _role, text in roles_and_text if text.startswith("long"))
    assert body.endswith(ELISION)
    assert len(body) <= MAX_TRANSCRIPT_MESSAGE_CHARS + len(ELISION)


# --- the two tiers -----------------------------------------------------------


def test_a_short_thread_is_quoted_in_full_with_no_summary():
    """Prefixing a summary of what the reader can already see is noise the
    model has to reconcile."""
    run_id, _ = _thread(3, summary="They discussed the launch.")
    digest = _digest(run_id)
    assert [role for role, _text in digest] == ["user", "assistant", "user"]


def test_a_full_window_carries_the_summary_of_what_fell_out():
    limit = get_settings().memory_transcript_messages
    run_id, _ = _thread(limit + 5, summary="Earlier: they picked Postgres.")
    digest = _digest(run_id)

    assert digest[0] == ("earlier", "Earlier: they picked Postgres.")
    assert len(digest) == limit + 1
    # The window is still the NEWEST messages, summary or not.
    assert digest[-1][1] == f"message {limit + 4}"


def test_a_full_window_with_no_summary_row_degrades_to_the_window():
    """A thread indexed before the summary row existed, or one whose indexing
    has not caught up: the window alone, exactly as before."""
    limit = get_settings().memory_transcript_messages
    run_id, _ = _thread(limit + 5)
    digest = _digest(run_id)
    assert len(digest) == limit
    assert "earlier" not in [role for role, _text in digest]
    assert digest[-1][1] == f"message {limit + 4}"


def test_another_threads_summary_is_never_read():
    """The id comes off a `Run`; a summary read across a tenant boundary would
    be spliced straight into this turn's prompt."""
    limit = get_settings().memory_transcript_messages
    run_id, workspace_id = _thread(limit + 2)
    db = SessionLocal()
    try:
        # A summary belonging to a DIFFERENT conversation in the same workspace.
        other = Conversation(workspace_id=workspace_id, created_by="someone")
        db.add(other)
        db.flush()
        db.add(
            ConversationChunk(
                workspace_id=workspace_id,
                conversation_id=other.id,
                kind="summary",
                ordinal=0,
                content="SECRET other-thread summary",
                message_count=50,
                last_message_at=utcnow(),
            )
        )
        db.commit()
    finally:
        db.close()
    assert all("SECRET" not in text for _role, text in _digest(run_id))


def test_the_summary_is_bounded():
    limit = get_settings().memory_transcript_messages
    run_id, _ = _thread(limit + 2, summary="sentence " * 5000)
    digest = _digest(run_id)
    assert digest[0][0] == "earlier"
    assert digest[0][1].endswith(ELISION)
    assert len(digest[0][1]) < 1400


# --- the two tiers have to meet ----------------------------------------------


def test_a_summary_that_does_not_reach_the_window_is_left_out():
    """The splice assumed coverage that only holds by coincidence.

    The summary covers messages 1..`message_count` and is refreshed every
    `SUMMARY_REFRESH_EVERY` (10) messages; the window covers the last
    `MEMORY_TRANSCRIPT_MESSAGES` (10). They meet only while the window is at
    least as wide as the refresh cadence — one a module constant, the other an
    operator-settable env var, with nothing coupling them. Lower the window to
    five and messages 11-14 of a 19-message thread are in neither tier, under
    an "earlier" label that reads as full coverage: the exact forgetting this
    tier was added to fix, now with a summary vouching for it.
    """
    limit = get_settings().memory_transcript_messages
    # The summary stopped at `limit` messages; the window reaches back only
    # `limit` from the end, so message `limit + 1` onwards is in neither tier.
    total = limit * 2 + 1
    run_id, _ = _thread(
        total, summary="Earlier: they picked Postgres.", summary_count=limit
    )
    digest = _digest(run_id)

    assert "earlier" not in [role for role, _text in digest]
    assert len(digest) == limit
    assert digest[-1][1] == f"message {total - 1}"


def test_a_summary_that_reaches_the_window_is_still_spliced():
    """The ordinary case, and the one the guard must not cost: the summary is
    adjacent to the window, so the two tiers are contiguous."""
    limit = get_settings().memory_transcript_messages
    total = limit + 5
    run_id, _ = _thread(
        total, summary="Earlier: they picked Postgres.", summary_count=total - limit
    )
    digest = _digest(run_id)
    assert digest[0] == ("earlier", "Earlier: they picked Postgres.")
    assert len(digest) == limit + 1
