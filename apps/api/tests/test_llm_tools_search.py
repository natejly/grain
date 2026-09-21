"""The agent-facing half of retrieval parity: `search_sources`' new arguments
and the `list_sources` tool that makes them fillable.

THE COMPATIBILITY PIN is the bare call. A `{"query": "..."}` call must produce
exactly today's result and today's no-results string, because that is the call
every existing turn makes and the one every eval script measures.

THE USABILITY POINT is `list_sources`. Allow and deny lists are parameters the
model has no way to fill otherwise — nothing else in the registry ever says a
source id out loud — and a listing scoped by a second, hand-written copy of the
scope predicate would name ids the search cannot reach.
"""
from __future__ import annotations

import json

from conftest import create_identity

from app.database import SessionLocal
from app.models import Chunk, Source
from app.services.llm_tools import (
    LIST_SOURCES,
    MAX_LISTED_FILENAME_CHARS,
    MAX_LISTED_SOURCES,
    MAX_RESULT_CHARS,
    SEARCH_SOURCES,
    ToolContext,
    _list_sources,
    _search_sources,
    registry_families,
)

CORPUS = "The violet deployment ring rotates every Tuesday at noon."


def _seed(
    workspace_id: str,
    *,
    filename: str,
    passages: list[str],
    space_id: str = "",
    conversation_id: str = "",
    status: str = "ready",
    deleted: bool = False,
) -> str:
    db = SessionLocal()
    try:
        source = Source(
            workspace_id=workspace_id,
            created_by="seed",
            filename=filename,
            media_type="text/markdown",
            object_key="/tmp/not-used",
            byte_size=1,
            status=status,
            chunk_count=len(passages),
            space_id=space_id,
            conversation_id=conversation_id,
        )
        if deleted:
            from app.clock import utcnow

            source.deleted_at = utcnow()
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
        return source.id
    finally:
        db.close()


def _context(workspace_id: str, **kwargs) -> ToolContext:
    return ToolContext(
        workspace_id=workspace_id,
        user_id="tester",
        conversation_id=kwargs.pop("conversation_id", ""),
        run_id="",
        **kwargs,
    )


def _call(context: ToolContext, args: dict):
    db = SessionLocal()
    try:
        return _search_sources(db, context, args)
    finally:
        db.close()


# --- the bare call is today's call -------------------------------------------


def test_a_bare_call_returns_todays_result(client):
    identity = create_identity(workspace_name="Search bare")
    _seed(identity.workspace_id, filename="ring.md", passages=[CORPUS])
    result = _call(_context(identity.workspace_id), {"query": "violet ring"})
    assert result.evidence
    assert result.content == ""


def test_the_no_results_string_is_verbatim_without_filters(client):
    """Two different facts, said differently: "nothing is indexed that answers
    this" versus "nothing the filters admitted does". A model told the first
    when the second is true retries the same query forever."""
    identity = create_identity(workspace_name="Search empty")
    _seed(identity.workspace_id, filename="ring.md", passages=[CORPUS])
    context = _context(identity.workspace_id)
    bare = _call(context, {"query": "quantum flux capacitor alignment"})
    assert bare.content == "No matching passages in the indexed sources."
    filtered = _call(
        context, {"query": "violet ring", "sources": ["no-such-source-id"]}
    )
    assert filtered.content == (
        "No matching passages in the indexed sources under the filters you named."
    )


# --- the budget ladder --------------------------------------------------------


def test_an_explicit_budget_beats_the_turns_and_the_turns_beats_unset(client):
    identity = create_identity(workspace_name="Search budget")
    for index in range(8):
        _seed(
            identity.workspace_id,
            filename=f"ring-{index}.md",
            passages=[f"{CORPUS} Note {index}."],
        )
    unset = _call(_context(identity.workspace_id), {"query": "violet ring"})
    assert len(unset.evidence) == 5  # medium

    turn_low = _call(
        _context(identity.workspace_id, retrieval_budget="low"), {"query": "violet ring"}
    )
    assert len(turn_low.evidence) == 3

    # The call's own budget wins over the turn's.
    call_high = _call(
        _context(identity.workspace_id, retrieval_budget="low"),
        {"query": "violet ring", "budget": "high"},
    )
    assert len(call_high.evidence) > 3


def test_a_bogus_budget_is_ignored_rather_than_refused(client):
    """A bogus name falls back to the turn's budget instead of erroring, the
    same forgiving rule `_clamped_limit` follows: a tool argument the model got
    slightly wrong should cost accuracy, never the turn."""
    identity = create_identity(workspace_name="Search bogus budget")
    for index in range(8):
        _seed(
            identity.workspace_id,
            filename=f"ring-{index}.md",
            passages=[f"{CORPUS} Note {index}."],
        )
    result = _call(
        _context(identity.workspace_id), {"query": "violet ring", "budget": "enormous"}
    )
    assert len(result.evidence) == 5


# --- malformed filters are answers, not exceptions ----------------------------


