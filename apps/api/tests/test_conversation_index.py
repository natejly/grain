"""The conversation index: transcripts chunked, summarized, and quoted back.

Three properties carry the feature and each is pinned here:

1. **Incremental, verbatim chunking.** A message is covered by exactly one
   window, windows quote the transcript's own words, and re-indexing writes
   nothing new — so the index can run after every turn without re-billing the
   thread.
2. **Visibility is `resolve_visible`'s rule, exactly.** A personal thread's
   words are quotable only by its creator; shared and subject threads by any
   member; nothing crosses a workspace. This is the transcript version of the
   leak commit ffa0608 closed for memory, and the structural test keeps the
   rule in one chokepoint.
3. **Search degrades, never fails.** No embeddings means lexical-only; an
   unindexed thread is picked up by the search-time reconcile; the feature
   switched off means empty results and zero writes.
"""
from __future__ import annotations

import inspect
import json
from datetime import timedelta
from typing import List

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import DEV_SEED_USER_ID
from app.clock import utcnow
from app.config import get_settings
from app.database import SessionLocal
from app.models import Conversation, ConversationChunk, Message, User, Workspace
from app.services import conversation_index as ci
from app.services.embeddings import pack_vector
from app.services.llm_tools import ToolContext, registry_families
from app.services.subjects import DOCUMENT, allowed_tools_for


@pytest.fixture
def workspace(client) -> str:
    """A fresh, empty workspace so seeded threads cannot collide across tests."""
    db = SessionLocal()
    try:
        row = Workspace(name="conversation-index")
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _user(db: Session, email: str) -> str:
    row = User(email=email, name=email.split("@")[0])
    db.add(row)
    db.flush()
    return row.id


def _conversation(
    db: Session,
    workspace_id: str,
    *,
    created_by: str = DEV_SEED_USER_ID,
    shared: bool = False,
    subject_kind: str = "",
    subject_id: str = "",
    title: str = "A thread",
) -> Conversation:
    row = Conversation(
        workspace_id=workspace_id,
        created_by=created_by,
        title=title,
        shared=shared,
        subject_kind=subject_kind,
        subject_id=subject_id,
    )
    db.add(row)
    db.flush()
    return row


