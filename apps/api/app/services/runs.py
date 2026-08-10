from __future__ import annotations

import json
import logging
from datetime import timedelta

import httpx
from sqlalchemy import select

from ..clock import utcnow
from ..config import get_settings
from ..database import SessionLocal
from ..models import Message, Run, Tool, ToolCall, ToolGrant
from .agent_loop import resume_agent_turn, run_agent_turn
from .audit import record_audit
from .events import append_event
from .memory import recall, render_memory_context, write_conversation_memory
from .model import stream_words
from .retrieval import Evidence, search_evidence
from .tools import ToolSecurityError, execute_read_only_get, parse_tool_prompt

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
) -> None:
    """Persist the assistant message and close the run.

    `already_streamed` is set by the agent path, whose deltas were emitted by the
    loop as the model produced them. The `/tool` paths compose their reply here
    with no model behind it, so that text is still chunked for a live feel.
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
            role="assistant",
            content=content,
            citations_json=json.dumps(citations),
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
        db.commit()
    finally:
        db.close()


def _finish_run(
    run: Run,
    *,
    answer: str,
    citations: list[dict[str, object]],
    already_streamed: bool = True,
) -> None:
    _complete_with_message(
        run,
        content=answer,
        citations=citations,
        already_streamed=already_streamed,
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


def _fail_run(db, run_id: str, exc: Exception) -> None:
    db.rollback()
    run = db.get(Run, run_id)
    if run is None or run.status in TERMINAL_RUN_STATES:
        return
    run.status = "failed"
    run.error = str(exc)[:1000]
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


def resume_run(run_id: str, tool_call_id: str, decision: str) -> None:
    """Continue a run parked on a tool approval, after the user decided."""
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
            db, run, tool_call_id=tool_call_id, decision=decision
        )
        if result is None:
            return
        _finish_run(run, answer=result.answer, citations=_citations(result.evidence))
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

        evidence = search_evidence(
            db,
            workspace_id=run.workspace_id,
            query=run.prompt,
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
        _finish_run(run, answer=result.answer, citations=_citations(result.evidence))
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
            payload={"tool_call_id": call.id},
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
                payload={"tool_call_id": call.id, "error": call.error},
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