def test_malformed_filters_come_back_as_model_readable_errors(client):
    identity = create_identity(workspace_name="Search bad filters")
    _seed(identity.workspace_id, filename="ring.md", passages=[CORPUS])
    context = _context(identity.workspace_id)
    cases = [
        ({"query": "x", "sources": "not-a-list"}, "`sources` must be a list of ids."),
        ({"query": "x", "sources": [1, 2]}, "`sources` must be a list of id strings."),
        ({"query": "x", "sources": ["id"] * 21}, "`sources` takes at most"),
        (
            {"query": "x", "exclude_sources": "nope"},
            "`exclude_sources` must be a list of ids.",
        ),
        (
            {"query": "x", "ingested_after": "last tuesday"},
            "`ingested_after` must be an ISO date like 2026-09-01.",
        ),
        (
            {"query": "x", "ingested_before": 20260901},
            "`ingested_before` must be an ISO date like 2026-09-01.",
        ),
    ]
    for args, expected in cases:
        result = _call(context, args)
        assert result.content.startswith("Error: "), args
        assert expected in result.content, args
        assert result.evidence == []


# --- list_sources is scoped by the same predicate the search uses -------------


def _list(context: ToolContext, args: dict | None = None) -> list[dict]:
    db = SessionLocal()
    try:
        return json.loads(_list_sources(db, context, args or {}).content)
    finally:
        db.close()


def test_a_listing_never_names_what_the_search_cannot_reach(client):
    """Every exclusion here is one the ranking arms already make. A listing that
    disagreed would have the model building allow-lists out of ids that
    silently match nothing."""
    identity = create_identity(workspace_name="Listing scope")
    other = create_identity(workspace_name="Listing other tenant")
    visible = _seed(identity.workspace_id, filename="visible.md", passages=[CORPUS])
    _seed(other.workspace_id, filename="foreign.md", passages=[CORPUS])
    _seed(
        identity.workspace_id,
        filename="pending.md",
        passages=[CORPUS],
        status="processing",
    )
    _seed(
        identity.workspace_id, filename="gone.md", passages=[CORPUS], deleted=True
    )
    _seed(
        identity.workspace_id,
        filename="space.md",
        passages=[CORPUS],
        space_id="space-b",
    )
    _seed(
        identity.workspace_id,
        filename="attached.md",
        passages=[CORPUS],
        conversation_id="another-thread",
    )
    listed = _list(_context(identity.workspace_id))
    assert [row["id"] for row in listed] == [visible]
    row = listed[0]
    assert row["filename"] == "visible.md"
    assert row["chunks"] == 1
    assert row["space_id"] == ""
    assert row["ingested"]


def test_a_listing_matches_filenames_case_insensitively(client):
    identity = create_identity(workspace_name="Listing needle")
    _seed(identity.workspace_id, filename="Quarterly Ring Review.md", passages=[CORPUS])
    _seed(identity.workspace_id, filename="unrelated.md", passages=[CORPUS])
    context = _context(identity.workspace_id)
    assert len(_list(context, {"query": "ring"})) == 1
    assert len(_list(context, {"query": "RING"})) == 1
    assert _list(context, {"query": "nothing-like-this"}) == []


def test_a_full_listing_stays_parseable_inside_the_tool_budget(client):
    """The worst case the caps allow — 50 rows of 120-character filenames —
    serializes to roughly 12,000 characters, three times the budget. The
    listing therefore stops at whole rows: a short array is readable and an
    array clipped mid-object is not."""
    identity = create_identity(workspace_name="Listing budget")
    for index in range(MAX_LISTED_SOURCES + 5):
        _seed(
            identity.workspace_id,
            filename=f"{index:03d}-" + "n" * 200 + ".md",
            passages=[CORPUS],
        )
    db = SessionLocal()
    try:
        result = _list_sources(db, _context(identity.workspace_id), {})
    finally:
        db.close()
    assert len(result.content) <= MAX_RESULT_CHARS
    rows = json.loads(result.content)  # parses, which is the point
    assert 0 < len(rows) < MAX_LISTED_SOURCES
    assert all(len(row["filename"]) <= MAX_LISTED_FILENAME_CHARS for row in rows)
    # Newest first, so what survives the budget is what the model most likely
    # wants; the rest is reachable through `query`.
    assert rows[0]["filename"].startswith("054-")


def test_a_small_listing_is_not_truncated(client):
    identity = create_identity(workspace_name="Listing small")
    for index in range(3):
        _seed(identity.workspace_id, filename=f"doc-{index}.md", passages=[CORPUS])
    assert len(_list(_context(identity.workspace_id))) == 3


def test_list_sources_ships_in_the_core_family(client):
    """In `core`, so a document-subject thread still gets it — the addressing
    tool has to travel with the search tool or the filters are unusable exactly
    where scoping matters most."""
    db = SessionLocal()
    try:
        families = dict(registry_families(db, _context("workspace-does-not-matter")))
        assert LIST_SOURCES in families["core"]
        assert SEARCH_SOURCES in families["core"]
    finally:
        db.close()
