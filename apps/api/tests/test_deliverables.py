"""Deliverable runs: the preset's shape, and the manifest's join.

The manifest's entire value is the JOIN — this file came out of that sandbox
session, and the run that made it was holding these queries and these passages.
So the tests are about what that join is allowed to reach: only this run's own
rows, never another's, and never a claim the product cannot back up (a manifest
file is `status="stored"` and is not quotable).
"""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.models import (
    AgentToolCall,
    Conversation,
    CoverageLedger,
    DeliverableManifest,
    ManifestFile,
    Message,
    Run,
    RunCheckpoint,
    Source,
    Workflow,
    WorkflowRun,
    Workspace,
    new_id,
)
from app.services import deliverables
from app.services.llm_tools import ToolContext
from app.services.workflows import parse_graph, validate


def _workspace(db) -> str:
    workspace_id = new_id()
    db.add(Workspace(id=workspace_id, name="Deliverables"))
    db.flush()
    return workspace_id


def _run(db, workspace_id: str) -> Run:
    conversation = Conversation(workspace_id=workspace_id, created_by="")
    db.add(conversation)
    db.flush()
    run = Run(
        id=new_id(),
        workspace_id=workspace_id,
        conversation_id=conversation.id,
        agent_id="",
        created_by="",
        status="running",
        prompt="build it",
    )
    db.add(run)
    db.flush()
    return run


def _download_call(db, run: Run, *, filename: str, session_id: str) -> AgentToolCall:
    """The call row a `sandbox_download` writes, with NO checkpoint behind it.

    The argument key is `session` — the name the tool's own schema advertises,
    so it is what a schema-conformant model emits. (`_session_for` also accepts
    `session_id`, and the manifest reads both, but a fixture that only ever
    wrote the alias is how the key mismatch stayed invisible.)
    """
    call = AgentToolCall(
        workspace_id=run.workspace_id,
        run_id=run.id,
        name="sandbox_download",
        arguments_json=json.dumps({"path": "/out/" + filename, "session": session_id}),
        status="succeeded",
    )
    db.add(call)
    db.flush()
    return call


def _downloaded(db, run: Run, *, filename: str, session_id: str) -> Source:
    source = Source(
        workspace_id=run.workspace_id,
        created_by="",
        filename=filename,
        media_type="text/csv",
        object_key="/x/" + filename,
        byte_size=42,
        # Stored, NOT ready: a manifest file is not retrievable and not quotable.
        status="stored",
    )
    db.add(source)
    db.flush()
    call = _download_call(db, run, filename=filename, session_id=session_id)
    db.add(
        RunCheckpoint(
            workspace_id=run.workspace_id,
            run_id=run.id,
            # The KEYED join: `record_checkpoint` writes this, and it is what
            # pairs a file with the call that made it.
            tool_call_id=call.id,
            tool_name="sandbox_download",
            kind="source",
            reversible=True,
            before_json=json.dumps({"existed": False, "source_ids": [source.id]}),
        )
    )
    db.flush()
    return source


def _searched(db, run: Run, query: str) -> None:
    db.add(
        AgentToolCall(
            workspace_id=run.workspace_id,
            run_id=run.id,
            name="search_sources",
            arguments_json=json.dumps({"query": query}),
            status="succeeded",
        )
    )
    db.flush()


def test_the_preset_graph_is_valid_and_can_never_carry_a_cron():
    """A preset that could arrive with a live schedule would be a scheduled job
    nobody in the workspace agreed to (the WorkflowTemplate doctrine)."""
    graph = deliverables.preset_graph()
    parsed, errors = parse_graph(graph.to_document())
    assert errors == []
    assert parsed is not None
    assert graph.trigger.kind == "manual"
    assert graph.trigger.cron == ""
    assert graph.node_ids() == ["plan", "research", "build", "manifest"]
    assert [edge.source for edge in graph.edges] == ["plan", "research", "build"]
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        db.commit()
        registry = deliverables.registry_tools(
            db, ToolContext(workspace_id=workspace_id, user_id="", conversation_id="")
        )
    finally:
        db.close()
    report = validate.validate_graph(parsed, registry)
    assert report.errors == [], report.errors


