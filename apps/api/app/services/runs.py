from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, Optional

import httpx
from sqlalchemy import select

from ..clock import utcnow
from ..config import get_settings
from ..database import SessionLocal
from ..models import Message, Run, Tool, ToolCall, ToolGrant
from . import spaces, webhooks
from .agent_loop import (
    PAUSED_FOR_BUDGET,
    resume_after_budget,
    resume_agent_turn,
    run_agent_turn,
)
from .audit import record_audit
from .citations import summarize_citations, validate_citations
from .conversation_index import update_conversation_index
from .errors import user_facing_message
from .events import append_event
from .memory import recall, render_memory_context, write_conversation_memory
from .model import stream_words
from .retrieval import Evidence, search_evidence
from .tools import ToolSecurityError, execute_read_only_get, parse_tool_prompt
from .web_search import citation_url

logger = logging.getLogger(__name__)

TERMINAL_RUN_STATES = {"completed", "failed", "cancelled"}


def _citations(evidence: list[Evidence]) -> list[dict[str, object]]:
    return [
        {
            "chunk_id": item.chunk_id,
            "source_id": item.source_id,
            "filename": item.filename,
            "ordinal": item.ordinal,
            "excerpt": item.excerpt,
            "score": item.score,
            # None for a passage from the index, whose provenance is its chunk.
            # A web source has no chunk to open, so the URL is the only address
            # a reader can follow back to what was cited.
            "url": citation_url(item),
        }
        for item in evidence
    ]


