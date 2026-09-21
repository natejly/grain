"""The coverage ledger: the classifier, the forced pass, and the byte-stable report.

The classifier and both counter-queries are pure functions, and these tests are
written in those terms. A model-written classifier would inherit exactly the
one-sidedness the counter-evidence pass exists to catch, so "pure" is the
property under test and not an implementation detail.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Chunk, CoverageEntry, Run, Source, Workspace, new_id
from app.services import coverage, retrieval, runs, step_plan
from app.services.llm_tools import ToolContext

PLAIN_QUESTIONS = [
    "How long are raw event logs kept?",
    "Who owns the launch?",
    "What does the Best Practices doc say about deletion?",
    "When does the rotation hand over?",
    "List the archive buckets.",
    "What is the retention window?",
]


@pytest.mark.parametrize("marker", coverage.DEBATE_MARKERS)
def test_every_marker_classifies_debate(marker: str):
    """Table-driven over the constant itself, so a marker added without a
    thought about how it reads in a sentence fails here first."""
    question = f"Tell me{marker}about the two options"
    assert coverage.classify(question) == "debate", question


@pytest.mark.parametrize("question", PLAIN_QUESTIONS)
def test_plain_questions_classify_plain(question: str):
    assert coverage.classify(question) == "plain"


def test_the_best_practices_trap_classifies_plain():
    """THE TRAP. 'the Best Practices doc' contains ' best ' with spaces on both
    sides, so space-padding does not save a superlative — which is why the
    markers are all comparatives."""
    assert (
        coverage.classify("What does the Best Practices doc say about deletion?")
        == "plain"
    )


def test_a_comparison_classifies_debate():
    assert coverage.classify("Postgres versus DuckDB for the ledger?") == "debate"
    assert coverage.classify("Is Postgres better than DuckDB?") == "debate"


def test_counter_queries_are_fixed_templates():
    supporting, against = coverage.counter_queries("we should move to DuckDB")
    assert supporting == "evidence supporting: we should move to DuckDB"
    assert against == "evidence against: we should move to DuckDB"


def _workspace(db) -> str:
    workspace_id = new_id()
    db.add(Workspace(id=workspace_id, name="Coverage"))
    db.flush()
    return workspace_id


def _source(db, workspace_id: str, *, filename: str, text: str, space_id: str = ""):
    source = Source(
        workspace_id=workspace_id,
        created_by="",
        filename=filename,
        media_type="text/markdown",
        object_key="/x/" + filename,
        byte_size=len(text),
        status="ready",
        space_id=space_id,
    )
    db.add(source)
    db.flush()
    chunk = Chunk(
        workspace_id=workspace_id,
        source_id=source.id,
        ordinal=0,
        content=text,
        char_start=0,
        char_end=len(text),
        token_count=len(text.split()),
    )
    db.add(chunk)
    db.flush()
    retrieval.index_chunks(db, [chunk])
    return source, chunk


def test_a_debate_ledger_always_runs_the_counter_evidence_pass():
    """The FORCED step: it runs whether or not the model asked for either side."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        _source(
            db,
            workspace_id,
            filename="engines.md",
            text=(
                "Postgres is better for the transactional ledger because it is "
                "backed up hourly. DuckDB is better for analytics because it "
                "reads Parquet extracts directly."
            ),
        )
        db.commit()
        ledger = coverage.open_ledger(
            db,
            workspace_id=workspace_id,
            question="Is Postgres better than DuckDB for the ledger?",
        )
        assert ledger.shape == "debate"
        coverage.counter_evidence_pass(db, ledger=ledger)
        coverage.close_ledger(db, ledger=ledger)
        db.commit()
        stances = [
            entry.stance for entry in coverage.entries_for(db, ledger=ledger)
        ]
        assert stances == ["for", "against"]
    finally:
        db.close()


def test_a_plain_ledger_runs_no_counter_evidence_pass():
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        _source(db, workspace_id, filename="retention.md", text="Logs are kept 90 days.")
        db.commit()
        ledger = coverage.open_ledger(
            db, workspace_id=workspace_id, question="How long are logs kept?"
        )
        coverage.counter_evidence_pass(db, ledger=ledger)
        db.commit()
        assert coverage.entries_for(db, ledger=ledger) == []
    finally:
        db.close()