def test_record_manifest_joins_files_to_the_sessions_and_evidence_of_its_own_run():
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        run = _run(db, workspace_id)
        workflow = Workflow(
            workspace_id=workspace_id,
            created_by="",
            name=deliverables.PRESET_NAME,
            description="",
            source_prompt="",
            graph_json=json.dumps(deliverables.preset_graph().to_document()),
            status="draft",
            trigger_kind="manual",
        )
        db.add(workflow)
        db.flush()
        workflow_run = WorkflowRun(
            workspace_id=workspace_id,
            workflow_id=workflow.id,
            created_by="",
            workflow_version=1,
            graph_json=workflow.graph_json,
            trigger="manual",
            status="running",
            run_id=run.id,
            input_json="{}",
        )
        db.add(workflow_run)
        db.flush()

        _searched(db, run, "retention window")
        first = _downloaded(db, run, filename="one.csv", session_id="sess-1")
        second = _downloaded(db, run, filename="two.csv", session_id="sess-2")
        db.add(
            Message(
                workspace_id=workspace_id,
                conversation_id=run.conversation_id,
                run_id=run.id,
                role="assistant",
                content="Answered [1].",
                citations_json=json.dumps([{"chunk_id": "chunk-a"}]),
            )
        )

        # ANOTHER run's rows, which must not be reachable from this one.
        other = _run(db, workspace_id)
        _searched(db, other, "somebody else's question")
        _downloaded(db, other, filename="theirs.csv", session_id="sess-9")
        db.commit()

        context = ToolContext(
            workspace_id=workspace_id,
            user_id="",
            conversation_id=run.conversation_id,
            run_id=run.id,
        )
        result = deliverables.record(
            db, context, {"title": "Report", "space_id": "", "question": "q"}
        )
        db.commit()
        assert "2 file(s)" in result.content
        assert "cannot be cited" in result.content

        manifest = db.scalar(
            select(DeliverableManifest).where(
                DeliverableManifest.workspace_id == workspace_id,
                DeliverableManifest.workflow_run_id == workflow_run.id,
            )
        )
        assert manifest is not None
        assert manifest.space_id == ""
        assert manifest.status == "complete"
        files = list(
            db.scalars(
                select(ManifestFile)
                .where(ManifestFile.manifest_id == manifest.id)
                .order_by(ManifestFile.ordinal)
            )
        )
        assert [row.source_id for row in files] == [first.id, second.id]
        assert [row.sandbox_session_id for row in files] == ["sess-1", "sess-2"]
        assert json.loads(files[0].queries_json) == ["retention window"]
        assert json.loads(files[0].chunk_ids_json) == ["chunk-a"]
        # Nothing from the other run leaked in.
        assert "theirs.csv" not in {row.filename for row in files}
        assert "somebody else's question" not in json.loads(files[0].queries_json)
    finally:
        db.close()


def test_record_manifest_refuses_outside_a_run():
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        db.commit()
        result = deliverables.record(
            db,
            ToolContext(workspace_id=workspace_id, user_id="", conversation_id=""),
            {"title": "Report"},
        )
        assert result.content.startswith("Error:")
    finally:
        db.close()


