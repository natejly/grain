from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import List, Optional, cast

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..config import Settings, get_settings
from ..database import SessionLocal, get_db
from ..models import (
    Agent,
    Conversation,
    Dashboard,
    Message,
    Run,
    RunCheckpoint,
    RunEvent,
    User,
    new_id,
)
from ..schemas import (
    ApiModel,
    ApprovalMode,
    ApprovalModeRequest,
    CitationCheck,
    ConversationCreate,
    ConversationDefaultsRequest,
    ConversationForkRequest,
    ConversationOut,
    ConversationShareRequest,
    ConversationTitleRequest,
    MessageOut,
    RunOut,
    SendMessageRequest,
    SendMessageResponse,
    SteerRequest,
)
from ..services import checkpoints, conversation_index, conversations, orgs, subjects
from ..services import skills as skills_service
from ..services import spaces as spaces_service
from ..services.artifacts import documents
from ..services.audit import record_audit
from ..services.events import append_event
from ..services.projects import store as project_store
from ..services.runs import TERMINAL_RUN_STATES, process_run
from .dependencies import idempotency_key
from .idempotency import find_replay, record_key, replayed_resource_gone

router = APIRouter(prefix="/api", tags=["chat"])


def _citation_report(raw: str) -> Optional[CitationCheck]:
    """The stored verdict, or None when there is not a usable one.

    Never raises. An answer predating the column, or one whose validator run
    crashed, has no verdict — and a chat that 500s because a *diagnostic* could
    not be parsed would be a strictly worse product than one that shows no
    badge.
    """
    if not raw:
        return None
    try:
        return CitationCheck.model_validate(json.loads(raw))
    except (ValueError, ValidationError):
        return None


def _message_out(message: Message, sender_name: str = "") -> MessageOut:
    return MessageOut(
        id=message.id,
        run_id=message.run_id,
        role=message.role,
        content=message.content,
        citations=json.loads(message.citations_json),
        citation_report=_citation_report(message.citation_report_json),
        sender_id=message.created_by,
        sender_name=sender_name,
        created_at=message.created_at,
    )


def _can_share(conversation: Conversation, actor: Actor) -> bool:
    """Who may toggle a thread's visibility: its creator, or the workspace owner.

    Mirrors the skills share gate. There is no "admin" role in this codebase —
    membership is `member` or `owner` — so an owner is the only non-creator who
    may share or unshare a thread others may be using.
    """
    return conversation.created_by == actor.user_id or actor.role == "owner"