def test_one_sided_is_true_when_a_stance_found_nothing_and_the_report_says_so():
    """The whole section, byte for byte — the renderer is a contract."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        db.commit()
        ledger = coverage.open_ledger(
            db,
            workspace_id=workspace_id,
            question="Is Postgres better than DuckDB?",
        )
        # No corpus at all, so both stances find nothing.
        coverage.counter_evidence_pass(db, ledger=ledger)
        coverage.record_step(
            db,
            ledger=ledger,
            sub_question="Which engine holds the audit trail?",
            evidence=[],
        )
        coverage.close_ledger(db, ledger=ledger)
        db.commit()
        assert ledger.one_sided is True
        assert coverage.render(db, ledger=ledger) == (
            "## Coverage\n"
            "\n"
            "- Sub-questions posed: 1\n"
            "- Sources consulted: 0 of 0 in scope\n"
            "- Unsupported sub-questions: 1\n"
            "  - “Which engine holds the audit trail?” — no passage was "
            "recorded for this.\n"
            "\n"
            "### Both sides\n"
            "\n"
            "- Supporting passages: 0\n"
            "- Opposing passages: 0\n"
            "\n"
            "The corpus is one-sided on this question: no counter-evidence was "
            "found.\n"
        )
    finally:
        db.close()


def test_a_plain_ledger_emits_no_both_sides_block():
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        source, chunk = _source(
            db, workspace_id, filename="retention.md", text="Logs are kept 90 days."
        )
        db.commit()
        ledger = coverage.open_ledger(
            db, workspace_id=workspace_id, question="How long are logs kept?"
        )
        coverage.record_step(
            db,
            ledger=ledger,
            sub_question="How long are logs kept?",
            evidence=[
                retrieval.Evidence(
                    chunk_id=chunk.id,
                    source_id=source.id,
                    filename=source.filename,
                    ordinal=0,
                    excerpt=chunk.content,
                    score=1.0,
                )
            ],
        )
        coverage.close_ledger(db, ledger=ledger)
        db.commit()
        rendered = coverage.render(db, ledger=ledger)
        assert "### Both sides" not in rendered
        assert "- Unsupported sub-questions: 0\n" in rendered
        assert ledger.consulted_count == 1
        assert ledger.one_sided is False
    finally:
        db.close()


def test_in_scope_count_matches_retrieval_and_a_space_counts_fewer():
    """The ledger's denominator is retrieval's own answer to "what is in scope"."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        space_id = new_id()
        _source(db, workspace_id, filename="library.md", text="Library passage.")
        _source(
            db,
            workspace_id,
            filename="scoped.md",
            text="Scoped passage.",
            space_id=space_id,
        )
        db.commit()
        workspace_wide = coverage.open_ledger(
            db, workspace_id=workspace_id, question="What is in scope?"
        )
        scoped = coverage.open_ledger(
            db,
            workspace_id=workspace_id,
            question="What is in scope?",
            space_id=space_id,
        )
        db.commit()
        assert workspace_wide.in_scope_count == retrieval.in_scope_chunk_count(
            db, workspace_id=workspace_id
        )
        # The library alone, versus the library PLUS the space's own file.
        assert workspace_wide.in_scope_count == 1
        assert scoped.in_scope_count == 2
    finally:
        db.close()


def test_entries_record_the_chunk_and_source_ids_they_leaned_on():
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        source, chunk = _source(
            db, workspace_id, filename="x.md", text="A passage worth citing."
        )
        db.commit()
        ledger = coverage.open_ledger(
            db, workspace_id=workspace_id, question="What is cited?"
        )
        entry = coverage.record_step(
            db,
            ledger=ledger,
            sub_question="What is cited?",
            evidence=[
                retrieval.Evidence(
                    chunk_id=chunk.id,
                    source_id=source.id,
                    filename="x.md",
                    ordinal=0,
                    excerpt="A passage",
                    score=1.0,
                )
            ],
        )
        db.commit()
        assert json.loads(entry.chunk_ids_json) == [chunk.id]
        assert json.loads(entry.source_ids_json) == [source.id]
        assert entry.supported is True
        assert entry.ordinal == 0
        stored = db.scalar(
            select(CoverageEntry).where(CoverageEntry.id == entry.id)
        )
        assert stored is not None and stored.workspace_id == workspace_id
    finally:
        db.close()