def test_budget_exhaustion_ships_a_partial_manifest_with_its_ledger():
    """A run that ran out still shipped something. Refusing to record the half
    that exists would throw away the honest part."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        run = _run(db, workspace_id)
        workflow = Workflow(
            workspace_id=workspace_id,
            created_by="",
            name=deliverables.PRESET_NAME,
            description="",
            source_prompt="",
            graph_json="{}",
            status="draft",
            trigger_kind="manual",
        )
        db.add(workflow)
        db.flush()
        workflow_run = WorkflowRun(
            workspace_id=workspace_id,
            workflow_id=workflow.id,
            created_by="",
            workflow_version=1,
            graph_json="{}",
            trigger="manual",
            status="failed",
            run_id=run.id,
            input_json=json.dumps(
                {
                    "question": "How long do we keep logs?",
                    "space_id": "",
                    "budget_seconds": 900,
                    "budget_tool_calls": 40,
                }
            ),
        )
        db.add(workflow_run)
        db.flush()
        landed = _downloaded(db, run, filename="partial.csv", session_id="sess-1")
        ledger = CoverageLedger(
            workspace_id=workspace_id,
            run_id=run.id,
            question="How long do we keep logs?",
        )
        db.add(ledger)
        db.commit()

        manifest = deliverables.finalize_partial(
            db, workflow_run=workflow_run, reason="budget_exhausted"
        )
        db.commit()
        assert manifest is not None
        assert manifest.status == "partial"
        assert manifest.ledger_id == ledger.id
        assert manifest.budget_seconds == 900
        assert manifest.budget_tool_calls == 40
        files = list(
            db.scalars(
                select(ManifestFile).where(ManifestFile.manifest_id == manifest.id)
            )
        )
        assert [row.source_id for row in files] == [landed.id]

        # Idempotent: a second halt path must not double-write.
        again = deliverables.finalize_partial(
            db, workflow_run=workflow_run, reason="cancelled"
        )
        db.commit()
        assert again is not None and again.id == manifest.id
        assert (
            len(
                list(
                    db.scalars(
                        select(DeliverableManifest).where(
                            DeliverableManifest.workflow_run_id == workflow_run.id
                        )
                    )
                )
            )
            == 1
        )
    finally:
        db.close()


def test_record_manifest_ships_through_the_ordinary_registry():
    """A tool that bypassed the registry would bypass ToolPolicy, which is what
    makes a scheduled workflow safe to run unattended."""
    from app.services.llm_tools import registry_families

    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        db.commit()
        context = ToolContext(
            workspace_id=workspace_id, user_id="", conversation_id=""
        )
        families = dict(registry_families(db, context))
        assert deliverables.RECORD_MANIFEST in families["deliverables"]
        spec = families["deliverables"][deliverables.RECORD_MANIFEST]
        # It writes rows, so the workspace's policy decides whether an
        # unattended run may do it.
        assert spec.read_only is False
    finally:
        db.close()


def _workflow_run(db, workspace_id: str, run: Run, **overrides) -> WorkflowRun:
    workflow = Workflow(
        workspace_id=workspace_id,
        created_by="",
        name=deliverables.PRESET_NAME,
        description="",
        source_prompt="",
        graph_json="{}",
        status="draft",
        trigger_kind="manual",
    )
    db.add(workflow)
    db.flush()
    fields = {
        "workspace_id": workspace_id,
        "workflow_id": workflow.id,
        "created_by": "",
        "workflow_version": 1,
        "graph_json": "{}",
        "trigger": "manual",
        "status": "running",
        "run_id": run.id,
        "input_json": "{}",
    }
    fields.update(overrides)
    workflow_run = WorkflowRun(**fields)
    db.add(workflow_run)
    db.flush()
    return workflow_run


def test_a_download_that_produced_no_file_does_not_shift_the_next_ones_session():
    """The join is keyed, not positional.

    A `sandbox_download` writes an `AgentToolCall` before anything is decided
    and writes NO checkpoint when it is denied, fails, or comes back with no
    file (a missing path, an oversize file). Pairing the two lists by position
    then pins a real session id to a file it never produced — served to the
    reader as fact, with nothing marking it as a guess.
    """
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        run = _run(db, workspace_id)
        workflow_run = _workflow_run(db, workspace_id, run)
        # First call: parked and denied, so no checkpoint behind it.
        failed = _download_call(db, run, filename="chart.png", session_id="sess-1")
        failed.status = "denied"
        # A filename this suite uses nowhere else: `test_sandbox_tools` looks
        # its own downloads up by filename with no workspace filter, so a name
        # shared across files is a cross-test collision waiting to happen.
        landed = _downloaded(db, run, filename="manifest-join.csv", session_id="sess-2")
        db.commit()

        manifest = deliverables.write_manifest(
            db,
            workspace_id=workspace_id,
            run_id=run.id,
            workflow_run_id=workflow_run.id,
            space_id="",
            title="Report",
            created_by="",
        )
        db.commit()
        files = list(
            db.scalars(
                select(ManifestFile)
                .where(ManifestFile.manifest_id == manifest.id)
                .order_by(ManifestFile.ordinal)
            )
        )
        assert [row.source_id for row in files] == [landed.id]
        assert [row.sandbox_session_id for row in files] == ["sess-2"]
        # And the denied call did not bill against the budget.
        assert manifest.spent_tool_calls == 1
    finally:
        db.close()


def test_a_checkpoint_with_no_resolvable_call_records_a_blank_session():
    """A blank is honest; a neighbouring session id is not."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        run = _run(db, workspace_id)
        workflow_run = _workflow_run(db, workspace_id, run)
        source = Source(
            workspace_id=workspace_id,
            created_by="",
            filename="orphan.csv",
            media_type="text/csv",
            object_key="/x/orphan.csv",
            byte_size=7,
            status="stored",
        )
        db.add(source)
        db.flush()
        _download_call(db, run, filename="other.csv", session_id="sess-1")
        db.add(
            RunCheckpoint(
                workspace_id=workspace_id,
                run_id=run.id,
                # Written before the column existed: no call to resolve.
                tool_call_id="",
                tool_name="sandbox_download",
                kind="source",
                reversible=True,
                before_json=json.dumps({"existed": False, "source_ids": [source.id]}),
            )
        )
        db.commit()

        manifest = deliverables.write_manifest(
            db,
            workspace_id=workspace_id,
            run_id=run.id,
            workflow_run_id=workflow_run.id,
            space_id="",
            title="Report",
            created_by="",
        )
        db.commit()
        files = list(
            db.scalars(
                select(ManifestFile).where(ManifestFile.manifest_id == manifest.id)
            )
        )
        assert [row.sandbox_session_id for row in files] == [""]
    finally:
        db.close()


