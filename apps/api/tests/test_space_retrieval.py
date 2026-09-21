"""A space's knowledge files retrieve in the space and nowhere else.

The scope predicate lives in `_live_sources`, the one tuple every ranking arm
and the hydrate spread — so these tests drive `search_evidence` (the turn's
entry) and `_search_sources` (the tool's entry, the would-be bypass) and
assert the same three facts through both: space files rank in the space,
never outside it, and the workspace library ranks everywhere.
"""
from __future__ import annotations

from typing import List

import pytest
from conftest import create_identity
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Chunk, Source, Space
from app.services.llm_tools import (
    ToolContext,
    _grounded_answer,  # type: ignore[attr-defined]
    _search_sources,  # type: ignore[attr-defined]
)
from app.services.retrieval import search_evidence


@pytest.fixture
def tenant() -> tuple[str, str]:
    identity = create_identity(name="Retriever", workspace_name="Space retrieval")
    return identity.workspace_id, identity.user_id


def _space(workspace_id: str, name: str) -> str:
    db = SessionLocal()
    try:
        space = Space(workspace_id=workspace_id, name=name)
        db.add(space)
        db.commit()
        return space.id
    finally:
        db.close()


def _seed(
    db: Session,
    workspace_id: str,
    user_id: str,
    passages: List[str],
    *,
    filename: str,
    space_id: str = "",
) -> None:
    source = Source(
        workspace_id=workspace_id,
        created_by=user_id,
        filename=filename,
        media_type="text/markdown",
        object_key="/tmp/not-used",
        byte_size=1,
        status="ready",
        chunk_count=len(passages),
        space_id=space_id,
    )
    db.add(source)
    db.flush()
    for ordinal, text in enumerate(passages):
        db.add(
            Chunk(
                workspace_id=workspace_id,
                source_id=source.id,
                ordinal=ordinal,
                content=text,
                char_start=0,
                char_end=len(text),
                token_count=len(text.split()),
            )
        )
    db.commit()


def _filenames(workspace_id: str, query: str, space_id: str = "") -> set[str]:
    db = SessionLocal()
    try:
        return {
            item.filename
            for item in search_evidence(
                db, workspace_id=workspace_id, query=query, space_id=space_id
            )
        }
    finally:
        db.close()


@pytest.fixture
def corpus(tenant) -> tuple[str, str, str, str]:
    """One space file, one library file, one file in a second space."""
    workspace_id, user_id = tenant
    space_a = _space(workspace_id, "Falconry")
    space_b = _space(workspace_id, "Astronomy")
    db = SessionLocal()
    try:
        _seed(
            db,
            workspace_id,
            user_id,
            ["The kestrel hunts hovering into the wind."],
            filename="kestrel.md",
            space_id=space_a,
        )
        _seed(
            db,
            workspace_id,
            user_id,
            ["The kestrel is a small falcon of open country."],
            filename="library.md",
        )
        _seed(
            db,
            workspace_id,
            user_id,
            ["Kestrel is also the name of a lunar crater."],
            filename="crater.md",
            space_id=space_b,
        )
    finally:
        db.close()
    return workspace_id, user_id, space_a, space_b


def test_a_space_turn_sees_its_files_plus_the_library(corpus) -> None:
    workspace_id, _user, space_a, _b = corpus
    assert _filenames(workspace_id, "kestrel", space_a) == {
        "kestrel.md",
        "library.md",
    }


def test_a_general_turn_never_sees_a_space_file(corpus) -> None:
    workspace_id, *_ = corpus
    assert _filenames(workspace_id, "kestrel") == {"library.md"}


def test_one_space_never_sees_anothers_files(corpus) -> None:
    workspace_id, _user, _a, space_b = corpus
    assert _filenames(workspace_id, "kestrel", space_b) == {
        "crater.md",
        "library.md",
    }


def test_the_search_sources_tool_obeys_the_same_scope(corpus) -> None:
    """The tool is a second entry into retrieval; unscoped, it is the bypass."""
    workspace_id, user_id, space_a, _b = corpus

    def tool_filenames(space_id: str) -> set[str]:
        db = SessionLocal()
        try:
            result = _search_sources(
                db,
                ToolContext(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    conversation_id="",
                    space_id=space_id,
                ),
                {"query": "kestrel"},
            )
            return {item.filename for item in result.evidence}
        finally:
            db.close()

    assert tool_filenames(space_a) == {"kestrel.md", "library.md"}
    assert tool_filenames("") == {"library.md"}


# --- the grounded-answer tool shares the turn's scope ------------------------


def _grounded_filenames(
    workspace_id: str,
    user_id: str,
    *,
    space_id: str = "",
    conversation_id: str = "",
    args: dict | None = None,
) -> set[str]:
    db = SessionLocal()
    try:
        result = _grounded_answer(
            db,
            ToolContext(
                workspace_id=workspace_id,
                user_id=user_id,
                conversation_id=conversation_id,
                space_id=space_id,
            ),
            {"question": "kestrel", **(args or {})},
        )
        return {item.filename for item in result.evidence}
    finally:
        db.close()


def test_the_grounded_answer_tool_obeys_the_turns_space(corpus) -> None:
    workspace_id, user_id, space_a, _b = corpus
    assert _grounded_filenames(workspace_id, user_id, space_id=space_a) == {
        "kestrel.md",
        "library.md",
    }
    assert _grounded_filenames(workspace_id, user_id) == {"library.md"}


def test_a_model_argument_cannot_widen_a_threads_space_scope(corpus) -> None:
    """`_live_sources` reads a space id as `IN ("", space)` — library UNION
    that space — so on an ordinary space-less thread a model-supplied
    `space_id` WIDENED retrieval instead of narrowing it, and the model writing
    that argument has just read workspace text the taint ledger calls
    untrusted. Widening is the one direction scoping must never fail in.
    """
    workspace_id, user_id, space_a, _b = corpus
    assert _grounded_filenames(
        workspace_id,
        user_id,
        conversation_id="conv-1",
        args={"space_id": space_a},
    ) == {"library.md"}
    # Inside a space, the argument was already ignored; it still is.
    assert _grounded_filenames(
        workspace_id,
        user_id,
        conversation_id="conv-1",
        space_id=space_a,
        args={"space_id": "some-other-space"},
    ) == {"kestrel.md", "library.md"}


def test_a_thread_less_caller_may_still_name_a_space(corpus) -> None:
    """The MCP and REST doors build a context with no conversation, and
    `GroundedAnswerRequest.space_id` is a deliberate caller-chosen scope
    there — so the argument keeps working exactly where it was designed to.
    """
    workspace_id, user_id, space_a, _b = corpus
    assert _grounded_filenames(workspace_id, user_id, args={"space_id": space_a}) == {
        "kestrel.md",
        "library.md",
    }
