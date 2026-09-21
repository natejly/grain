"""Deliverable runs: a fixed research-to-artefact workflow, and its manifest.

Every moving part already exists — the DAG grammar, the agent node, the
sandbox, the `Source` rows `sandbox_download` writes, the `AgentToolCall`
record of what actually ran. The only thing missing was the JOIN: which file
came out of which sandbox session, and which queries and passages the run was
holding when it produced them. That join is the manifest, and it is the whole
of this module's claim.

THE PRESET IS A FIXED GRAPH, never compiled by a model. Nothing drifts, there
is no repair pass, and a reviewer reads four nodes rather than whatever the
compiler produced today. Its trigger is `manual` with empty schedule fields,
because the `WorkflowTemplate` doctrine holds here too: a preset may never
smuggle a live cron into a workspace.

MANIFEST FILES ARE NOT QUOTABLE. `sandbox_download` writes its `Source` rows
with `status="stored"` rather than `"ready"`, precisely so retrieval cannot
cite them. A manifest names those files and says where they came from; it must
never imply the corpus can quote them back.

Nothing here commits; the route and the executor own their transactions.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clock import utcnow
from ..models import (
    AgentToolCall,
    CoverageLedger,
    DeliverableManifest,
    ManifestFile,
    Message,
    RunCheckpoint,
    Source,
    Workflow,
    WorkflowRun,
)
from .llm_tools import ToolContext, ToolResult, ToolSpec
from .workflows.dag import EdgeSpec, InputSpec, NodeSpec, TriggerSpec, WorkflowGraph

logger = logging.getLogger(__name__)

#: The preset's name, and the marker the executor's halt path reads to decide
#: whether a failed run still owes somebody a partial manifest.
PRESET_NAME = "Deliverable run"
DEFAULT_BUDGET_SECONDS = 900
DEFAULT_BUDGET_TOOL_CALLS = 40

RECORD_MANIFEST = "record_manifest"


def preset_graph() -> WorkflowGraph:
    """plan → research → build → manifest. Fixed, and valid by construction."""
    return WorkflowGraph(
        name=PRESET_NAME,
        description=(
            "Plan the sub-questions, research them against the workspace, build "
            "the artefacts in the sandbox, and record a manifest naming every "
            "file and the evidence behind it."
        ),
        # A preset may NEVER carry a cron: a template that could arrive with a
        # live schedule would be a scheduled job nobody in this workspace
        # agreed to.
        trigger=TriggerSpec(kind="manual", cron="", timezone="UTC"),
        inputs=[
            InputSpec(
                name="question",
                type="string",
                label="What should this deliverable answer?",
                required=True,
            ),
            InputSpec(
                name="space_id",
                type="string",
                label="Space",
                description="Leave blank for the workspace library.",
                required=False,
                default="",
            ),
            InputSpec(
                name="budget_seconds",
                type="integer",
                label="Time budget (seconds)",
                required=False,
                default=DEFAULT_BUDGET_SECONDS,
            ),
            InputSpec(
                name="budget_tool_calls",
                type="integer",
                label="Tool-call budget",
                required=False,
                default=DEFAULT_BUDGET_TOOL_CALLS,
            ),
        ],
        nodes=[
            NodeSpec(
                id="plan",
                kind="agent",
                description="List the sub-questions this deliverable needs answered.",
                prompt=(
                    "List the sub-questions this deliverable needs answered, one "
                    "per line. Do not answer them.\n\n"
                    "Deliverable: {{ input.question }}"
                ),
            ),
            NodeSpec(
                id="research",
                kind="agent",
                description="Answer each sub-question from the workspace's sources.",
                prompt=(
                    "Answer each of these sub-questions using search_sources, "
                    "citing the passages you used with [n]. Say plainly when the "
                    "sources do not cover one.\n\n{{ plan.output }}"
                ),
            ),
            NodeSpec(
                id="build",
                kind="agent",
                description="Produce the artefacts and bring each one back.",
                prompt=(
                    "Build the deliverable's artefacts with run_python, then call "
                    "sandbox_download once per file to save each into the "
                    "workspace.\n\nThe research so far:\n{{ research.output }}"
                ),
            ),
            NodeSpec(
                id="manifest",
                kind="tool",
                description="Record what was produced and what it came from.",
                tool=RECORD_MANIFEST,
                arguments={
                    "title": "{{ input.question }}",
                    "space_id": "{{ input.space_id }}",
                    "question": "{{ input.question }}",
                },
            ),
        ],
        edges=[
            EdgeSpec.model_validate({"from": "plan", "to": "research"}),
            EdgeSpec.model_validate({"from": "research", "to": "build"}),
            EdgeSpec.model_validate({"from": "build", "to": "manifest"}),
        ],
    )


def instantiate(
    db: Session, *, workspace_id: str, user_id: str, title: str
) -> Workflow:
    """Write the preset as an ordinary draft workflow. Flushes; never commits."""
    graph = preset_graph()
    workflow = Workflow(
        workspace_id=workspace_id,
        created_by=user_id,
        name=PRESET_NAME,
        description=(title or graph.description)[:500],
        source_prompt="",
        graph_json=json.dumps(graph.to_document()),
        status="draft",
        trigger_kind="manual",
        schedule_cron="",
        schedule_timezone="UTC",
    )
    db.add(workflow)
    db.flush()
    return workflow


# --- the record_manifest tool ----------------------------------------------


def record(db: Session, context: ToolContext, args: Dict[str, Any]) -> ToolResult:
    """Write the manifest for the run this call is happening inside.

    Reads THIS RUN's own rows and nothing else: `AgentToolCall` and
    `RunCheckpoint` are both filtered on `workspace_id` AND `run_id`, never
    fetched by primary key, so another run's files can never be claimed.
    """
    run_id = context.run_id
    if not run_id:
        return ToolResult(
            content="Error: record_manifest can only run inside a workflow run."
        )
    workflow_run = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.run_id == run_id,
            WorkflowRun.workspace_id == context.workspace_id,
        )
    )
    space_id = str(args.get("space_id") or "")
    title = str(args.get("title") or "")[:200]
    question = str(args.get("question") or "")
    manifest = write_manifest(
        db,
        workspace_id=context.workspace_id,
        run_id=run_id,
        workflow_run_id=workflow_run.id if workflow_run is not None else "",
        space_id=space_id,
        title=title,
        created_by=context.user_id,
        question=question,
        status="complete",
    )
    files = len(
        list(
            db.scalars(
                select(ManifestFile.id).where(
                    ManifestFile.manifest_id == manifest.id,
                    ManifestFile.workspace_id == context.workspace_id,
                )
            )
        )
    )
    return ToolResult(
        content=(
            f"Recorded manifest {manifest.id} with {files} file(s). "
            "These files are stored, not indexed — they cannot be cited."
        ),
        created_ids=[manifest.id],
    )


def write_manifest(
    db: Session,
    *,
    workspace_id: str,
    run_id: str,
    workflow_run_id: str,
    space_id: str,
    title: str,
    created_by: str,
    question: str = "",
    status: str = "complete",
) -> DeliverableManifest:
    """The manifest row plus one file row per downloaded Source, in call order.

    THE BUDGET AND THE SPEND ARE FILLED IN HERE, on both paths. A manifest is
    the receipt for a run whose only cost control is those two numbers, and a
    completed manifest reading `spent_tool_calls: 38 of —` with no time at all
    cannot even show the reader that the lever was there. The budgets are read
    back off the run's own `input_json` (bound and re-stored by the executor,
    defaults included), and the elapsed time from its `started_at`.
    """
    queries = _queries(db, workspace_id=workspace_id, run_id=run_id)
    chunk_ids = _cited_chunks(db, workspace_id=workspace_id, run_id=run_id)
    ledger = db.scalar(
        select(CoverageLedger)
        .where(
            CoverageLedger.workspace_id == workspace_id,
            CoverageLedger.run_id == run_id,
        )
        .order_by(CoverageLedger.created_at.desc(), CoverageLedger.id)
        .limit(1)
    )
    workflow_run = (
        db.scalar(
            select(WorkflowRun).where(
                WorkflowRun.id == workflow_run_id,
                WorkflowRun.workspace_id == workspace_id,
            )
        )
        if workflow_run_id
        else None
    )
    budget_seconds, budget_tool_calls = budgets(workflow_run)
    manifest = DeliverableManifest(
        workspace_id=workspace_id,
        workflow_run_id=workflow_run_id,
        # THE SENTINEL: '' is the workspace library, never NULL.
        space_id=space_id,
        title=(title or question)[:200],
        status=status,
        budget_seconds=budget_seconds,
        budget_tool_calls=budget_tool_calls,
        spent_seconds=spent_seconds(workflow_run),
        spent_tool_calls=_tool_call_count(db, workspace_id=workspace_id, run_id=run_id),
        ledger_id=ledger.id if ledger is not None else "",
        created_by=created_by,
    )
    db.add(manifest)
    db.flush()
    for ordinal, (source_id, session_id) in enumerate(
        _downloaded(db, workspace_id=workspace_id, run_id=run_id)
    ):
        source = db.scalar(
            select(Source).where(
                Source.id == source_id, Source.workspace_id == workspace_id
            )
        )
        db.add(
            ManifestFile(
                workspace_id=workspace_id,
                manifest_id=manifest.id,
                ordinal=ordinal,
                source_id=source_id,
                filename=source.filename if source is not None else "",
                byte_size=source.byte_size if source is not None else 0,
                sandbox_session_id=session_id,
                queries_json=json.dumps(queries),
                chunk_ids_json=json.dumps(chunk_ids),
            )
        )
    db.flush()
    return manifest


def finalize_partial(
    db: Session, *, workflow_run: WorkflowRun, reason: str
) -> Optional[DeliverableManifest]:
    """Ship what a halted deliverable run did produce, marked `partial`.

    A run that exhausted its budget still made files, still consulted sources
    and may still have a coverage ledger; refusing to record any of that would
    throw away the honest half. Idempotent — a run that already has a manifest
    keeps it — so a second halt path cannot double-write.
    """
    if not workflow_run.run_id:
        return None
    existing = db.scalar(
        select(DeliverableManifest).where(
            DeliverableManifest.workflow_run_id == workflow_run.id,
            DeliverableManifest.workspace_id == workflow_run.workspace_id,
        )
    )
    if existing is not None:
        return existing
    try:
        payload = json.loads(workflow_run.input_json or "{}")
    except ValueError:
        payload = {}
    manifest = write_manifest(
        db,
        workspace_id=workflow_run.workspace_id,
        run_id=workflow_run.run_id,
        workflow_run_id=workflow_run.id,
        space_id=str(payload.get("space_id") or ""),
        title=str(payload.get("question") or PRESET_NAME),
        created_by=workflow_run.created_by,
        status="partial",
    )
    # The budgets and the spend are `write_manifest`'s job on BOTH paths now —
    # a completed manifest needs them just as much as a halted one.
    logger.info(
        "deliverable run %s shipped a partial manifest (%s)", workflow_run.id, reason
    )
    return manifest


def budgets(workflow_run: Optional[WorkflowRun]) -> tuple[int, int]:
    """(seconds, tool calls) this run was given. 0 means NO LIMIT, explicitly.

    Read off `input_json`, which the executor re-stores after `inputs.bind`, so
    the declared defaults are already applied and a run record answers "what
    did this actually run with". The schema allows `ge=0`, and 0 has to mean
    "unlimited" rather than "halt immediately" — a zero that halted on the
    first node would turn an optional field into a trap.
    """
    if workflow_run is None:
        return (0, 0)
    try:
        payload = json.loads(workflow_run.input_json or "{}")
    except ValueError:
        return (0, 0)
    if not isinstance(payload, dict):
        return (0, 0)

    def _int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    return (_int(payload.get("budget_seconds")), _int(payload.get("budget_tool_calls")))


def spent_seconds(workflow_run: Optional[WorkflowRun]) -> int:
    """Wall-clock seconds this run has been running, from its own timestamps."""
    if workflow_run is None:
        return 0
    started = workflow_run.started_at or workflow_run.created_at
    if started is None:
        return 0
    finished = workflow_run.finished_at or utcnow()
    return max(0, int((finished - started).total_seconds()))


def _tool_calls(db: Session, *, workspace_id: str, run_id: str) -> List[AgentToolCall]:
    return list(
        db.scalars(
            select(AgentToolCall)
            .where(
                AgentToolCall.workspace_id == workspace_id,
                AgentToolCall.run_id == run_id,
            )
            .order_by(AgentToolCall.created_at, AgentToolCall.id)
        )
    )


#: What counts as spend. A call the gate parked and a human denied, or one
#: still sitting `proposed`, cost the run nothing and must not bill against a
#: budget — `_park_for_approval` writes the row BEFORE any decision, so a
#: plain row count charges for calls that never ran.
EXECUTED_STATUSES = frozenset({"succeeded", "failed"})


def executed_tool_calls(db: Session, *, workspace_id: str, run_id: str) -> int:
    """The spend a tool-call budget is measured against. The executor's read."""
    return _tool_call_count(db, workspace_id=workspace_id, run_id=run_id)


