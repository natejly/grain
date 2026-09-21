"""The fused-score floor, and the preload it gates.

The evidence preload was unconditional: every turn arrived with the five
best-ranked chunks in the workspace attached, whatever the question. "What's
for lunch" came back with five citations, and a grounded-answer product that
cites five irrelevant passages is worse than one that says the sources do not
cover it.

The floor is READ from config and never derived. That is the sharp edge here:
`retrieval_dense_floor` is a cosine on one arm and this is a sum of
1/(k + rank) terms — with k=60, a chunk ranked first by both arms scores
0.0328, so a value borrowed from the cosine floor would cut everything.
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.auth import DEV_SEED_USER_ID
from app.config import get_settings
from app.database import SessionLocal
from app.models import Chunk, Source, Workspace
from app.services.retrieval import reciprocal_rank_fusion, search_evidence


@pytest.fixture
def workspace(client) -> str:
    db = SessionLocal()
    try:
        row = Workspace(name="retrieval-floor")
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _seed(db: Session, workspace_id: str, passages: list[str]) -> None:
    source = Source(
        workspace_id=workspace_id,
        created_by=DEV_SEED_USER_ID,
        filename="doc.md",
        media_type="text/markdown",
        object_key="/tmp/not-used",
        byte_size=1,
        status="ready",
        chunk_count=len(passages),
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


def test_the_default_admits_everything_today(workspace):
    """0.0 is the deliberate default: nothing about this release changes what a
    turn is handed, because the instrument that could pick a real value — the
    answerability eval — does not exist yet."""
    assert get_settings().retrieval_fused_floor == 0.0
    db = SessionLocal()
    try:
        _seed(db, workspace, ["Project Juniper uses a violet deployment ring."])
        assert search_evidence(db, workspace_id=workspace, query="violet ring")
    finally:
        db.close()


def test_a_floor_above_every_score_returns_nothing(workspace):
    """Returning nothing IS the feature: the model is instructed to say the
    sources do not cover the question when handed no passages."""
    db = SessionLocal()
    try:
        _seed(db, workspace, ["Project Juniper uses a violet deployment ring."])
        assert (
            search_evidence(
                db, workspace_id=workspace, query="violet ring", fused_floor=1.0
            )
            == []
        )
    finally:
        db.close()


def test_a_floor_below_every_score_changes_nothing(workspace):
    db = SessionLocal()
    try:
        _seed(db, workspace, ["Project Juniper uses a violet deployment ring."])
        unfloored = search_evidence(db, workspace_id=workspace, query="violet ring")
        floored = search_evidence(
            db, workspace_id=workspace, query="violet ring", fused_floor=0.0001
        )
        assert [item.chunk_id for item in floored] == [
            item.chunk_id for item in unfloored
        ]
    finally:
        db.close()


def test_the_floor_keeps_the_head_and_drops_the_tail(workspace):
    """Applied against a ranking that is already sorted descending, so the
    first score under the floor ends the list."""
    db = SessionLocal()
    try:
        _seed(
            db,
            workspace,
            [
                "Project Juniper uses a violet deployment ring.",
                "Juniper ships on Thursdays.",
                "The cafeteria menu rotates weekly.",
            ],
        )
        everything = search_evidence(db, workspace_id=workspace, query="juniper violet")
        assert len(everything) >= 2
        cutoff = everything[0].score
        trimmed = search_evidence(
            db, workspace_id=workspace, query="juniper violet", fused_floor=cutoff
        )
        assert [item.chunk_id for item in trimmed] == [everything[0].chunk_id]
    finally:
        db.close()


def test_the_settings_value_is_what_an_unpassed_floor_reads(workspace, monkeypatch):
    """READ from config, not derived and not hard-coded at the call site — so
    the preload and the `search_sources` tool cannot come to disagree."""
    settings = get_settings()
    monkeypatch.setattr(settings, "retrieval_fused_floor", 1.0, raising=False)
    db = SessionLocal()
    try:
        _seed(db, workspace, ["Project Juniper uses a violet deployment ring."])
        assert search_evidence(db, workspace_id=workspace, query="violet ring") == []
    finally:
        db.close()


def test_the_two_floors_are_not_on_the_same_scale():
    """The unit error this parameter exists to make impossible: with k=60 the
    best possible fused score is far below the cosine floor, so borrowing
    `retrieval_dense_floor` here would cut every result there is."""
    settings = get_settings()
    best_possible = reciprocal_rank_fusion(
        [[("chunk", 1.0)], [("chunk", 1.0)]],
        k=settings.retrieval_rrf_k,
        depth=settings.retrieval_fusion_depth,
    )[0][1]
    assert best_possible < settings.retrieval_dense_floor