def test_a_completed_manifest_records_the_budget_and_the_spend():
    """The receipt for the only per-run cost control has to show both numbers.

    A complete manifest used to carry 0/0/0 — `finalize_partial` filled the
    budgets in and the happy path filled in nothing — so the detail view read
    "38 of —" and could not even reveal that the lever was there.
    """
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        run = _run(db, workspace_id)
        workflow_run = _workflow_run(
            db,
            workspace_id,
            run,
            input_json=json.dumps(
                {"question": "q", "budget_seconds": 900, "budget_tool_calls": 40}
            ),
        )
        workflow_run.started_at = workflow_run.created_at - timedelta(seconds=61)
        _searched(db, run, "retention window")
        _downloaded(db, run, filename="one.csv", session_id="sess-1")
        db.commit()

        manifest = deliverables.write_manifest(
            db,
            workspace_id=workspace_id,
            run_id=run.id,
            workflow_run_id=workflow_run.id,
            space_id="",
            title="Report",
            created_by="",
        )
        db.commit()
        assert manifest.budget_seconds == 900
        assert manifest.budget_tool_calls == 40
        assert manifest.spent_seconds >= 61
        assert manifest.spent_tool_calls == 2
    finally:
        db.close()


def test_a_zero_budget_means_no_limit_rather_than_an_instant_halt():
    """`ge=0` is allowed at the boundary, so 0 has to mean something usable."""
    db = SessionLocal()
    try:
        workspace_id = _workspace(db)
        run = _run(db, workspace_id)
        workflow_run = _workflow_run(
            db,
            workspace_id,
            run,
            input_json=json.dumps({"budget_seconds": 0, "budget_tool_calls": 0}),
        )
        db.commit()
        assert deliverables.budgets(workflow_run) == (0, 0)
        # Garbage reads as "no budget" rather than raising inside a receipt.
        workflow_run.input_json = json.dumps({"budget_seconds": "soon"})
        assert deliverables.budgets(workflow_run) == (0, 0)
        workflow_run.input_json = "not json"
        assert deliverables.budgets(workflow_run) == (0, 0)
        assert deliverables.budgets(None) == (0, 0)
    finally:
        db.close()