def _tool_call_count(db: Session, *, workspace_id: str, run_id: str) -> int:
    """How many tool calls this run actually EXECUTED. See EXECUTED_STATUSES."""
    return len(
        [
            call
            for call in _tool_calls(db, workspace_id=workspace_id, run_id=run_id)
            if call.status in EXECUTED_STATUSES
        ]
    )


def _queries(db: Session, *, workspace_id: str, run_id: str) -> List[str]:
    """Every `search_sources` query this run asked, in call order, deduplicated."""
    queries: List[str] = []
    for call in _tool_calls(db, workspace_id=workspace_id, run_id=run_id):
        if call.name != "search_sources":
            continue
        try:
            arguments = json.loads(call.arguments_json or "{}")
        except ValueError:
            continue
        query = str(arguments.get("query") or "").strip()
        if query and query not in queries:
            queries.append(query)
    return queries


def _cited_chunks(db: Session, *, workspace_id: str, run_id: str) -> List[str]:
    """The passages this run actually CITED, in order.

    Cited rather than merely returned, and the difference is the point: what a
    search handed the model is not persisted per call, while what the answer
    leaned on is. A weaker join stated honestly beats a stronger one invented.
    """
    chunk_ids: List[str] = []
    for message in db.scalars(
        select(Message)
        .where(
            Message.workspace_id == workspace_id,
            Message.run_id == run_id,
            Message.role == "assistant",
        )
        .order_by(Message.created_at, Message.id)
    ):
        try:
            citations = json.loads(message.citations_json or "[]")
        except ValueError:
            continue
        if not isinstance(citations, list):
            continue
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            chunk_id = str(citation.get("chunk_id") or "")
            if chunk_id and chunk_id not in chunk_ids:
                chunk_ids.append(chunk_id)
    return chunk_ids


