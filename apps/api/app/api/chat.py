from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import SessionLocal, get_db
from ..models import (
    Agent,
    Conversation,
    Message,
    Run,
    RunEvent,
    ToolCall,
    new_id,
)
from ..schemas import (
    ConversationCreate,
    ConversationOut,
    MessageOut,
    RunOut,
    SendMessageRequest,
    SendMessageResponse,
)
from ..services.audit import record_audit
from ..services.events import append_event
from ..services.runs import TERMINAL_RUN_STATES, process_run
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api", tags=["chat"])


def _message_out(message: Message) -> MessageOut:
    return MessageOut(
        id=message.id,
        run_id=message.run_id,
        role=message.role,
        content=message.content,
        citations=json.loads(message.citations_json),
        created_at=message.created_at,
    )


@router.get("/conversations", response_model=List[ConversationOut])
def list_conversations(
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[Conversation]:
    return list(
        db.scalars(
            select(Conversation)
            .where(Conversation.workspace_id == actor.workspace_id)
            .order_by(Conversation.updated_at.desc())
        )
    )


@router.post("/conversations", response_model=ConversationOut, status_code=201)
def create_conversation(
    payload: ConversationCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> Conversation:
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="conversation.create",
        key=key,
    )
    if replay:
        conversation = db.get(Conversation, replay.resource_id)
        if conversation is None or conversation.workspace_id != actor.workspace_id:
            raise replayed_resource_gone()
        return conversation
    conversation = Conversation(
        id=new_id(),
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        title=payload.title.strip() or "New conversation",
    )
    db.add(conversation)
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="conversation.create",
        key=key,
        resource_id=conversation.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="conversation.created",
        resource_type="conversation",
        resource_id=conversation.id,
        detail={"title": conversation.title},
    )
    db.commit()
    db.refresh(conversation)
    return conversation


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> None:
    if find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="conversation.delete",
        key=key,
    ):
        # A delete that already happened is the outcome the caller asked for,
        # so a replay is answered with the same 204 whether or not the row is
        # still there to look at.
        return
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == actor.workspace_id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    title = conversation.title
    run_ids = list(
        db.scalars(
            select(Run.id).where(
                Run.conversation_id == conversation.id,
                Run.workspace_id == actor.workspace_id,
            )
        )
    )
    if run_ids:
        db.execute(delete(ToolCall).where(ToolCall.run_id.in_(run_ids)))
        db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
    db.execute(
        delete(Message).where(
            Message.conversation_id == conversation.id,
            Message.workspace_id == actor.workspace_id,
        )
    )
    db.execute(
        delete(Run).where(
            Run.conversation_id == conversation.id,
            Run.workspace_id == actor.workspace_id,
        )
    )
    db.delete(conversation)
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="conversation.delete",
        key=key,
        resource_id=conversation_id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="conversation.deleted",
        resource_type="conversation",
        resource_id=conversation_id,
        detail={"title": title},
    )
    db.commit()