def _conversation_out(conversation: Conversation, actor: Actor) -> ConversationOut:
    """Build the wire view, folding in the two actor-dependent facts.

    `owned` and `can_share` depend on who is asking, not on the row, so the
    response is built explicitly rather than validated off the ORM object.
    """
    return ConversationOut(
        id=conversation.id,
        title=conversation.title,
        subject_kind=conversation.subject_kind,
        subject_id=conversation.subject_id,
        approval_mode=cast(ApprovalMode, conversation.approval_mode),
        shared=bool(conversation.shared),
        owned=conversation.created_by == actor.user_id,
        can_share=_can_share(conversation, actor),
        space_id=conversation.space_id,
        default_agent_id=conversation.default_agent_id,
        default_model=conversation.default_model,
        default_effort=conversation.default_effort,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


@router.get("/conversations", response_model=List[ConversationOut])
def list_conversations(
    space_id: Optional[str] = Query(default=None),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[ConversationOut]:
    # Subject-scoped threads are deliberately absent. They belong to the side
    # panel of the document, project or dashboard they are about, are created
    # and deleted with it, and one entry per thing opened would turn the Chat
    # rail into a list of things the user never started.
    #
    # The caller sees the workspace's shared threads PLUS their own personal
    # ones. The `workspace_id` filter is never removed — `shared` only relaxes
    # the within-workspace creator filter, so this can never return another
    # workspace's rows, and another member's personal thread stays hidden.
    stmt = (
        select(Conversation)
        .where(
            Conversation.workspace_id == actor.workspace_id,  # NEVER removed
            Conversation.subject_id == "",
            (Conversation.shared.is_(True))
            | (Conversation.created_by == actor.user_id),
        )
        .order_by(Conversation.updated_at.desc())
    )
    if space_id is not None:
        # Exact, "" included, so the space page lists its threads and a caller
        # asking for "no space" is expressible. Only ever narrows the query —
        # the visibility predicate above is untouched.
        stmt = stmt.where(Conversation.space_id == space_id)
    conversations_list = db.scalars(stmt)
    return [_conversation_out(conversation, actor) for conversation in conversations_list]


class ConversationSearchHitOut(ApiModel):
    """One transcript passage matching a search, with where and when."""

    conversation_id: str
    title: str
    #: "quote" for a transcript window, "summary" for a thread's rolling summary.
    kind: str
    snippet: str
    spoken_at: datetime


#: Enough to recognise the conversation in a palette row; the full passage is
#: one click away in the thread itself.
SEARCH_SNIPPET_CHARS = 240


@router.get("/conversations/search", response_model=List[ConversationSearchHitOut])
def search_conversations_http(
    q: str = Query(min_length=2, max_length=200),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> List[ConversationSearchHitOut]:
    """Search past conversations by what was said, not only what they are named.

    The same hybrid index (and, critically, the same visibility chokepoint)
    the agent's `search_conversations` tool reads — a member searching by HTTP
    must see exactly what the agent quoting on their behalf would see, and a
    personal thread stays its creator's in both. Declared before FastAPI could
    ever confuse it with a by-id path, and returning [] rather than erroring
    when the index is disabled: to a palette, "no hits" and "no index" call
    for the same quiet row.
    """
    hits = conversation_index.search_conversation_chunks(
        db,
        workspace_id=actor.workspace_id,
        viewer_id=actor.user_id,
        query=q,
    )
    return [
        ConversationSearchHitOut(
            conversation_id=hit.conversation_id,
            title=hit.title,
            kind=hit.kind,
            snippet=hit.content[:SEARCH_SNIPPET_CHARS],
            spoken_at=hit.spoken_at,
        )
        for hit in hits
    ]


@router.post("/conversations", response_model=ConversationOut, status_code=201)
def create_conversation(
    payload: ConversationCreate,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ConversationOut:
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
        return _conversation_out(conversation, actor)
    space_id = ""
    if payload.space_id:
        # Proved against the caller's workspace before it is stamped; a foreign
        # or deleted space is the same fact to this caller — 404 — never a
        # thread that silently lost its scope.
        try:
            space_id = spaces_service.get_space(
                db, workspace_id=actor.workspace_id, space_id=payload.space_id
            ).id
        except spaces_service.SpaceError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    conversation = Conversation(
        id=new_id(),
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        title=payload.title.strip() or "New conversation",
        space_id=space_id,
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
    return _conversation_out(conversation, actor)


@router.post(
    "/conversations/{conversation_id}/fork",
    response_model=ConversationOut,
    status_code=201,
)
def fork_conversation(
    conversation_id: str,
    payload: ConversationForkRequest,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ConversationOut:
    """Branch a new thread from everything said up to one message.

    The source is resolved through the same visibility chokepoint as reading
    it (`conversations.resolve_visible`): a foreign workspace's thread or
    another member's personal thread is the same 404 here as on
    `GET .../messages`. The anchor must belong to *that* conversation — a
    message id from any other thread, this workspace's included, is also a
    404, so the pair of ids never becomes an existence oracle.

    The fork is a plain personal thread: created by the caller, unshared,
    subjectless, kept in the source's space so it stays findable where the
    original lives. Only the transcript up to and including the anchor is
    copied — fresh message ids, `run_id` cleared, sender attribution kept —
    and nothing that hangs off the source's runs comes along: a run's tool
    calls and parked agent state are bound to their original `call_id`
    pairing and must never be resumable from a copy.
    """
    replay = find_replay(
        db,
        workspace_id=actor.workspace_id,
        operation="conversation.fork",
        key=key,
    )
    if replay:
        fork = db.get(Conversation, replay.resource_id)
        if fork is None or fork.workspace_id != actor.workspace_id:
            raise replayed_resource_gone()
        return _conversation_out(fork, actor)
    source = conversations.resolve_visible(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        conversation_id=conversation_id,
    )
    if source is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    anchor = db.scalar(
        select(Message).where(
            Message.id == payload.message_id,
            Message.conversation_id == conversation_id,
            Message.workspace_id == actor.workspace_id,
        )
    )
    if anchor is None:
        raise HTTPException(status_code=404, detail="Message not found")
    fork = Conversation(
        id=new_id(),
        workspace_id=actor.workspace_id,
        created_by=actor.user_id,
        title=(payload.title.strip() or f"Fork of {source.title}")[:200],
        shared=False,
        space_id=source.space_id,
    )
    db.add(fork)
    # Everything up to and including the anchor, in the transcript's own
    # (created_at, id) order — the `ix_messages_conversation_created` ordering
    # with the id as tiebreak, so two messages sharing a timestamp copy
    # deterministically and the anchor's same-instant successors stay behind.
    # Timestamps are preserved so the copied transcript reads in the order it
    # was spoken; run_id is cleared because the copied words answer no run of
    # this thread's.
    copied = 0
    for message in db.scalars(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.workspace_id == actor.workspace_id,
            or_(
                Message.created_at < anchor.created_at,
                and_(
                    Message.created_at == anchor.created_at,
                    Message.id <= anchor.id,
                ),
            ),
        )
        .order_by(Message.created_at.asc(), Message.id.asc())
    ):
        db.add(
            Message(
                id=new_id(),
                workspace_id=actor.workspace_id,
                conversation_id=fork.id,
                run_id="",
                role=message.role,
                content=message.content,
                created_by=message.created_by,
                citations_json=message.citations_json,
                citation_report_json=message.citation_report_json,
                created_at=message.created_at,
            )
        )
        copied += 1
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="conversation.fork",
        key=key,
        resource_id=fork.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="conversation.forked",
        resource_type="conversation",
        resource_id=fork.id,
        detail={
            "source_conversation_id": conversation_id,
            "message_id": anchor.id,
            "messages": copied,
        },
    )
    db.commit()
    db.refresh(fork)
    return _conversation_out(fork, actor)


def _subject_conversation(
    db: Session,
    actor: Actor,
    *,
    subject_kind: str,
    subject_id: str,
    title: str,
) -> ConversationOut:
    """The thread for the chat panel beside one subject, made on first open.

    Reached by a POST because the first call creates, but idempotent by
    construction rather than by an `Idempotency-Key`: the subject id *is* the
    key. There is one thread per subject, so a retry, a second tab and a remount
    all land on the same conversation, and nothing here needs a client to
    remember a nonce.

    Shared by all three routes rather than written three times, because the only
    thing that differs between them is how the subject is looked up and what a
    404 means — and a get-or-create implemented per kind is a get-or-create that
    will eventually create two threads for one of them.
    """
    existing = conversations.for_subject_ids(
        db,
        workspace_id=actor.workspace_id,
        subject_kind=subject_kind,
        subject_id=subject_id,
    )
    conversation = conversations.for_subject(
        db,
        workspace_id=actor.workspace_id,
        subject_kind=subject_kind,
        subject_id=subject_id,
        user_id=actor.user_id,
        title=title,
    )
    if not existing:
        record_audit(
            db,
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            action="conversation.created",
            resource_type="conversation",
            resource_id=conversation.id,
            detail={
                "title": conversation.title,
                "subject_kind": subject_kind,
                "subject_id": subject_id,
            },
        )
    db.commit()
    db.refresh(conversation)
    return _conversation_out(conversation, actor)


@router.post(
    "/documents/{document_id}/conversation", response_model=ConversationOut
)
def document_conversation(
    document_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ConversationOut:
    try:
        document = documents.get_document(
            db, workspace_id=actor.workspace_id, document_id=document_id
        )
    except documents.DocumentError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _subject_conversation(
        db,
        actor,
        subject_kind=subjects.DOCUMENT,
        subject_id=document.id,
        title=document.title,
    )


@router.post("/projects/{project_id}/conversation", response_model=ConversationOut)
def project_conversation(
    project_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ConversationOut:
    try:
        project = project_store.get_project(
            db, workspace_id=actor.workspace_id, project_id=project_id
        )
    except project_store.ProjectError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _subject_conversation(
        db,
        actor,
        subject_kind=subjects.PROJECT,
        subject_id=project.id,
        title=project.name,
    )


@router.post("/dashboards/{dashboard_id}/conversation", response_model=ConversationOut)
def dashboard_conversation(
    dashboard_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ConversationOut:
    dashboard = db.scalar(
        select(Dashboard).where(
            Dashboard.id == dashboard_id,
            Dashboard.workspace_id == actor.workspace_id,
        )
    )
    if dashboard is None:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return _subject_conversation(
        db,
        actor,
        subject_kind=subjects.DASHBOARD,
        subject_id=dashboard.id,
        title=dashboard.name,
    )


@router.put(
    "/conversations/{conversation_id}/approval-mode", response_model=ConversationOut
)
def set_approval_mode(
    conversation_id: str,
    payload: ApprovalModeRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ConversationOut:
    """Change how much this thread asks before acting.

    Audited on every call, including the no-op where the mode is already what
    was asked for. Turning the approval park off is the single most consequential
    switch in the product — it is the containment that prompt injection has to
    get past — so "when did this thread stop asking, and who stopped it" must
    have an answer, and an audit that skips the unchanged case is an audit whose
    silence means two different things.

    No `Idempotency-Key`: this is a PUT of a value, not the creation of one, so a
    retry lands on the same state by construction. It writes a second audit row,
    which is the correct record of a request that was actually made twice.

    Any member of a shared thread may set its mode — the mode governs the shared
    thread they are collaborating in — and the audit row records who did it.
    """
    conversation = conversations.resolve_visible(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    previous = conversation.approval_mode
    conversation.approval_mode = payload.mode
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="conversation.approval_mode_set",
        resource_type="conversation",
        resource_id=conversation.id,
        detail={"from": previous, "to": payload.mode},
    )
    db.commit()
    db.refresh(conversation)
    return _conversation_out(conversation, actor)


@router.patch(
    "/conversations/{conversation_id}/defaults", response_model=ConversationOut
)
def set_conversation_defaults(
    conversation_id: str,
    payload: ConversationDefaultsRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ConversationOut:
    """Remember the composer's choices — agent, model, effort — on the thread.

    A PATCH of preferences, not policy: the run path never reads these (every
    turn still names its controls explicitly), so there is nothing here to
    audit and no gate beyond visibility. Any member a shared thread is visible
    to may set them — they are the thread's working setup, like its title.

    No `Idempotency-Key`: a retry lands on the same state by construction.
    """
    conversation = conversations.resolve_visible(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if payload.default_agent_id is not None:
        conversation.default_agent_id = payload.default_agent_id
    if payload.default_model is not None:
        conversation.default_model = payload.default_model
    if payload.default_effort is not None:
        conversation.default_effort = payload.default_effort
    db.commit()
    db.refresh(conversation)
    return _conversation_out(conversation, actor)


@router.put("/conversations/{conversation_id}/title", response_model=ConversationOut)
def rename_conversation(
    conversation_id: str,
    payload: ConversationTitleRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ConversationOut:
    """Rename a thread.

    Any member the thread is visible to may rename it, same as the approval
    mode: a shared thread's name is the collaboration's, not the creator's,
    and a personal thread is only ever visible to its creator anyway. Subject
    threads are refused — their titles are derived from the subject they hang
    off ("Document: Q3 notes"), and a hand-renamed one would stop saying what
    it is attached to.

    No `Idempotency-Key`: a PUT of a value, so a retry lands on the same state
    by construction.
    """
    conversation = conversations.resolve_visible(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conversation.subject_id:
        raise HTTPException(
            status_code=409,
            detail="A subject thread is named by what it is attached to",
        )
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="A title needs at least one character")
    conversation.title = title
    db.commit()
    db.refresh(conversation)
    return _conversation_out(conversation, actor)


@router.put("/conversations/{conversation_id}/share", response_model=ConversationOut)
def set_conversation_shared(
    conversation_id: str,
    payload: ConversationShareRequest,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> ConversationOut:
    """Share or unshare a thread with the rest of the workspace.

    Owner-gated, mirroring the skills share gate: only the creator, or the
    workspace owner, may flip a thread between personal and shared. A plain
    member seeing a shared thread must not be able to unshare it out from under
    the people using it, nor share a personal thread they merely happened to
    reach — which is why `resolve_visible` (a 404 for anything outside the
    workspace or another member's personal thread) is followed by the
    creator-or-owner gate (a 403). Sharing changes visibility ONLY within the
    workspace; the `workspace_id` filter in `resolve_visible` is never removed,
    so this can never expose a thread cross-workspace.
    """
    conversation = conversations.resolve_visible(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not _can_share(conversation, actor):
        raise HTTPException(
            status_code=403,
            detail="Only the creator or an owner may share this thread",
        )
    previous = bool(conversation.shared)
    conversation.shared = payload.shared
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="conversation.shared_set",
        resource_type="conversation",
        resource_id=conversation.id,
        detail={"from": previous, "to": payload.shared},
    )
    db.commit()
    db.refresh(conversation)
    return _conversation_out(conversation, actor)


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
    conversation = conversations.resolve_visible(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # A member must not nuke a shared thread others are using: deletion is gated
    # to the creator or the workspace owner, even though every member can read it.
    if conversation.created_by != actor.user_id and actor.role != "owner":
        raise HTTPException(
            status_code=403,
            detail="Only the creator or an owner may delete this thread",
        )
    title = conversations.purge(
        db, workspace_id=actor.workspace_id, conversation_id=conversation_id
    )
    if title is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
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
    conversation = conversations.resolve_visible(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = list(
        db.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.workspace_id == actor.workspace_id,
            )
            .order_by(Message.created_at.asc())
        )
    )
    # One query for the distinct senders, so a shared thread shows who said what
    # without an N+1. Restricted to this workspace's users — a name is only ever
    # resolved for a member of the same workspace, never leaked across one.
    sender_ids = {message.created_by for message in messages if message.created_by}
    names: dict[str, str] = {}
    if sender_ids:
        names = {
            user_id: name
            for user_id, name in db.execute(
                select(User.id, User.name).where(User.id.in_(sender_ids))
            )
        }
    return [
        _message_out(message, names.get(message.created_by, ""))
        for message in messages
    ]


def _stage_turn(
    db: Session,
    *,
    actor: Actor,
    settings: Settings,
    conversation: Conversation,
    payload: SendMessageRequest,
) -> tuple[Run, Message]:
    """Resolve the agent, model and skill, then stage the run and its user
    message — everything a new turn needs short of idempotency, audit and
    commit, which differ between the callers (send vs edit)."""
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
    # A per-turn model override must be on the deployment allow-list *as narrowed
    # by the organization*; an arbitrary string would reach the provider unpriced,
    # and one the org has excluded would reach it against policy. `allowed_models`
    # intersects the two, so this refusal and the list `/api/bootstrap` offers the
    # composer are the same list — a dropdown cannot show a choice this 422s.
    # (An off-ladder `effort` is already refused by the `ReasoningEffort` Literal.)
    if payload.model and payload.model not in orgs.allowed_models(
        db, workspace_id=actor.workspace_id, settings=settings
    ):
        raise HTTPException(status_code=422, detail="Model is not selectable")
    # `fast` maps to "low", not "none" — the honest lowest-latency effort every
    # model accepts — and an explicit `effort` always wins over it.
    requested_effort = payload.effort or ("low" if payload.fast else "")
    # A skill invoked for this turn must be visible to the caller (own or shared,
    # same-workspace) and its args must validate now, so the refusal lands at send
    # time rather than inside the turn. The resolved args are stored on the run and
    # the body is spliced into the instructions in `resolve_directives`.
    skill_id = ""
    skill_args_json = ""
    skill_version = 0
    if payload.skill_id:
        skill = skills_service.resolve_visible(
            db,
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            skill_id=payload.skill_id,
        )
        if skill is None:
            raise HTTPException(status_code=404, detail="Skill not available")
        skill_id = skill.id
        skill_args_json = skills_service.validate_args(skill, payload.skill_args or {})
        # Pin the version so a run parked over a later edit resumes with this body.
        skill_version = skill.version
    run = Run(
        id=new_id(),
        workspace_id=actor.workspace_id,
        conversation_id=conversation.id,
        agent_id=agent.id,
        created_by=actor.user_id,
        status="queued",
        prompt=payload.content,
        requested_model=payload.model or "",
        requested_effort=requested_effort,
        skill_id=skill_id,
        skill_args_json=skill_args_json,
        skill_version=skill_version,
        show_thinking=payload.thinking,
        # What was on screen when this was typed — the file the project editor
        # had open. Stored, not acted on: `subjects.resolve` reads it back on
        # every entry into the loop, so a turn that parks for an approval comes
        # back to the file it was asked about rather than to whatever is open an
        # hour later.
        subject_focus=(payload.subject_focus or "")[:400],
    )
    message = Message(
        id=new_id(),
        workspace_id=actor.workspace_id,
        conversation_id=conversation.id,
        run_id=run.id,
        role="user",
        # Attribute the message to the member who sent it, so a shared thread
        # shows who said what. `Run.created_by` is already `actor.user_id`.
        created_by=actor.user_id,
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
    return run, message


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
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> SendMessageResponse:
    conversation = conversations.resolve_visible(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if payload.aside:
        # An aside ("/btw") is a message, not a turn: it lands in the transcript
        # and is read as context by whichever turn comes next — `_transcript`
        # collects by conversation, not by run — but nothing is queued and no
        # agent runs. Its own idempotency operation, because the resource a
        # replay must return is a message rather than a run.
        replay = find_replay(
            db, workspace_id=actor.workspace_id, operation="message.aside", key=key
        )
        if replay:
            message = db.scalar(
                select(Message).where(
                    Message.id == replay.resource_id,
                    Message.workspace_id == actor.workspace_id,
                )
            )
            if message is None:
                raise replayed_resource_gone()
            return SendMessageResponse(
                message=_message_out(message, actor.user_name),
                run=None,
                replayed=True,
            )
        message = Message(
            id=new_id(),
            workspace_id=actor.workspace_id,
            conversation_id=conversation.id,
            run_id="",
            role="user",
            created_by=actor.user_id,
            content=payload.content,
        )
        db.add(message)
        record_key(
            db,
            workspace_id=actor.workspace_id,
            operation="message.aside",
            key=key,
            resource_id=message.id,
        )
        record_audit(
            db,
            workspace_id=actor.workspace_id,
            actor_id=actor.user_id,
            action="message.aside",
            resource_type="message",
            resource_id=message.id,
            detail={"conversation_id": conversation.id},
        )
        db.commit()
        return SendMessageResponse(
            message=_message_out(message, actor.user_name), run=None
        )
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
            message=_message_out(message, actor.user_name),
            run=RunOut.model_validate(run),
            replayed=True,
        )
    run, message = _stage_turn(
        db, actor=actor, settings=settings, conversation=conversation, payload=payload
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
        detail={"conversation_id": conversation.id, "agent_id": run.agent_id},
    )
    db.commit()
    background_tasks.add_task(process_run, run.id)
    return SendMessageResponse(
        message=_message_out(message, actor.user_name),
        run=RunOut.model_validate(run),
    )


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/edit",
    response_model=SendMessageResponse,
    status_code=202,
)
def edit_message(
    conversation_id: str,
    message_id: str,
    payload: SendMessageRequest,
    background_tasks: BackgroundTasks,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
) -> SendMessageResponse:
    """Rewrite one of your prompts and re-run the conversation from there.

    The edit IS a truncation: the old turn's messages, tool cards and runs are
    deleted — not superseded — and a fresh turn is queued with the new words.
    History reaches the model rebuilt from `messages` on every turn, so what
    remains after the cut is exactly the context the re-run sees; keeping the
    old answer around would leave the transcript contradicting itself.

    Only the author may edit (a shared thread must not let one member rewrite
    another's words), only a `user` message that started a turn qualifies (an
    assistant message is not yours to put words into; an aside never ran), and
    a turn that is still running or parked refuses with a 409 — deciding or
    cancelling it comes first.

    Same idempotency shape as `message.send`, its own operation: the resource
    a replay must return is the NEW run.
    """
    conversation = conversations.resolve_visible(
        db,
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        conversation_id=conversation_id,
    )
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if payload.aside:
        raise HTTPException(status_code=422, detail="An aside is added, not edited into")
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="message.edit", key=key
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
            message=_message_out(message, actor.user_name),
            run=RunOut.model_validate(run),
            replayed=True,
        )
    pivot = db.scalar(
        select(Message).where(
            Message.id == message_id,
            Message.conversation_id == conversation.id,
            Message.workspace_id == actor.workspace_id,
        )
    )
    if pivot is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if pivot.role != "user" or pivot.run_id == "":
        raise HTTPException(
            status_code=422,
            detail="Only a prompt that started a turn can be edited",
        )
    if pivot.created_by != actor.user_id:
        raise HTTPException(
            status_code=403, detail="Only the author may edit their message"
        )
    try:
        removed_runs = conversations.truncate_after(
            db,
            workspace_id=actor.workspace_id,
            conversation_id=conversation.id,
            message_id=pivot.id,
            # On a shared thread the sweep must not take a teammate's turn with
            # it; on a personal thread every run is the caller's anyway.
            only_runs_by=actor.user_id if conversation.shared else None,
        )
    except conversations.TruncationBlocked as blocked:
        raise HTTPException(status_code=409, detail=str(blocked)) from blocked
    run, message = _stage_turn(
        db, actor=actor, settings=settings, conversation=conversation, payload=payload
    )
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="message.edit",
        key=key,
        resource_id=run.id,
    )
    # The destructive half gets its own audit row, before the routine
    # `run.created` one: "who cut this transcript, where, and which runs went"
    # must have an answer that is not implied by a run appearing.
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="message.edited",
        resource_type="message",
        resource_id=pivot.id,
        detail={"conversation_id": conversation.id, "removed_runs": removed_runs},
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="run.created",
        resource_type="run",
        resource_id=run.id,
        detail={"conversation_id": conversation.id, "agent_id": run.agent_id},
    )
    db.commit()
    background_tasks.add_task(process_run, run.id)
    return SendMessageResponse(
        message=_message_out(message, actor.user_name),
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
    # A member must not cancel a run on another member's personal thread. The run
    # resolves by workspace_id, but its conversation is not visible to them.
    # Automation and shared/own/document threads pass — same gate as the stream.
    if not conversations.run_activity_visible(
        db,
        actor_workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        run=run,
    ):
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


@router.post(
    "/runs/{run_id}/steer", response_model=SendMessageResponse, status_code=202
)
def steer_run(
    run_id: str,
    payload: SteerRequest,
    key: str = Depends(idempotency_key),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> SendMessageResponse:
    """Fold mid-run guidance into a live turn — same text box, no new run.

    The note lands twice on purpose: as an ordinary user Message under this
    run (the transcript record, attributed to whoever typed it) and as a
    `run.steer` RunEvent — the channel the loop actually consumes, keyed by
    the event's per-run `sequence` so a park/resume neither replays a note
    nor drops one sent while parked. A finished run answers 409 rather than
    404, so the composer can tell "too late, send it as a fresh turn" apart
    from "not yours to steer".
    """
    run = db.scalar(
        select(Run).where(Run.id == run_id, Run.workspace_id == actor.workspace_id)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    # Same gate as cancel and the event stream: a member must not steer a run
    # on another member's personal thread.
    if not conversations.run_activity_visible(
        db,
        actor_workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        run=run,
    ):
        raise HTTPException(status_code=404, detail="Run not found")
    replay = find_replay(
        db, workspace_id=actor.workspace_id, operation="run.steer", key=key
    )
    if replay:
        message = db.scalar(
            select(Message).where(
                Message.id == replay.resource_id,
                Message.workspace_id == actor.workspace_id,
            )
        )
        if message is None:
            raise replayed_resource_gone()
        return SendMessageResponse(
            message=_message_out(message, actor.user_name), run=None, replayed=True
        )
    if run.status in TERMINAL_RUN_STATES or run.cancel_requested:
        raise HTTPException(
            status_code=409,
            detail="This run has finished — send the note as a new message",
        )
    message = Message(
        id=new_id(),
        workspace_id=actor.workspace_id,
        conversation_id=run.conversation_id,
        run_id=run.id,
        role="user",
        created_by=actor.user_id,
        content=payload.content,
    )
    db.add(message)
    append_event(
        db,
        workspace_id=actor.workspace_id,
        run_id=run.id,
        event_type="run.steer",
        payload={"content": payload.content, "message_id": message.id},
    )
    record_key(
        db,
        workspace_id=actor.workspace_id,
        operation="run.steer",
        key=key,
        resource_id=message.id,
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="run.steered",
        resource_type="run",
        resource_id=run.id,
        detail={"conversation_id": run.conversation_id},
    )
    db.commit()
    return SendMessageResponse(message=_message_out(message, actor.user_name), run=None)


class RunUndoRevertedOut(ApiModel):
    tool_name: str
    kind: str


class RunUndoSkippedOut(ApiModel):
    tool_name: str
    reason: str


class RunUndoOut(ApiModel):
    run_id: str
    reverted: List[RunUndoRevertedOut]
    skipped: List[RunUndoSkippedOut]


@router.post("/runs/{run_id}/undo", response_model=RunUndoOut)
def undo_run(
    run_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> RunUndoOut:
    """Revert the writes a finished run recorded checkpoints for.

    Gated exactly as the stream and cancel are: resolve under the workspace,
    then `run_activity_visible`, so a foreign or invisible run is uniformly a
    404 before any state question is answered. Only terminal runs can be
    undone (409 otherwise — a live run is still writing), and only once: each
    checkpoint is consumed by a conditional UPDATE on `reverted_at IS NULL`
    inside `revert_run` (never a check-then-act here), so two concurrent
    undos cannot double-apply a row, and a run whose rows are all consumed
    answers 409. Rows an interrupted earlier undo left unconsumed are picked
    up rather than stranded. No Idempotency-Key: the consumed marker *is* the
    natural guard, the same shape as the assign endpoint's upsert.

    Checkpoints apply newest-first, so a resource created and then written to
    is unwound in the only order that works. Irreversible rows — external
    effects, clipped captures, resources someone else changed after the run —
    come back in `skipped` with a reason rather than pretending.
    """
    run = db.scalar(
        select(Run).where(Run.id == run_id, Run.workspace_id == actor.workspace_id)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if not conversations.run_activity_visible(
        db,
        actor_workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        run=run,
    ):
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status not in TERMINAL_RUN_STATES:
        raise HTTPException(
            status_code=409, detail="The run is still active; undo it once it ends"
        )
    rows = list(
        db.scalars(
            select(RunCheckpoint)
            .where(
                RunCheckpoint.workspace_id == actor.workspace_id,
                RunCheckpoint.run_id == run.id,
            )
            .order_by(RunCheckpoint.created_at.desc(), RunCheckpoint.id.desc())
        )
    )
    # The friendly refusal for the common case; the *guard* is revert_run's
    # per-row conditional UPDATE. Rows an interrupted undo never consumed
    # (reverted_at still NULL after a mid-undo crash) stay eligible, so a
    # retry repairs the remainder instead of 409ing forever.
    pending_rows = [row for row in rows if row.reverted_at is None]
    if rows and not pending_rows:
        raise HTTPException(
            status_code=409, detail="This run's changes were already undone"
        )
    reverted, skipped = checkpoints.revert_run(
        db, run=run, actor_id=actor.user_id, rows=pending_rows
    )
    append_event(
        db,
        workspace_id=actor.workspace_id,
        run_id=run.id,
        event_type="run.reverted",
        payload={"reverted": reverted, "skipped": skipped},
    )
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="run.reverted",
        resource_type="run",
        resource_id=run.id,
        detail={"reverted": len(reverted), "skipped": len(skipped)},
    )
    db.commit()
    return RunUndoOut(
        run_id=run.id,
        reverted=[RunUndoRevertedOut(**item) for item in reverted],
        skipped=[RunUndoSkippedOut(**item) for item in skipped],
    )


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
    # A member must not stream a run on another member's personal thread. The run
    # resolves by workspace_id, but its conversation is not visible to them.
    # Automation and shared/own/document threads pass — same gate as cancel.
    if not conversations.run_activity_visible(
        db,
        actor_workspace_id=actor.workspace_id,
        actor_user_id=actor.user_id,
        run=run,
    ):
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
