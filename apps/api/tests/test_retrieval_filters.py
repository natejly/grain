"""Retrieval parameter parity: narrowing filters, budgets, and stable ids.

THE PIN THAT MATTERS MOST is the first one. Every filter here is optional and
every default is the value the code already used, so a no-argument
`search_evidence` must return exactly what it returned before this module's
subject existed. If that stops being true the retrieval eval's numbers stop
being comparable, and every later claim about a ranking change becomes
unfalsifiable.

The second is the SPACE axis. `SourceFilter` can only ever narrow what
`_live_sources` already permits; naming a foreign space must yield nothing
rather than reach into it. That is a scoping boundary, not a convenience.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from app.auth import DEV_SEED_USER_ID
from app.database import SessionLocal
from app.models import Chunk, Source, Workspace
from app.services.embeddings import content_fingerprint
from app.services.retrieval import (
    BUDGETS,
    Evidence,
    SourceFilter,
    _live_sources,
    budget_for,
    evidence_manifest,
    search_evidence,
)
from app.services.web_search import WebEvidence, revive_evidence


@pytest.fixture
def workspace(client) -> str:
    db = SessionLocal()
    try:
        row = Workspace(name="retrieval-filters")
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _seed(
    db: Session,
    workspace_id: str,
    *,
    filename: str,
    passages: list[str],
    space_id: str = "",
    created_at: datetime | None = None,
) -> str:
    source = Source(
        workspace_id=workspace_id,
        created_by=DEV_SEED_USER_ID,
        filename=filename,
        media_type="text/markdown",
        object_key="/tmp/not-used",
        byte_size=1,
        status="ready",
        chunk_count=len(passages),
        space_id=space_id,
    )
    if created_at is not None:
        source.created_at = created_at
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


# --- (a) identity: the no-filter path is the old path ------------------------


def test_no_filters_returns_the_same_four_predicates():
    """The tuple is four elements, exactly as it was.

    Not "four-ish": appending an always-true predicate would change the SQL and
    therefore the plan, and the whole compatibility claim rests on the SQL being
    the same statement it always was.
    """
    assert len(_live_sources("", "", filters=None)) == 4
    assert len(_live_sources("space-1", "conv-1", filters=None)) == 4
    # An EMPTY filter is the same statement as no filter: "the caller passed a
    # filter object" is not itself a narrowing.
    assert len(_live_sources("", "", filters=SourceFilter())) == 4
    assert SourceFilter().empty is True


def test_a_bare_search_is_unchanged_by_the_new_keywords(workspace):
    db = SessionLocal()
    try:
        _seed(
            db,
            workspace,
            filename="alpha.md",
            passages=["Juniper ships on a violet deployment ring."],
        )
        _seed(
            db,
            workspace,
            filename="beta.md",
            passages=["Juniper's rollback runs from the violet ring."],
        )
        bare = search_evidence(db, workspace_id=workspace, query="violet ring")
        explicit = search_evidence(
            db,
            workspace_id=workspace,
            query="violet ring",
            filters=None,
            per_passage_tokens=0,
        )
        assert [item.chunk_id for item in bare] == [item.chunk_id for item in explicit]
        assert [item.excerpt for item in bare] == [item.excerpt for item in explicit]
    finally:
        db.close()


# --- (b)-(d) allow and deny lists --------------------------------------------


def test_an_allow_list_narrows_to_the_named_sources(workspace):
    db = SessionLocal()
    try:
        first = _seed(
            db, workspace, filename="one.md", passages=["Violet ring alpha note."]
        )
        _seed(db, workspace, filename="two.md", passages=["Violet ring beta note."])
        _seed(db, workspace, filename="three.md", passages=["Violet ring gamma note."])
        found = search_evidence(
            db,
            workspace_id=workspace,
            query="violet ring",
            filters=SourceFilter(source_ids=(first,)),
        )
        assert found
        assert {item.source_id for item in found} == {first}
    finally:
        db.close()


def test_a_deny_list_removes_exactly_that_source(workspace):
    db = SessionLocal()
    try:
        first = _seed(
            db, workspace, filename="one.md", passages=["Violet ring alpha note."]
        )
        second = _seed(
            db, workspace, filename="two.md", passages=["Violet ring beta note."]
        )
        found = search_evidence(
            db,
            workspace_id=workspace,
            query="violet ring",
            filters=SourceFilter(exclude_source_ids=(first,)),
        )
        assert found
        assert {item.source_id for item in found} == {second}
    finally:
        db.close()


def test_deny_beats_allow_for_the_same_id(workspace):
    """An id in both lists yields nothing from it. Deny wins because the two
    predicates are ANDed, and that is the safe direction: a filter pair a
    caller got wrong should return too little, never too much."""
    db = SessionLocal()
    try:
        first = _seed(
            db, workspace, filename="one.md", passages=["Violet ring alpha note."]
        )
        _seed(db, workspace, filename="two.md", passages=["Violet ring beta note."])
        found = search_evidence(
            db,
            workspace_id=workspace,
            query="violet ring",
            filters=SourceFilter(source_ids=(first,), exclude_source_ids=(first,)),
        )
        assert found == []
    finally:
        db.close()


# --- (e)-(f) the space axis is narrow-only -----------------------------------


def test_naming_a_foreign_space_reaches_nothing(workspace):
    """THE SECURITY PIN. A thread in space A that names space B in `spaces`
    must see nothing of B — the intersection with the thread's own scope is
    what makes the axis narrow-only, and an empty IN () matching nothing is the
    correct answer rather than an error the caller could route around."""
    db = SessionLocal()
    try:
        _seed(
            db,
            workspace,
            filename="secret.md",
            passages=["Violet ring rotation is weekly."],
            space_id="space-b",
        )
        found = search_evidence(
            db,
            workspace_id=workspace,
            query="violet ring",
            space_id="space-a",
            filters=SourceFilter(space_ids=("space-b",)),
        )
        assert found == []
        # And unfiltered from space A it is still invisible: the filter did not
        # cause the absence, it merely failed to cause a presence.
        assert (
            search_evidence(
                db, workspace_id=workspace, query="violet ring", space_id="space-a"
            )
            == []
        )
    finally:
        db.close()


def test_naming_your_own_space_means_that_space_and_not_the_library(workspace):
    """`spaces` means the spaces it names, and the library is not one of them.

    DIVERGENCE NOTE: the cluster spec's prose for this case expected the ""
    library to ride along. The predicate as built — and the tool description
    the model reads, "only search sources in these spaces" — say otherwise, and
    the narrower reading is the honest one: a caller who asked for one space
    and silently got the whole workspace library back would have no way to ask
    for what they actually said. Passing "" alongside is how to widen back to
    the library, and the case below pins that.
    """
    db = SessionLocal()
    try:
        owned = _seed(
            db,
            workspace,
            filename="owned.md",
            passages=["Violet ring rotation is weekly."],
            space_id="space-a",
        )
        library = _seed(
            db,
            workspace,
            filename="library.md",
            passages=["Violet ring rotation policy, workspace-wide."],
        )
        found = search_evidence(
            db,
            workspace_id=workspace,
            query="violet ring rotation",
            space_id="space-a",
            filters=SourceFilter(space_ids=("space-a",)),
        )
        assert {item.source_id for item in found} == {owned}
        both = search_evidence(
            db,
            workspace_id=workspace,
            query="violet ring rotation",
            space_id="space-a",
            filters=SourceFilter(space_ids=("space-a", "")),
        )
        assert {item.source_id for item in both} == {owned, library}
    finally:
        db.close()


# --- (g) ingestion window ----------------------------------------------------


def test_the_ingestion_window_is_inclusive_on_both_days(workspace):
    db = SessionLocal()
    try:
        day = datetime(2026, 5, 10, 13, 30, tzinfo=timezone.utc)
        inside = _seed(
            db,
            workspace,
            filename="inside.md",
            passages=["Violet ring note from May."],
            created_at=day,
        )
        _seed(
            db,
            workspace,
            filename="older.md",
            passages=["Violet ring note from March."],
            created_at=day - timedelta(days=60),
        )
        _seed(
            db,
            workspace,
            filename="newer.md",
            passages=["Violet ring note from July."],
            created_at=day + timedelta(days=60),
        )
        window = SourceFilter(
            ingested_after=datetime(2026, 5, 10, 0, 0, tzinfo=timezone.utc),
            ingested_before=datetime(2026, 5, 10, 23, 59, 59, tzinfo=timezone.utc),
        )
        found = search_evidence(
            db, workspace_id=workspace, query="violet ring note", filters=window
        )
        assert {item.source_id for item in found} == {inside}
    finally:
        db.close()


# --- (h) budgets -------------------------------------------------------------


def test_the_default_budget_is_todays_numbers():
    """"" and an unknown name both resolve to medium, and medium IS today.

    The three numbers are asserted literally on purpose: they are the claim
    that picking no budget changes nothing, and a test that read them from the
    same dict it is checking would assert only that a dict is a dict.
    """
    assert budget_for("") is BUDGETS["medium"]
    assert budget_for("nonsense") is BUDGETS["medium"]
    medium = budget_for("medium")
    assert (medium.limit, medium.token_budget, medium.per_passage_tokens) == (
        5,
        1200,
        0,
    )


def test_a_low_budget_returns_less(workspace):
    db = SessionLocal()
    try:
        for index in range(6):
            _seed(
                db,
                workspace,
                filename=f"doc-{index}.md",
                passages=[f"Violet ring note number {index} about the rotation."],
            )
        low = budget_for("low")
        found = search_evidence(
            db,
            workspace_id=workspace,
            query="violet ring rotation",
            limit=low.limit,
            token_budget=low.token_budget,
            per_passage_tokens=low.per_passage_tokens,
        )
        assert len(found) <= 3
        assert sum(len(item.excerpt.split()) for item in found) <= 400
    finally:
        db.close()


def test_the_per_passage_cap_is_off_on_the_default_path(workspace):
    """A long passage is untruncated at medium and clipped at high.

    This is the whole reason `per_passage_tokens` defaults to 0: a cap on the
    default path would rewrite every existing answer and every eval number.
    """
    db = SessionLocal()
    try:
        long_passage = "violet ring " + " ".join(f"word{index}" for index in range(600))
        _seed(db, workspace, filename="long.md", passages=[long_passage])
        medium = budget_for("medium")
        uncapped = search_evidence(
            db,
            workspace_id=workspace,
            query="violet ring",
            limit=medium.limit,
            token_budget=medium.token_budget,
            per_passage_tokens=medium.per_passage_tokens,
        )
        high = budget_for("high")
        capped = search_evidence(
            db,
            workspace_id=workspace,
            query="violet ring",
            limit=high.limit,
            token_budget=high.token_budget,
            per_passage_tokens=high.per_passage_tokens,
        )
        assert len(uncapped[0].excerpt.split()) > 400
        assert len(capped[0].excerpt.split()) <= 400
    finally:
        db.close()


# --- (i)-(k) stable ids ------------------------------------------------------


def test_the_fingerprint_is_of_the_content_and_moves_when_it_does(workspace):
    """Of `chunk.content`, not `indexed_text`: content is what gets quoted, and
    therefore what a later validator has to be able to re-verify."""
    db = SessionLocal()
    try:
        source_id = _seed(
            db,
            workspace,
            filename="fp.md",
            passages=["Violet ring rotation is weekly."],
        )
        found = search_evidence(db, workspace_id=workspace, query="violet ring")
        assert found
        assert found[0].chunk_fingerprint == content_fingerprint(
            "Violet ring rotation is weekly."
        )
        before = found[0].chunk_fingerprint
        chunk = (
            db.query(Chunk)
            .filter(Chunk.source_id == source_id, Chunk.ordinal == 0)
            .one()
        )
        chunk.content = "Violet ring rotation is daily now."
        db.commit()
        after = search_evidence(db, workspace_id=workspace, query="violet ring")
        assert after[0].chunk_fingerprint != before
    finally:
        db.close()


def test_the_manifest_numbers_from_one_in_returned_order(workspace):
    db = SessionLocal()
    try:
        for index in range(3):
            _seed(
                db,
                workspace,
                filename=f"m-{index}.md",
                passages=[f"Violet ring manifest note {index}."],
            )
        found = search_evidence(db, workspace_id=workspace, query="violet ring manifest")
        manifest = evidence_manifest(found)
        assert [row["n"] for row in manifest] == list(range(1, len(found) + 1))
        assert [row["chunk_id"] for row in manifest] == [
            item.chunk_id for item in found
        ]
        assert [row["fingerprint"] for row in manifest] == [
            item.chunk_fingerprint for item in found
        ]
    finally:
        db.close()


def test_revive_evidence_round_trips_both_classes():
    """The new field is trailing and defaulted, so `WebEvidence.url` still
    follows it — the reason that ordering was chosen, pinned."""
    indexed = Evidence(
        chunk_id="chunk-1",
        source_id="source-1",
        filename="brief.md",
        ordinal=0,
        excerpt="Maya owns the launch.",
        score=1.0,
        chunk_fingerprint="abc123",
    )
    web = WebEvidence(
        chunk_id="",
        source_id="",
        filename="example.com",
        ordinal=0,
        excerpt="A web passage.",
        score=0.5,
        url="https://example.com/a",
    )
    revived_indexed = revive_evidence(
        {
            "chunk_id": indexed.chunk_id,
            "source_id": indexed.source_id,
            "filename": indexed.filename,
            "ordinal": indexed.ordinal,
            "excerpt": indexed.excerpt,
            "score": indexed.score,
            "chunk_fingerprint": indexed.chunk_fingerprint,
        }
    )
    assert revived_indexed == indexed
    assert not isinstance(revived_indexed, WebEvidence)
    revived_web = revive_evidence(
        {
            "chunk_id": web.chunk_id,
            "source_id": web.source_id,
            "filename": web.filename,
            "ordinal": web.ordinal,
            "excerpt": web.excerpt,
            "score": web.score,
            "url": web.url,
        }
    )
    assert isinstance(revived_web, WebEvidence)
    assert revived_web.url == web.url
    # A payload written before the field existed still revives: defaulted.
    assert (
        revive_evidence(
            {
                "chunk_id": "c",
                "source_id": "s",
                "filename": "f",
                "ordinal": 0,
                "excerpt": "e",
                "score": 1.0,
            }
        ).chunk_fingerprint
        == ""
    )