# --- the denominator counts documents, like the numerator --------------------


def _many_chunk_source(db, workspace_id: str, *, filename: str, passages: list[str]):
    source = Source(
        workspace_id=workspace_id,
        created_by="",
        filename=filename,
        media_type="text/markdown",
        object_key="/x/" + filename,
        byte_size=sum(len(text) for text in passages),
        status="ready",
        chunk_count=len(passages),
    )
    db.add(source)
    db.flush()
    chunks = []
    for ordinal, text in enumerate(passages):
        chunk = Chunk(
            workspace_id=workspace_id,
            source_id=source.id,
            ordinal=ordinal,
            content=text,
            char_start=0,
            char_end=len(text),
            token_count=len(text.split()),
        )
        db.add(chunk)
        chunks.append(chunk)
    db.flush()
    retrieval.index_chunks(db, chunks)
    return source, chunks


def test_sources_consulted_is_counted_in_documents_at_both_ends():
    """The units error the existing fixtures could not see.

    `close_ledger` counts distinct `Evidence.source_id` — documents — while the
    denominator came from `in_scope_chunk_count`, whose own docstring says
    passages. Every fixture here used one-sentence sources, where one document
    is one chunk and the two counters are indistinguishable; a real corpus runs
    to tens of chunks per document, so the report read "6 of 4,000 sources
    consulted" about a run that had read six of forty. The denominator is
    frozen at open time, so a row written wrong stays wrong.
    """
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        source, chunks = _many_chunk_source(
            db,
            workspace_id,
            filename="retention.md",
            passages=[f"Retention policy paragraph number {n}." for n in range(12)],
        )
        db.commit()
        ledger = coverage.open_ledger(
            db, workspace_id=workspace_id, question="How long are logs kept?"
        )
        coverage.record_step(
            db,
            ledger=ledger,
            sub_question="How long are logs kept?",
            evidence=[
                retrieval.Evidence(
                    chunk_id=chunk.id,
                    source_id=source.id,
                    filename=source.filename,
                    ordinal=index,
                    excerpt=chunk.content,
                    score=1.0,
                )
                for index, chunk in enumerate(chunks[:3])
            ],
        )
        coverage.close_ledger(db, ledger=ledger)
        db.commit()

        assert ledger.in_scope_count == 1
        assert ledger.consulted_count == 1
        assert "- Sources consulted: 1 of 1 in scope\n" in coverage.render(
            db, ledger=ledger
        )
        # And the passage count is still available under its own name, for a
        # caller that genuinely wants passages.
        assert retrieval.in_scope_chunk_count(db, workspace_id=workspace_id) == 12
    finally:
        db.close()