def _downloaded(
    db: Session, *, workspace_id: str, run_id: str
) -> List[tuple[str, str]]:
    """(source_id, sandbox session id) per `sandbox_download`, in call order.

    The source ids come from the undo trail's own capture
    (`checkpoints._capture_sandbox_download` stores exactly the ids the
    executor reported), so the manifest and the undo agree on what this run
    created — rather than the manifest re-deriving it from a clipped preview.

    THE JOIN IS KEYED, never positional. `RunCheckpoint.tool_call_id` names the
    exact call a checkpoint came from, and that is the join this module's one
    claim rests on. Pairing the two lists by position would shift every later
    row the first time a `sandbox_download` wrote a call row but no checkpoint
    — a denied approval, a failed executor, or the ordinary soft failure where
    the file is missing or too big and `finalize` returns no ids. A shifted
    row is a real session id pinned to a file it never produced, served as
    fact; an unresolved one records "" instead, because a blank is honest and a
    guessed neighbour is not.

    The session is read as `session` first: that is the name the tool's own
    schema advertises, so a schema-conformant model emits it. `session_id` is
    kept as the alias `_session_for` also accepts, for rows already written.
    """
    sessions: Dict[str, str] = {}
    for call in _tool_calls(db, workspace_id=workspace_id, run_id=run_id):
        if call.name != "sandbox_download":
            continue
        try:
            arguments = json.loads(call.arguments_json or "{}")
        except ValueError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        sessions[call.id] = str(
            arguments.get("session") or arguments.get("session_id") or ""
        )
    downloaded: List[tuple[str, str]] = []
    for checkpoint in db.scalars(
        select(RunCheckpoint)
        .where(
            RunCheckpoint.workspace_id == workspace_id,
            RunCheckpoint.run_id == run_id,
            RunCheckpoint.tool_name == "sandbox_download",
        )
        .order_by(RunCheckpoint.created_at, RunCheckpoint.id)
    ):
        try:
            before = json.loads(checkpoint.before_json or "{}")
        except ValueError:
            before = {}
        session_id = sessions.get(checkpoint.tool_call_id or "", "")
        for source_id in before.get("source_ids") or []:
            if isinstance(source_id, str) and source_id:
                downloaded.append((source_id, session_id))
    return downloaded


def registry_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """The `record_manifest` executor, offered through the ordinary registry.

    Registered like every other family so it is policy-gated, org-ceiling-gated
    and allowlist-narrowable. A tool that bypassed the registry would bypass
    `ToolPolicy`, which is what makes a scheduled workflow safe to run
    unattended.
    """
    return {
        RECORD_MANIFEST: ToolSpec(
            name=RECORD_MANIFEST,
            description=(
                "Record the manifest for this deliverable run: every file it "
                "saved to the workspace, the sandbox session each came from, "
                "and the queries and cited passages behind them. Call this once, "
                "after the files have been downloaded."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "space_id": {
                        "type": "string",
                        "description": "Blank for the workspace library.",
                    },
                    "question": {"type": "string"},
                },
                "required": ["title"],
            },
            executor=record,
            # It writes manifest rows, so it is not read-only and the workspace
            # policy decides whether an unattended run may do it.
            read_only=False,
        )
    }