@router.get("/conversations/{conversation_id}/messages", response_model=List[MessageOut])
def list_messages(
    conversation_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[MessageOut]:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == actor.workspace_id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = db.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.workspace_id == actor.workspace_id,
        )
        .order_by(Message.created_at.asc())
    )
    return [_message_out(message) for message in messages]


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=SendMessageResponse,
    status_code=202,
)
def send_message(
    conversation_id: str,
    payload: SendMessageRequest,
    background_tasks: BackgroundTasks,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SendMessageResponse:
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.workspace_id == actor.workspace_id,
        )
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="message.send", key=key
    )
    if replay:
        run = db.scalar(
            select(Run).where(
                Run.id == replay.resource_id,
                Run.workspace_id == actor.workspace_id,
            )
        )
        message = db.scalar(
            select(Message).where(Message.run_id == replay.resource_id, Message.role == "user")
        )
        if run is None or message is None:
            raise replayed_resource_gone()
        return SendMessageResponse(
            message=_message_out(message),
            run=RunOut.model_validate(run),
            replayed=True,
        )
    # "The default agent" is now per workspace — every account gets one at
    # signup — because a global id would point a new tenant at the dev seed's
    # agent, or at nothing at all.
    agent_query = select(Agent).where(
        Agent.workspace_id == actor.workspace_id, Agent.enabled.is_(True)
    )
    if payload.agent_id:
        agent_query = agent_query.where(Agent.id == payload.agent_id)
    else:
        agent_query = agent_query.order_by(Agent.created_at, Agent.id)
    agent = db.scalar(agent_query)
    if agent is None:
        raise HTTPException(status_code=400, detail="Agent is not available")
    run = Run(
        id=new_id(),
        workspace_id=actor.workspace_id,
        conversation_id=conversation.id,
        agent_id=agent.id,
        created_by=actor.user_id,
        status="queued",
        prompt=payload.content,
    )
    message = Message(
        id=new_id(),
        workspace_id=actor.workspace_id,
        conversation_id=conversation.id,
        run_id=run.id,
        role="user",
        content=payload.content,
    )
    db.add_all([run, message])
    append_event(
        db,
        workspace_id=actor.workspace_id,
        run_id=run.id,
        event_type="run.queued",
        payload={"status": "queued", "message_id": message.id},
    )
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="message.send",
        key=key,
        resource_id=run.id,
    )
    if conversation.title == "New conversation":
        conversation.title = payload.content.strip()[:64]
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="run.created",
        resource_type="run",
        resource_id=run.id,
        detail={"conversation_id": conversation.id, "agent_id": agent.id},
    )
    db.commit()
    background_tasks.add_task(process_run, run.id)
    return SendMessageResponse(
        message=_message_out(message),
        run=RunOut.model_validate(run),
    )


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
def cancel_run(
    run_id: str,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> Run:
    run = db.scalar(
        select(Run).where(Run.id == run_id, Run.workspace_id == actor.workspace_id)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if find_replay(
        db, workspace_id=actor.workspace_id, operation="run.cancel", key=key
    ):
        # The run was resolved before the replay branch, so there is always
        # something to return here.
        return run
    if run.status in {"queued", "waiting_for_approval"}:
        run.cancel_requested = True
        run.status = "cancelled"
        # Cancelling a parked run ends the park, whether it was waiting on an
        # approval or on the spend ceiling.
        run.paused_reason = ""
        append_event(
            db,
            workspace_id=actor.workspace_id,
            run_id=run.id,
            event_type="run.cancelled",
            payload={"status": "cancelled"},
        )
    elif run.status not in TERMINAL_RUN_STATES:
        run.cancel_requested = True
        run.status = "cancelling"
        append_event(
            db,
            workspace_id=actor.workspace_id,
            run_id=run.id,
            event_type="run.cancelling",
            payload={"status": "cancelling"},
        )
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="run.cancel",
        key=key,
        resource_id=run.id,
    )
    db.commit()
    return run


async def _event_stream(
    *,
    workspace_id: str,
    run_id: str,
    after: int,
) -> AsyncIterator[str]:
    cursor = after
    idle_ticks = 0
    while True:
        db = SessionLocal()
        try:
            run = db.scalar(
                select(Run).where(
                    Run.id == run_id,
                    Run.workspace_id == workspace_id,
                )
            )
            events = list(
                db.scalars(
                    select(RunEvent)
                    .where(
                        RunEvent.run_id == run_id,
                        RunEvent.workspace_id == workspace_id,
                        RunEvent.sequence > cursor,
                    )
                    .order_by(RunEvent.sequence.asc())
                )
            )
            for event in events:
                cursor = event.sequence
                idle_ticks = 0
                yield (
                    "id: "
                    + str(event.sequence)
                    + "\nevent: "
                    + event.event_type
                    + "\ndata: "
                    + event.payload_json
                    + "\n\n"
                )
            if run is None:
                return
            if run.status in TERMINAL_RUN_STATES and not events:
                return
        finally:
            db.close()
        idle_ticks += 1
        if idle_ticks >= 40:
            yield ": heartbeat\n\n"
            idle_ticks = 0
        await asyncio.sleep(0.25)


@router.get("/runs/{run_id}/events")
def stream_run_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    run = db.scalar(
        select(Run).where(Run.id == run_id, Run.workspace_id == actor.workspace_id)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if last_event_id and last_event_id.isdigit():
        after = max(after, int(last_event_id))
    return StreamingResponse(
        _event_stream(workspace_id=actor.workspace_id, run_id=run.id, after=after),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