def test_close_ledger_counts_what_the_run_read_as_well_as_what_a_step_named():
    """A plan step reports the passages it says it used; a step that names none
    is not evidence that the run read nothing. The producer passes the turn's
    own evidence, and the numerator is the union."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        first, first_chunk = _source(
            db, workspace_id, filename="one.md", text="The first passage."
        )
        second, second_chunk = _source(
            db, workspace_id, filename="two.md", text="The second passage."
        )
        db.commit()
        ledger = coverage.open_ledger(
            db, workspace_id=workspace_id, question="What did the run read?"
        )
        coverage.record_step(
            db,
            ledger=ledger,
            sub_question="A step that named one passage",
            evidence=[
                retrieval.Evidence(
                    chunk_id=first_chunk.id,
                    source_id=first.id,
                    filename="one.md",
                    ordinal=0,
                    excerpt="The first passage.",
                    score=1.0,
                )
            ],
        )
        coverage.close_ledger(
            db,
            ledger=ledger,
            consulted_evidence=[
                retrieval.Evidence(
                    chunk_id=second_chunk.id,
                    source_id=second.id,
                    filename="two.md",
                    ordinal=0,
                    excerpt="The second passage.",
                    score=1.0,
                )
            ],
        )
        db.commit()
        assert ledger.consulted_count == 2
    finally:
        db.close()


# --- the producer ------------------------------------------------------------


def test_a_plan_mode_run_leaves_a_ledger_the_drawer_can_read(client):
    """END TO END through the real path, with nothing hand-built.

    The gap this closes: `open_ledger`, `record_step`, `counter_evidence_pass`
    and `close_ledger` had no caller anywhere in the app, so
    `GET /api/runs/{id}/coverage` could only 404 and the drawer under every
    answer could only say "No coverage recorded for this run" — while three
    docstrings claimed a live producer. Every other test in this file builds
    the ledger itself, which is exactly the writer production lacked, so none
    of them could fail.
    """
    identity = client.get("/api/bootstrap").json()["identity"]
    workspace_id = identity["workspace_id"]
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "coverage-" + new_id()},
        json={"title": "Coverage"},
    ).json()

    db = SessionLocal()
    try:
        source, chunk = _source(
            db,
            workspace_id,
            filename="retention.md",
            text="Raw event logs are kept for ninety days and then deleted.",
        )
        run = Run(
            workspace_id=workspace_id,
            conversation_id=conversation["id"],
            agent_id=client.get("/api/bootstrap").json()["default_agent_id"],
            created_by=identity["user_id"],
            status="running",
            prompt="How long are raw event logs kept?",
            step_plan=True,
        )
        db.add(run)
        db.commit()
        run_id, source_id, chunk_id = run.id, source.id, chunk.id

        context = ToolContext(
            workspace_id=workspace_id,
            user_id=identity["user_id"],
            conversation_id=conversation["id"],
            run_id=run_id,
        )
        submitted = step_plan._submit_plan(
            db,
            context,
            {
                "steps": [
                    {
                        "question": "How long are raw event logs kept?",
                        "queries": ["retention window for raw event logs"],
                    },
                    {
                        "question": "Who approves an exception?",
                        "queries": ["retention exception approval"],
                    },
                ]
            },
        )
        assert "next_step" in submitted.content
        step_plan._complete_step(
            db,
            context,
            {"step": 1, "summary": "Ninety days.", "chunk_ids": [chunk_id]},
        )
        # The second step names nothing — the ordinary case, and the one the
        # report has to describe without claiming the corpus was empty.
        step_plan._complete_step(db, context, {"step": 2, "summary": "Unclear."})
        row = db.get(Run, run_id)
        db.expunge(row)
    finally:
        db.close()

    runs._finish_run(
        row,
        answer="Raw event logs are kept for ninety days [1].",
        evidence=[
            retrieval.Evidence(
                chunk_id=chunk_id,
                source_id=source_id,
                filename="retention.md",
                ordinal=0,
                excerpt="Raw event logs are kept for ninety days and then deleted.",
                score=1.0,
            )
        ],
        already_streamed=True,
    )

    response = client.get(f"/api/runs/{run_id}/coverage")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [entry["sub_question"] for entry in body["entries"]] == [
        "How long are raw event logs kept?",
        "Who approves an exception?",
    ]
    assert body["entries"][0]["supported"] is True
    assert body["entries"][1]["supported"] is False
    assert body["consulted_count"] == 1
    assert body["in_scope_count"] >= 1
    assert "- Sub-questions posed: 2" in body["report_markdown"]
    assert "no passage was recorded for this" in body["report_markdown"]


def test_a_turn_without_a_plan_leaves_no_ledger(client):
    """An ordinary turn has no declared sub-questions, and inventing them from
    the prompt would be the report making up its own subject. The drawer's
    empty state is the honest answer there."""
    identity = client.get("/api/bootstrap").json()["identity"]
    conversation = client.post(
        "/api/conversations",
        headers={"Idempotency-Key": "coverage-plain-" + new_id()},
        json={"title": "Coverage plain"},
    ).json()
    db = SessionLocal()
    try:
        run = Run(
            workspace_id=identity["workspace_id"],
            conversation_id=conversation["id"],
            agent_id=client.get("/api/bootstrap").json()["default_agent_id"],
            created_by=identity["user_id"],
            status="running",
            prompt="Who owns the launch?",
        )
        db.add(run)
        db.commit()
        run_id = run.id
        db.expunge(run)
        row = run
    finally:
        db.close()

    runs._finish_run(
        row, answer="Maya Chen owns it.", evidence=[], already_streamed=True
    )
    assert client.get(f"/api/runs/{run_id}/coverage").status_code == 404