def _transcript(db, run: Run) -> list[tuple[str, str]]:
    limit = get_settings().memory_transcript_messages
    rows = list(
        db.scalars(
            select(Message)
            .where(
                Message.workspace_id == run.workspace_id,
                Message.conversation_id == run.conversation_id,
                Message.run_id != run.id,
            )
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    )
    rows.reverse()
    return [(row.role, " ".join(row.content.split())[:600]) for row in rows]


def _complete_with_message(
    run: Run,
    *,
    content: str,
    citations: list[dict[str, object]],
    already_streamed: bool = False,
    citation_report: dict[str, object] | None = None,
) -> None:
    """Persist the assistant message and close the run.

    `already_streamed` is set by the agent path, whose deltas were emitted by the
    loop as the model produced them. The `/tool` paths compose their reply here
    with no model behind it, so that text is still chunked for a live feel.

    `citation_report` is the validator's verdict on this exact answer, stored on
    the message so it outlives the event stream. None — every caller but
    `_finish_run` — leaves the column empty, which reads as "not checked" rather
    than "checked and clean".
    """
    db = SessionLocal()
    try:
        current = db.get(Run, run.id)
        if current is None:
            return
        if current.cancel_requested or current.status == "cancelling":
            current.status = "cancelled"
            current.lease_expires_at = None
            append_event(
                db,
                workspace_id=current.workspace_id,
                run_id=current.id,
                event_type="run.cancelled",
                payload={"status": "cancelled"},
            )
            db.commit()
            return
        for piece in [] if already_streamed else stream_words(content):
            db.refresh(current)
            if current.cancel_requested:
                current.status = "cancelled"
                current.lease_expires_at = None
                append_event(
                    db,
                    workspace_id=current.workspace_id,
                    run_id=current.id,
                    event_type="run.cancelled",
                    payload={"status": "cancelled"},
                )
                db.commit()
                return
            append_event(
                db,
                workspace_id=current.workspace_id,
                run_id=current.id,
                event_type="message.delta",
                payload={"delta": piece},
            )
            db.commit()
        message = Message(
            workspace_id=current.workspace_id,
            conversation_id=current.conversation_id,
            run_id=current.id,
            # Attribute the answer to the member whose turn produced it, so a
            # shared thread can show who said what.
            created_by=current.created_by,
            role="assistant",
            content=content,
            citations_json=json.dumps(citations),
            citation_report_json=(
                json.dumps(citation_report) if citation_report is not None else ""
            ),
        )
        db.add(message)
        db.flush()
        current.status = "completed"
        current.lease_expires_at = None
        append_event(
            db,
            workspace_id=current.workspace_id,
            run_id=current.id,
            event_type="message.completed",
            payload={
                "message_id": message.id,
                "content": content,
                "citations": citations,
            },
        )
        append_event(
            db,
            workspace_id=current.workspace_id,
            run_id=current.id,
            event_type="run.completed",
            payload={"status": "completed"},
        )
        # The outbound-webhook chokepoint for chat runs: ids only, no content
        # — the receiver learns a run finished, never what it said. Flushed
        # into this same transaction, committed with the completion.
        webhooks.emit(
            db,
            workspace_id=current.workspace_id,
            event="run.completed",
            payload={
                "run_id": current.id,
                "conversation_id": current.conversation_id,
                "status": "completed",
            },
        )
        db.commit()
    finally:
        db.close()


def _finish_run(
    run: Run,
    *,
    answer: str,
    evidence: list[Evidence],
    already_streamed: bool = True,
) -> None:
    # Takes the evidence list rather than pre-serialized citations so the
    # validator counts from the same object the message is built from. Passing
    # the dicts separately would let the two drift, which is exactly the drift
    # the validator exists to catch.
    # Before _complete_with_message, deliberately: that call emits run.completed,
    # which consumers treat as terminal and stop reading on. An event appended
    # after it is an event nobody sees.
    report = _record_citation_report(run, answer, evidence)
    _complete_with_message(
        run,
        content=answer,
        citations=_citations(evidence),
        already_streamed=already_streamed,
        citation_report=report,
    )
    try:
        write_conversation_memory(run.id)
    except Exception:
        # Memory persistence must never fail a completed run — but it must not
        # vanish either. Everything inside write_conversation_memory already
        # logs and rolls back, so reaching here means it could not even open a
        # session; that is worth a line rather than a silent `pass`.
        logger.warning(
            "memory persistence raised for run %s", run.id, exc_info=True
        )
    try:
        # Same contract as the memory write above, and a miss costs even less:
        # search-time reconcile indexes anything this call did not.
        update_conversation_index(run.id)
    except Exception:
        logger.warning(
            "conversation indexing raised for run %s", run.id, exc_info=True
        )


def _record_citation_report(
    run: Run, answer: str, evidence: list[Evidence]
) -> dict[str, object] | None:
    """Check the answer honoured the citation contract, and record the outcome.

    Recorded on every completed run, not only on violations: a hallucinated-citation
    *rate* needs a denominator, and "runs where evidence was supplied" only exists
    if the clean ones are logged too.

    This observes; it never rewrites the answer and never fails the run. A
    validator that can break the thing it is watching is worse than no validator.

    Returns the payload so the caller can store it on the message this verdict is
    about. An event and an audit row are both places a reader never goes; the
    message is the one surface where a fabricated `[4]` can still be caught. None
    means the validator itself failed, which is not a verdict.
    """
    try:
        report = validate_citations(answer, evidence)
        payload: dict[str, object] = {
            **report.to_dict(),
            "summary": summarize_citations(report),
        }
        db = SessionLocal()
        try:
            append_event(
                db,
                workspace_id=run.workspace_id,
                run_id=run.id,
                event_type="run.citations",
                payload=payload,
            )
            record_audit(
                db,
                workspace_id=run.workspace_id,
                actor_id=run.created_by,
                action="run.citations_validated",
                resource_type="run",
                resource_id=run.id,
                detail=payload,
            )
            db.commit()
        finally:
            db.close()
        return payload
    except Exception:
        logger.warning(
            "citation validation raised for run %s", run.id, exc_info=True
        )
        return None


def _fail_run(db, run_id: str, exc: Exception) -> None:
    # The whole detail, kept where operators can find it and nowhere else. This
    # is the only place it survives, so it is logged before anything can go
    # wrong with writing the row below.
    logger.error("run %s failed", run_id, exc_info=exc)
    db.rollback()
    run = db.get(Run, run_id)
    if run is None or run.status in TERMINAL_RUN_STATES:
        return
    run.status = "failed"
    # Never `str(exc)`. This field is published twice — into the `run.failed`
    # event the browser streams, and into the member-facing Inbox — so a driver
    # error put here becomes SQL, bound parameters and row ids on a user's
    # screen. `user_facing_message` passes through only the messages written to
    # be read, and says something honest about everything else.
    run.error = user_facing_message(exc, run_id=run_id)
    run.paused_reason = ""
    run.lease_expires_at = None
    run.agent_state_json = None
    append_event(
        db,
        workspace_id=run.workspace_id,
        run_id=run.id,
        event_type="run.failed",
        payload={"status": "failed", "error": run.error},
    )
    db.commit()


def resume_run(
    run_id: str,
    tool_call_id: str,
    decision: str,
    amendment: Optional[dict[str, Any]] = None,
    inputs: Optional[dict[str, Any]] = None,
) -> None:
    """Continue a run parked on a tool approval, after the user decided.

    `amendment` carries a partial approval — the subset of a document edit's
    hunks the reviewer accepted — through to the executor. `inputs` carries the
    values a person typed at a workflow `manual` node; both are ignored by the
    path they do not belong to.
    """
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if run is None or run.status != "waiting_for_approval":
            return
        run.lease_expires_at = utcnow() + timedelta(
            seconds=get_settings().run_lease_seconds
        )
        append_event(
            db,
            workspace_id=run.workspace_id,
            run_id=run.id,
            event_type="run.started",
            payload={"status": "running"},
        )
        db.commit()
        result = resume_agent_turn(
            db,
            run,
            tool_call_id=tool_call_id,
            decision=decision,
            amendment=amendment,
            inputs=inputs,
        )
        if result is None:
            return
        _finish_run(run, answer=result.answer, evidence=result.evidence)
    except Exception as exc:
        _fail_run(db, run_id, exc)
    finally:
        db.close()


def resume_run_after_budget(run_id: str) -> None:
    """Continue a run parked on the spend ceiling, after an owner raised it.

    The sibling of `resume_run`, and it guards on `paused_reason` as well as
    status so a release can never be applied to a run parked on an *approval* —
    that one is waiting on a decision this path does not have and must not
    invent.

    If the ceiling is still exceeded the loop parks it straight back; the
    enforcement predicate is consulted in exactly one place and this is not it.
    """
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if run is None or run.status != "waiting_for_approval":
            return
        if run.paused_reason != PAUSED_FOR_BUDGET:
            return
        run.lease_expires_at = utcnow() + timedelta(
            seconds=get_settings().run_lease_seconds
        )
        append_event(
            db,
            workspace_id=run.workspace_id,
            run_id=run.id,
            event_type="run.started",
            payload={"status": "running"},
        )
        db.commit()
        result = resume_after_budget(db, run)
        if result is None:
            return
        _finish_run(run, answer=result.answer, evidence=result.evidence)
    except Exception as exc:
        _fail_run(db, run_id, exc)
    finally:
        db.close()


def process_run(run_id: str) -> None:
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if run is None or run.status not in {"queued", "running"}:
            return
        if run.cancel_requested:
            run.status = "cancelled"
            run.lease_expires_at = None
            append_event(
                db,
                workspace_id=run.workspace_id,
                run_id=run.id,
                event_type="run.cancelled",
                payload={"status": "cancelled"},
            )
            db.commit()
            return
        run.status = "running"
        run.lease_expires_at = utcnow() + timedelta(
            seconds=get_settings().run_lease_seconds
        )
        append_event(
            db,
            workspace_id=run.workspace_id,
            run_id=run.id,
            event_type="run.started",
            payload={"status": "running"},
        )
        db.commit()

        tool_name = parse_tool_prompt(run.prompt)
        if tool_name:
            tool = db.scalar(
                select(Tool)
                .join(ToolGrant, ToolGrant.tool_id == Tool.id)
                .where(
                    Tool.workspace_id == run.workspace_id,
                    ToolGrant.workspace_id == run.workspace_id,
                    ToolGrant.agent_id == run.agent_id,
                    Tool.name == tool_name,
                    Tool.enabled.is_(True),
                )
            )
            if tool is None:
                _complete_with_message(
                    run,
                    content=(
                        "That agent is not granted a tool named “"
                        + tool_name
                        + "”. Try `/tool github-zen` in the demo workspace."
                    ),
                    citations=[],
                )
                return
            call = ToolCall(
                workspace_id=run.workspace_id,
                run_id=run.id,
                tool_id=tool.id,
                status="proposed" if tool.requires_approval else "approved",
                request_url=tool.base_url,
            )
            db.add(call)
            db.flush()
            if tool.requires_approval:
                run.status = "waiting_for_approval"
                run.lease_expires_at = None
                append_event(
                    db,
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    event_type="tool.proposed",
                    payload={
                        "tool_call_id": call.id,
                        "tool_name": tool.name,
                        "description": tool.description,
                        "request_url": call.request_url,
                    },
                )
                append_event(
                    db,
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    event_type="run.waiting_for_approval",
                    payload={"status": "waiting_for_approval"},
                )
                record_audit(
                    db,
                    workspace_id=run.workspace_id,
                    actor_id=run.created_by,
                    action="tool.proposed",
                    resource_type="tool_call",
                    resource_id=call.id,
                    detail={"tool": tool.name, "url": call.request_url},
                )
                db.commit()
                return
            db.commit()
            execute_tool_call(call.id)
            return

        # Resolved once for the turn: retrieval and recall must agree on the
        # scope, and a deleted or foreign space degrades to "" in the resolver.
        space_id = spaces.space_id_for_conversation(
            db, workspace_id=run.workspace_id, conversation_id=run.conversation_id
        )
        evidence = search_evidence(
            db,
            workspace_id=run.workspace_id,
            query=run.prompt,
            space_id=space_id,
        )
        citations = _citations(evidence)
        append_event(
            db,
            workspace_id=run.workspace_id,
            run_id=run.id,
            event_type="retrieval.completed",
            payload={
                "count": len(citations),
                "citations": citations,
            },
        )
        db.commit()
        settings = get_settings()
        transcript = _transcript(db, run)
        memory_context = ""
        if settings.memory_enabled:
            context = recall(
                db,
                workspace_id=run.workspace_id,
                conversation_id=run.conversation_id,
                query=run.prompt,
                # Whose turn this is. The member sees the workspace's memories
                # and their own; another member's personal memories are not
                # candidates for this prompt. ADR 0010.
                viewer_id=run.created_by,
                settings=settings,
            )
            memory_context = render_memory_context(context)
            append_event(
                db,
                workspace_id=run.workspace_id,
                run_id=run.id,
                event_type="memory.recalled",
                payload={
                    "count": len(context.items),
                    "items": [
                        {
                            "id": item.id,
                            "kind": item.kind,
                            "content": item.content,
                        }
                        for item in context.items
                    ],
                    "graph_digest": context.graph_digest,
                },
            )
            db.commit()
        result = run_agent_turn(
            db,
            run,
            evidence=evidence,
            transcript=transcript,
            memory_context=memory_context,
            settings=settings,
        )
        # None means the loop parked for a tool approval or was cancelled;
        # either way the run is not ours to complete right now.
        if result is None:
            return
        _finish_run(run, answer=result.answer, evidence=result.evidence)
    except Exception as exc:
        _fail_run(db, run_id, exc)
    finally:
        db.close()


def execute_tool_call(tool_call_id: str) -> None:
    db = SessionLocal()
    try:
        call = db.get(ToolCall, tool_call_id)
        if call is None or call.status != "approved":
            return
        run = db.get(Run, call.run_id)
        if run is None or run.cancel_requested:
            return
        # Carried on every event this call emits. The agent loop has always named
        # its tool; this path did not, so a chat watching the stream could say
        # only "a tool failed" — and a user cannot act on a failure they cannot
        # name. Filtered by workspace rather than fetched by primary key: the id
        # comes off a row, and a name read across a tenant boundary would be
        # printed straight into another workspace's event stream.
        tool = db.scalar(
            select(Tool).where(
                Tool.id == call.tool_id, Tool.workspace_id == run.workspace_id
            )
        )
        tool_name = tool.name if tool is not None else ""
        call.status = "executing"
        run.status = "running"
        run.lease_expires_at = utcnow() + timedelta(
            seconds=get_settings().run_lease_seconds
        )
        append_event(
            db,
            workspace_id=run.workspace_id,
            run_id=run.id,
            event_type="tool.started",
            payload={"tool_call_id": call.id, "tool_name": tool_name},
        )
        db.commit()
        try:
            status_code, body = execute_read_only_get(call.request_url, get_settings())
            call.status = "succeeded"
            call.response_status = status_code
            call.response_body = body
            append_event(
                db,
                workspace_id=run.workspace_id,
                run_id=run.id,
                event_type="tool.completed",
                payload={
                    "tool_call_id": call.id,
                    "tool_name": tool_name,
                    "status_code": status_code,
                    "preview": body[:500],
                },
            )
            record_audit(
                db,
                workspace_id=run.workspace_id,
                actor_id=run.created_by,
                action="tool.executed",
                resource_type="tool_call",
                resource_id=call.id,
                detail={"status_code": status_code},
            )
            db.commit()
            response = (
                "The approved read-only tool returned HTTP "
                + str(status_code)
                + ":\n\n```\n"
                + body[:1500]
                + "\n```"
            )
            _complete_with_message(run, content=response, citations=[])
        except (ToolSecurityError, httpx.HTTPError, OSError) as exc:
            call.status = "failed"
            call.error = str(exc)[:1000]
            run.status = "failed"
            run.error = call.error
            run.lease_expires_at = None
            append_event(
                db,
                workspace_id=run.workspace_id,
                run_id=run.id,
                event_type="tool.failed",
                payload={
                    "tool_call_id": call.id,
                    "tool_name": tool_name,
                    "error": call.error,
                },
            )
            append_event(
                db,
                workspace_id=run.workspace_id,
                run_id=run.id,
                event_type="run.failed",
                payload={"status": "failed", "error": call.error},
            )
            db.commit()
    finally:
        db.close()


def deny_tool_call(tool_call_id: str) -> None:
    db = SessionLocal()
    try:
        call = db.get(ToolCall, tool_call_id)
        if call is None:
            return
        run = db.get(Run, call.run_id)
        if run is None:
            return
        content = "The proposed tool call was denied. No external request was made."
        _complete_with_message(run, content=content, citations=[])
    finally:
        db.close()