def _messages(
    db: Session,
    conversation: Conversation,
    contents: List[str],
    *,
    start_offset_seconds: int = 0,
) -> List[Message]:
    """Alternating user/assistant messages with strictly increasing timestamps."""
    base = utcnow() + timedelta(seconds=start_offset_seconds)
    rows = []
    for index, content in enumerate(contents):
        row = Message(
            workspace_id=conversation.workspace_id,
            conversation_id=conversation.id,
            run_id="",
            role="user" if index % 2 == 0 else "assistant",
            content=content,
            created_by=conversation.created_by,
            created_at=base + timedelta(seconds=index),
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def _chunks(db: Session, conversation_id: str) -> List[ConversationChunk]:
    return list(
        db.scalars(
            select(ConversationChunk)
            .where(ConversationChunk.conversation_id == conversation_id)
            .order_by(ConversationChunk.kind, ConversationChunk.ordinal)
        )
    )


def _covered_ids(rows: List[ConversationChunk]) -> set[str]:
    return {
        message_id
        for row in rows
        if row.kind == "chunk"
        for message_id in json.loads(row.message_ids_json)
    }


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #


def test_indexing_covers_every_message_and_quotes_verbatim(workspace):
    db = SessionLocal()
    try:
        conversation = _conversation(db, workspace)
        sentence = "The kestrel deploy freeze starts Friday at noon."
        messages = _messages(
            db,
            conversation,
            ["What is the deploy plan?", sentence, "Thanks!", "Any time."],
        )
        written = ci.index_conversation(db, conversation)
        db.commit()

        rows = _chunks(db, conversation.id)
        chunk_rows = [row for row in rows if row.kind == "chunk"]
        assert written == len(chunk_rows) > 0
        assert _covered_ids(rows) == {message.id for message in messages}
        joined = "\n".join(row.content for row in chunk_rows)
        # Verbatim — the words, with the speaker named, not a paraphrase.
        assert f"Assistant: {sentence}" in joined
        assert "User: What is the deploy plan?" in joined
    finally:
        db.close()


def test_indexing_is_incremental_and_never_repacks(workspace):
    db = SessionLocal()
    try:
        conversation = _conversation(db, workspace)
        _messages(db, conversation, ["First question?", "First answer."])
        assert ci.index_conversation(db, conversation) > 0
        db.commit()
        before = {row.id: row.content for row in _chunks(db, conversation.id)}

        # A second pass over the same transcript writes nothing.
        assert ci.index_conversation(db, conversation) == 0

        # New messages extend the index; existing windows are never rewritten.
        added = _messages(
            db, conversation, ["Second question?", "Second answer."],
            start_offset_seconds=60,
        )
        assert ci.index_conversation(db, conversation) > 0
        db.commit()
        rows = _chunks(db, conversation.id)
        for row in rows:
            if row.id in before:
                assert row.content == before[row.id]
        assert {message.id for message in added} <= _covered_ids(rows)
    finally:
        db.close()


def test_an_oversized_message_is_split_not_dropped(workspace):
    db = SessionLocal()
    try:
        conversation = _conversation(db, workspace)
        essay = " ".join(f"sentence{i} of the very long answer." for i in range(200))
        assert len(essay) > ci.MAX_SINGLE_MESSAGE_CHARS
        (message,) = _messages(db, conversation, [essay])
        ci.index_conversation(db, conversation)
        db.commit()
        rows = [row for row in _chunks(db, conversation.id) if row.kind == "chunk"]
        assert len(rows) > 1
        for row in rows:
            assert json.loads(row.message_ids_json) == [message.id]
    finally:
        db.close()


def test_summary_falls_back_offline_and_refreshes_on_cadence(workspace):
    """Scripted mode has no summary model, so the naive topics line is the row —
    written at ten messages, stable until ten more arrive."""
    db = SessionLocal()
    try:
        conversation = _conversation(db, workspace)
        _messages(
            db,
            conversation,
            [f"Message number {i} about the atlas launch." for i in range(10)],
        )
        ci.index_conversation(db, conversation)
        db.commit()
        summary = next(
            row for row in _chunks(db, conversation.id) if row.kind == "summary"
        )
        assert summary.content.startswith("Conversation topics so far: ")
        assert summary.message_count == 10

        # One more message is not a refresh; ten more are.
        _messages(db, conversation, ["An eleventh message."], start_offset_seconds=60)
        ci.index_conversation(db, conversation)
        db.flush()
        assert summary.message_count == 10
        _messages(
            db,
            conversation,
            [f"Later message {i}." for i in range(9)],
            start_offset_seconds=120,
        )
        ci.index_conversation(db, conversation)
        db.commit()
        db.refresh(summary)
        assert summary.message_count == 20
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Visibility
# --------------------------------------------------------------------------- #


def _search(db, workspace_id: str, viewer_id: str, query: str):
    return ci.search_conversation_chunks(
        db, workspace_id=workspace_id, viewer_id=viewer_id, query=query
    )


def test_search_never_quotes_another_members_personal_thread(workspace):
    db = SessionLocal()
    try:
        other = _user(db, "colleague@example.com")
        theirs = _conversation(
            db, workspace, created_by=other, title="Their private thread"
        )
        _messages(db, theirs, ["The falconer budget is secret.", "Noted."])
        shared = _conversation(
            db, workspace, created_by=other, shared=True, title="Team thread"
        )
        _messages(db, shared, ["The falconer launch is Tuesday.", "Noted."])
        for conversation in (theirs, shared):
            ci.index_conversation(db, conversation)
        db.commit()

        as_owner = _search(db, workspace, DEV_SEED_USER_ID, "falconer")
        assert as_owner, "a shared thread must be searchable by any member"
        assert {hit.conversation_id for hit in as_owner} == {shared.id}
        assert all("secret" not in hit.content for hit in as_owner)

        as_creator = _search(db, workspace, other, "falconer")
        assert {hit.conversation_id for hit in as_creator} == {theirs.id, shared.id}
    finally:
        db.close()


def test_a_subject_thread_is_quotable_by_any_member(workspace):
    db = SessionLocal()
    try:
        other = _user(db, "author@example.com")
        panel = _conversation(
            db,
            workspace,
            created_by=other,
            subject_kind=DOCUMENT,
            subject_id="doc-1",
            title="Beside the doc",
        )
        _messages(db, panel, ["Rework the ospreys paragraph.", "Done."])
        ci.index_conversation(db, panel)
        db.commit()
        hits = _search(db, workspace, DEV_SEED_USER_ID, "ospreys paragraph")
        assert {hit.conversation_id for hit in hits} == {panel.id}
    finally:
        db.close()


def test_search_never_crosses_a_workspace_boundary(workspace):
    db = SessionLocal()
    try:
        foreign = Workspace(name="someone-elses")
        db.add(foreign)
        db.flush()
        conversation = _conversation(
            db, foreign.id, shared=True, title="Foreign thread"
        )
        _messages(db, conversation, ["The gyrfalcon migration is complete.", "Yes."])
        ci.index_conversation(db, conversation)
        db.commit()
        assert _search(db, workspace, DEV_SEED_USER_ID, "gyrfalcon migration") == []
    finally:
        db.close()


def test_the_visibility_rule_lives_in_one_chokepoint():
    """Every gate `resolve_visible` spells appears exactly once in the module —
    inside `_visible` — so no search arm can carry its own copy that drifts."""
    source = inspect.getsource(ci)
    assert source.count("Conversation.created_by") == 1
    assert source.count("Conversation.shared") == 1
    assert source.count('Conversation.subject_id != ""') == 1
    for arm in ("_lexical_ranking", "_dense_ranking", "search_conversation_chunks"):
        assert "_visible(" in inspect.getsource(getattr(ci, arm))


# --------------------------------------------------------------------------- #
# Search arms, reconcile, and the off switch
# --------------------------------------------------------------------------- #


def test_reconcile_makes_unhooked_threads_searchable(workspace):
    """Messages written by any path that skips the post-run hook still become
    quotable on the next search — the index self-heals on read."""
    db = SessionLocal()
    try:
        conversation = _conversation(db, workspace, title="Cron output")
        _messages(db, conversation, ["The merlin report finished overnight.", "Filed."])
        db.commit()
        assert _chunks(db, conversation.id) == []
        hits = _search(db, workspace, DEV_SEED_USER_ID, "merlin report")
        assert hits and hits[0].conversation_id == conversation.id
        assert "merlin report" in hits[0].content
        assert hits[0].title == "Cron output"
    finally:
        db.close()


def test_dense_arm_finds_what_shares_no_term_with_the_query(workspace, monkeypatch):
    monkeypatch.setattr(
        ci,
        "embed_texts",
        lambda texts, settings=None: [pack_vector([1.0, 0.0]) for _ in texts],
    )
    db = SessionLocal()
    try:
        mine = _conversation(db, workspace, title="Mine")
        _messages(db, mine, ["We chose peregrine for the rollout.", "Agreed."])
        other = _user(db, "rival@example.com")
        theirs = _conversation(db, workspace, created_by=other)
        _messages(db, theirs, ["My private peregrine notes.", "Kept."])
        for conversation in (mine, theirs):
            ci.index_conversation(db, conversation)
        db.commit()
        # No content term appears in the query: only the dense arm can match —
        # and the dense arm must still honour the visibility gate.
        hits = _search(db, workspace, DEV_SEED_USER_ID, "swift raptor decision")
        assert hits and {hit.conversation_id for hit in hits} == {mine.id}
    finally:
        db.close()


def test_disabled_feature_writes_nothing_and_returns_nothing(workspace, monkeypatch):
    settings = get_settings().model_copy(
        update={"conversation_index_enabled": False}
    )
    db = SessionLocal()
    try:
        conversation = _conversation(db, workspace)
        _messages(db, conversation, ["The saker question.", "The saker answer."])
        assert ci.index_conversation(db, conversation, settings) == 0
        assert (
            ci.search_conversation_chunks(
                db,
                workspace_id=workspace,
                viewer_id=DEV_SEED_USER_ID,
                query="saker question",
                settings=settings,
            )
            == []
        )
        assert _chunks(db, conversation.id) == []
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# The tool
# --------------------------------------------------------------------------- #


def test_search_conversations_ships_in_core_and_reaches_subject_threads(workspace):
    db = SessionLocal()
    try:
        context = ToolContext(
            workspace_id=workspace,
            user_id=DEV_SEED_USER_ID,
            conversation_id="",
        )
        families = dict(registry_families(db, context))
        assert "search_conversations" in families["core"]
        spec = families["core"]["search_conversations"]
        assert spec.read_only
        # Core ships to every subject thread, which is the point: "what did we
        # decide about this" is asked beside documents most of all.
        allowed = allowed_tools_for(db, context, DOCUMENT)
        assert allowed is not None and "search_conversations" in allowed
    finally:
        db.close()


def test_the_tool_returns_attributed_quotes(workspace):
    db = SessionLocal()
    try:
        conversation = _conversation(
            db, workspace, shared=True, title="Launch planning"
        )
        _messages(db, conversation, ["When do harriers ship?", "Harriers ship in May."])
        ci.index_conversation(db, conversation)
        db.commit()
        context = ToolContext(
            workspace_id=workspace, user_id=DEV_SEED_USER_ID, conversation_id=""
        )
        spec = dict(registry_families(db, context))["core"]["search_conversations"]
        result = spec.executor(db, context, {"query": "harriers ship"})
        payload = json.loads(result.content)
        assert payload["results"]
        first = payload["results"][0]
        assert first["conversation"] == "Launch planning"
        assert first["kind"] == "quote"
        assert "Harriers ship in May." in first["text"]
        assert first["date"]

        empty = spec.executor(db, context, {"query": "zzzunmatchable"})
        assert "No past conversation" in empty.content
    finally:
        db.close()


def test_the_palette_search_endpoint_reads_the_same_index(client):
    """GET /api/conversations/search: the palette's deep search is the agent
    tool's index and visibility verbatim, over HTTP. What it adds is only a
    snippet clip and a minimum query length — one character is a scan of
    everything ever said, refused at the contract rather than served slowly."""
    identity = client.get("/api/bootstrap").json()["identity"]
    db = SessionLocal()
    try:
        conversation = _conversation(
            db,
            identity["workspace_id"],
            created_by=identity["user_id"],
            title="Falcon notes",
        )
        _messages(db, conversation, ["The gyrfalcon migration starts in March.", "Noted."])
        ci.index_conversation(db, conversation)
        db.commit()
        conversation_id = conversation.id
    finally:
        db.close()

    response = client.get(
        "/api/conversations/search", params={"q": "gyrfalcon migration"}
    )
    assert response.status_code == 200
    hits = response.json()
    match = next((hit for hit in hits if hit["conversation_id"] == conversation_id), None)
    assert match is not None
    assert match["title"] == "Falcon notes"
    assert "gyrfalcon" in match["snippet"].lower()

    assert client.get("/api/conversations/search", params={"q": "x"}).status_code == 422
