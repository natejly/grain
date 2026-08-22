from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    DDL,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    false,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

# utcnow is re-exported so existing `from .models import utcnow` imports keep working.
from .clock import utcnow
from .database import Base


def new_id() -> str:
    return str(uuid.uuid4())


#: The `owner_id` of a row the whole workspace holds, as opposed to one person.
#: ADR 0010: one shape for personal scope across every table that grows one,
#: rather than a different spelling of "unowned" per table.
#:
#: An empty string and never NULL, and that is a correctness requirement rather
#: than taste — `owner_id` joins a unique key, and both SQLite and Postgres treat
#: NULLs inside a unique index as distinct, so a nullable column would quietly
#: stop constraining the shared rows at all. ADR 0005 picked NULL for
#: `sandbox_sessions.slot_index` for exactly that property, because there it is
#: what releases a slot. Here it is the bug.
SHARED_OWNER = ""

#: Org roles. `admin` is the only one that may write org configuration, and it is
#: deliberately *not* spelled "owner": a workspace owner and an org admin are
#: different authorities, and sharing a word would invite the one bug this whole
#: tier exists to prevent — a `require_owner` gate accidentally satisfying an org
#: check, or vice versa. Nothing anywhere compares `Membership.role` to
#: `OrgMembership.role`, and the distinct vocabulary is what keeps that true by
#: reading rather than by discipline.
ORG_ADMIN = "admin"
ORG_MEMBER = "member"
ORG_ROLES = (ORG_ADMIN, ORG_MEMBER)


class Organization(Base):
    """The tier above a workspace, and the only authority a workspace owner lacks.

    Every governance control in this codebase used to stop at the workspace,
    which made the workspace owner the highest authority that exists: there was
    no way to express "the organization forbids this even though the workspace
    owner wants it". An org is that sentence.

    It holds two kinds of configuration:

    - a **security posture**, as `OrgToolPolicy` rows that scopes below may only
      tighten (`services.agent_loop.evaluate_policy`);
    - **which harnesses and models are available**, as the two allow-lists
      below, which bound — never widen — what the deployment offers.

    Both allow-lists use "" for *unbounded* rather than an empty JSON array, so
    an org created before anyone thought about model governance constrains
    nothing, and "the org has opinions" is distinguishable from "the org allows
    nothing". The alternative — empty list means everything — makes the
    dangerous state (an admin clearing the list) look like the permissive one.
    """

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160))
    #: JSON array of harness/provider names, or "" for no bound at all.
    allowed_harnesses_json: Mapped[str] = mapped_column(
        Text, default="", server_default=""
    )
    #: JSON array of model names, or "" for no bound at all.
    allowed_models_json: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class OrgMembership(Base):
    """One person's standing in one organization.

    Separate from `Membership` rather than a role value on it, because the two
    answer different questions and a person can hold one without the other. An
    invited contractor is a member of a workspace and of no org — governed by the
    org's policy, with no voice in it — which is exactly the asymmetry the tier
    is for. Conversely an org admin need not be in any of the org's workspaces to
    set the posture they run under.
    """

    __tablename__ = "org_memberships"
    __table_args__ = (UniqueConstraint("organization_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    #: admin | member. Only `admin` may write org configuration.
    role: Mapped[str] = mapped_column(String(24), default=ORG_MEMBER)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OrgToolPolicy(Base):
    """The organization's verdict for one tool in one scope. A ceiling, not a floor.

    Shaped like `ToolPolicy` minus the `owner_id` axis, and that omission is the
    point: a personal row exists so a grant means what its clicker thought it
    meant, and an org row exists so it *cannot*. There is nobody below the org
    whose preference the org is expressing.

    `scope` is kept, and resolved by the same sentence the workspace tier uses —
    a row in this scope decides, else a `chat` deny carries across, else the org
    constrains nothing. An org that wants `send_email` denied at 3am but merely
    reviewed while someone is typing can say so, and it says it the same way a
    workspace does.
    """

    __tablename__ = "org_tool_policies"
    __table_args__ = (UniqueConstraint("organization_id", "tool_name", "scope"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    tool_name: Mapped[str] = mapped_column(String(120))
    policy: Mapped[str] = mapped_column(String(16), default="ask")
    # chat | workflow, exactly as on `ToolPolicy`.
    scope: Mapped[str] = mapped_column(String(16), default="chat", server_default="chat")
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    #: Which organization governs this workspace. NOT NULL and no default: a
    #: workspace with no org is a row nothing can govern, and the whole tier is
    #: worth nothing if it is reachable around. `_attach_orphan_workspaces` below
    #: is what makes the constraint satisfiable without threading an org through
    #: all thirty-odd construction sites.
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    # Null for a federated-only account. A null hash must never authenticate —
    # "no password set" and "any password matches" are one bug apart.
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    # Consecutive failures and the lockout they earned; reset on success.
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class UserIdentity(Base):
    """A federated login bound to a user, e.g. a Google account.

    Keyed on the provider's stable subject rather than the email: an email can be
    reassigned inside a workspace domain, and matching on it would hand the new
    holder the old holder's account.
    """

    __tablename__ = "user_identities"
    __table_args__ = (UniqueConstraint("provider", "subject"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(320), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserSession(Base):
    """A logged-in session, revocable and expiring.

    Only the SHA-256 of the session token is stored. A database leak then yields
    nothing that can be replayed as a login, which is the whole point of not
    keeping the raw value.
    """

    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Cross-origin cookies (Vercel -> API) require SameSite=None, which re-opens
    # CSRF. This per-session secret is what the double-submit check compares.
    csrf_secret: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(400), default="")
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EmailToken(Base):
    """A single-use link: verify an address, or reset a password.

    Hashed like a session, single-use via `consumed_at`, and short-lived — a
    reset link that stays valid after use is a standing account takeover.
    """

    __tablename__ = "email_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(24))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WorkspaceInvite(Base):
    """An invitation to join an existing workspace.

    The same shape as :class:`EmailToken` and for the same reasons: only the
    SHA-256 of the link is stored, `accepted_at` makes redemption a one-way
    door, and `expires_at` bounds how long a leaked link is worth anything. It
    is a *separate* table rather than a fourth `EmailToken.purpose` because an
    invite is addressed to an email that may have no user row yet, while every
    EmailToken hangs off `user_id` — there is nothing to hang this on until the
    invitee accepts.

    `revoked_at` rather than a DELETE: withdrawing an invitation and never
    having sent one are different facts, and the first is one an owner may need
    to see again. `accepted_at` and `revoked_at` are both terminal, and both are
    checked by the conditional UPDATE in `services.auth.invites.accept_invite`,
    so the database decides which of two racing clicks redeems the link.
    """

    __tablename__ = "workspace_invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[str] = mapped_column(String(24), default="member")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    invited_by: Mapped[str] = mapped_column(String(36), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Membership(Base):
    """One person's place in one workspace.

    The unique constraint is not bookkeeping — it is the concurrency control for
    invite acceptance. Two clicks on the same link race, and the constraint is
    what makes "one membership" true regardless of who wins; see
    `services.auth.invites.accept_invite`, which leans on it instead of on a
    read-then-write.
    """

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(24), default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200), default="New conversation")
    #: What this thread is about, for a conversation opened beside something.
    #: "" for an ordinary chat; otherwise document | project | dashboard. A
    #: scoped thread is deliberately kept out of the Chat rail: it belongs to its
    #: subject, is created and deleted with it, and its turns are handed that
    #: subject's content — and only that subject's tools.
    #:
    #: Polymorphic rather than three nullable foreign keys because everything
    #: that acts on the association — the rail filter, the visibility rule, the
    #: cascade, the turn's context and its registry subset — is the *same* code
    #: for all three. Three columns would be three places for each of those to
    #: forget one kind.
    subject_kind: Mapped[str] = mapped_column(String(16), default="", server_default="")
    subject_id: Mapped[str] = mapped_column(
        String(36), default="", server_default="", index=True
    )
    #: How much this thread asks before acting: ask_writes | ask_all |
    #: auto_writes | plan. See `agent_loop.ApprovalMode`.
    #:
    #: Per conversation and not per workspace, because the mode is an answer to
    #: what is being done *right now*. A bypass switched on to get through one
    #: refactor would otherwise still be on next week, governing chats nobody
    #: turned it on for — and the whole value of the ask is that it is still
    #: there when the work changes.
    approval_mode: Mapped[str] = mapped_column(
        String(24), default="ask_writes", server_default="ask_writes"
    )
    #: Personal (False) vs shared (True). A personal thread is visible only to
    #: its creator within the workspace; a shared thread is visible to every
    #: member of the SAME workspace — a collaborative multiplayer thread. Default
    #: False (personal) for new threads; migration 0037 backfills existing rows
    #: to True so no thread that is member-visible today becomes hidden. Sharing
    #: ONLY relaxes the within-workspace `created_by` filter — the workspace_id
    #: filter is never removed, so a shared thread is never visible cross-workspace
    #: and a personal thread is never visible to another member.
    shared: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    #: The space this thread lives in; "" for an ordinary thread. A space is a
    #: grouping *within* the rail — many threads per space, personal/shared
    #: semantics untouched — which is why this is its own column and not a
    #: fourth `subject_kind`: a subject thread is one-per-subject, hidden from
    #: the rail, exempt from the personal/shared gate, and tool-narrowed, and a
    #: space thread is none of those. A thread has a subject or a space, never
    #: both (only ordinary conversation creation sets this).
    #:
    #: Not a FK: "" is the common value and cannot satisfy one, and the space's
    #: delete cascades over its threads explicitly rather than through the
    #: schema, like every other cascade here.
    space_id: Mapped[str] = mapped_column(
        String(36), default="", server_default="", index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    #: The system prompt every turn run as this agent is given. Blank falls back
    #: to the stock `CHAT_INSTRUCTIONS` — an empty system prompt is never sent.
    instructions: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    #: The provisioned tool subset, as a JSON list of registry names. "" means
    #: unset — the agent sees the whole registry — while a stored list ("[]"
    #: included) is an explicit grant. The subset only ever *narrows* what
    #: `build_registry` offers; workspace `ToolPolicy` still applies on top.
    allowed_tools_json: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(36), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    #: The workspace member this message is attributed to: the sender for a
    #: `user` message, and `run.created_by` (the member whose turn produced it)
    #: for an `assistant` message. Lets a shared thread show who said what.
    #: Not a FK — a message is a historical record that must survive a user row
    #: change, matching the string-for-unset convention used elsewhere. "" for
    #: messages predating the column (backfilled from the run in 0037).
    created_by: Mapped[str] = mapped_column(String(36), default="", server_default="")
    citations_json: Mapped[str] = mapped_column(Text, default="[]")
    # The citation validator's verdict on *this* answer — the payload of the
    # `run.citations` event, kept where a reader will meet it again. Empty means
    # the answer was never checked (a denial, a budget park), which is a
    # different fact from "checked and clean" and must not render as one.
    citation_report_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    prompt: Mapped[str] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Serialized agent-loop state, present only while a run is parked waiting for
    # a tool approval. See services/agent_loop.LoopState.
    agent_state_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Why a `waiting_for_approval` run is parked: `approval` for a proposed tool
    # call, `budget` for the ADR 0008 spend ceiling, "" when it is not parked.
    # A separate fact rather than a separate status, because every guard, sweep
    # and filter in this app that reads `waiting_for_approval` means "waiting on
    # a person" and is already right about a budget park — a second status would
    # have needed each of them edited, and would have been wrong wherever one
    # was missed.
    paused_reason: Mapped[str] = mapped_column(String(16), default="")
    # Per-turn overrides chosen when the message was sent, persisted here because
    # `process_run` re-opens a fresh session and reads the run off the row — the
    # HTTP request is long gone. "" means "unset", the same string-for-unset
    # convention `paused_reason` uses, and resolves to the deployment defaults in
    # `stream_agent_response`. Persisting them (rather than passing them in
    # memory) also makes a turn resumed in another process after an approval or a
    # budget park use the same model and effort the user originally chose.
    requested_model: Mapped[str] = mapped_column(String(80), default="")
    requested_effort: Mapped[str] = mapped_column(String(16), default="")
    # The skill invoked for this one turn, or "" for none. Deliberately not a
    # ForeignKey: a run is a historical record and must survive the skill's
    # deletion, exactly as `requested_model` outlives a renamed deployment. The
    # body is resolved and spliced into this turn's instructions in
    # `agent_loop.resolve_directives`; it does not change the conversation.
    skill_id: Mapped[str] = mapped_column(String(36), default="")
    # The resolved args for that invocation, a JSON object keyed by arg name.
    # "" means none, the same string-for-unset convention the columns above use.
    skill_args_json: Mapped[str] = mapped_column(Text, default="")
    # The skill VERSION invoked, pinned at send time so a run parked over an edit
    # resumes with the body its sender saw. 0 means none / pre-pinning.
    skill_version: Mapped[int] = mapped_column(Integer, default=0)
    # Set when the workflow-less cron scheduler created this run, "" otherwise.
    # It is the marker `policy_scope_for_run` reads to run the turn at WORKFLOW
    # scope — a cron task fires with nobody watching, so a standing chat "always
    # allow" must not reach it. Not a FK: a run is a historical record and must
    # survive the cron's deletion, exactly as `skill_id` survives a skill's.
    cron_id: Mapped[str] = mapped_column(String(36), default="")
    #: Which part of the conversation's subject was on screen when this turn was
    #: sent — today, the file the project editor had open. "" for none.
    #:
    #: On the run and not on the conversation because it is a property of *this
    #: message*: "rename this function" means the file that was open when it was
    #: typed, and a turn that parks for an approval and resumes an hour later
    #: must come back to the same file rather than to whatever is open then.
    #: Same reason `requested_model` lives here.
    subject_focus: Mapped[str] = mapped_column(
        String(400), default="", server_default=""
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence"),
        Index("ix_run_events_run_sequence", "run_id", "sequence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(40))
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("workspace_id", "operation", "key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    operation: Mapped[str] = mapped_column(String(80))
    key: Mapped[str] = mapped_column(String(200))
    resource_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(120))
    object_key: Mapped[str] = mapped_column(String(500))
    byte_size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    error: Mapped[str] = mapped_column(Text, default="")
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    #: The space whose threads this source informs; "" means the workspace
    #: library. A thread in space S retrieves from `IN ("", S)` and an ordinary
    #: thread from `== ""` alone, so a space's files never surface in general
    #: chat. Retrieval filters on the Source row (every ranking arm already
    #: joins it for liveness), so `Chunk` needs no copy of this.
    space_id: Mapped[str] = mapped_column(
        String(36), default="", server_default="", index=True
    )
    deleted_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (Index("ix_chunks_workspace_source", "workspace_id", "source_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    token_count: Mapped[int] = mapped_column(Integer)
    # Dense half of hybrid retrieval. Nullable because a chunk exists the moment
    # it is written and is embedded shortly after — retrieval must work, lexically,
    # in the window between the two and if the embedding call fails entirely.
    embedding: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(64), default="")
    # Contextual Retrieval: a one-sentence situating blurb generated at ingest and
    # prepended to the text that gets embedded and scored. Stored separately from
    # `content` so provenance still quotes the author's words, not ours.
    context_prefix: Mapped[str] = mapped_column(Text, default="")
    # Length of this chunk in indexed terms — BM25's `b` normalisation needs it,
    # and NULL doubles as "never indexed", which is what lets retrieval reconcile
    # chunks written before the term index existed (or by a path that skipped it).
    # 0 is a real value: a chunk of pure stopwords indexes to nothing.
    lexical_length: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChunkTerm(Base):
    """One posting: term T occurs `tf` times in chunk C.

    An inverted index in ordinary rows rather than FTS5 or tsvector, so one
    ranking function serves both backends (RESEARCH.md #2).
    """

    __tablename__ = "chunk_terms"
    __table_args__ = (
        Index("ix_chunk_terms_workspace_term", "workspace_id", "term"),
        Index("ix_chunk_terms_chunk", "chunk_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"))
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunks.id"))
    term: Mapped[str] = mapped_column(String(64))
    tf: Mapped[int] = mapped_column(Integer)


class ConversationChunk(Base):
    """One retrievable window of a conversation's transcript — or its summary.

    What makes cross-thread history quotable: `Message` rows persist every word
    but nothing could search them, so the only channel between threads was the
    handful of extracted MemoryItems. A chunk packs consecutive messages of one
    conversation into a window sized for retrieval; `kind="summary"` marks the
    thread's single rolling summary row, kept in the same table so one hybrid
    search ranks both tiers at once.

    Deliberately no `owner_id` and no `space_id`: a chunk is exactly as visible
    as its conversation, and the Conversation row already answers that (shared,
    created_by, subject_id — and space_id when Spaces lands). Copying the answer
    here would be a second visibility rule that could disagree with the first;
    every search joins Conversation instead, the way document retrieval joins
    Source for liveness.

    Chunks are derived data. `content` is rebuildable from Message rows at any
    time (scripts/backfill_conversation_index.py does exactly that), so nothing
    here needs the supersession machinery memory has — a stale index is healed
    by reindexing, not versioned.
    """

    __tablename__ = "conversation_chunks"
    __table_args__ = (
        Index(
            "ix_conversation_chunks_ws_conversation",
            "workspace_id",
            "conversation_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id"), index=True
    )
    #: "chunk" for a transcript window, "summary" for the thread's one rolling
    #: summary row (ordinal 0, refreshed in place as the thread grows).
    kind: Mapped[str] = mapped_column(String(16), default="chunk")
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    #: The Message rows this window quotes — provenance, and the incremental
    #: watermark: indexing covers only messages no chunk has claimed yet.
    message_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    #: How many messages this row covers. On the summary row it is the thread's
    #: message count at the last refresh, which is what decides staleness.
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    #: created_at of the newest message covered — what a search renders as the
    #: quote's date, and what reconcile compares against Message.created_at.
    last_message_at: Mapped[datetime] = mapped_column(DateTime)
    # Dense half of hybrid search. Nullable for the same reason Chunk.embedding
    # is: a chunk must be lexically searchable in the window before the
    # embedding call runs, and if it never succeeds.
    embedding: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Tool(Base):
    __tablename__ = "tools"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(300))
    base_url: Mapped[str] = mapped_column(String(500))
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ToolGrant(Base):
    __tablename__ = "tool_grants"
    __table_args__ = (UniqueConstraint("agent_id", "tool_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    tool_id: Mapped[str] = mapped_column(ForeignKey("tools.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    tool_id: Mapped[str] = mapped_column(ForeignKey("tools.id"))
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    request_url: Mapped[str] = mapped_column(String(1000))
    response_status: Mapped[int] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    decided_by: Mapped[str] = mapped_column(String(36), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


#: What `AgentToolCall.decided_by` carries instead of a user id when a
#: conversation's approval *mode* is what let a call through. Defined here
#: rather than in `services/agent_loop`, which holds the rule that writes it,
#: because the column and the property that reads it both live in this module
#: and a constant cannot be imported upwards out of services.
MODE_DECIDER_PREFIX = "mode:"


class AgentToolCall(Base):
    """A function call issued by the LLM agent loop.

    Separate from ToolCall, which models the approval-gated external HTTP tool.
    Calls whose resolved policy is "ask" are written with status "proposed" and
    park the run until POST /api/agent-tool-calls/{id}/decision resolves them.
    """

    __tablename__ = "agent_tool_calls"
    __table_args__ = (
        # The Inbox's unbounded waiting-set query: WHERE workspace_id AND
        # status='proposed' ORDER BY created_at. Composite because the plain
        # workspace index degrades to a scan of every call the workspace ever
        # made once the filter includes status — and this table only grows.
        Index(
            "ix_agent_tool_calls_workspace_status_created",
            "workspace_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(24), default="succeeded")
    result_preview: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    # The Responses API call_id, kept so a resumed run can pair the tool output
    # with the function call the model emitted before the pause.
    call_id: Mapped[str] = mapped_column(String(80), default="")
    # What the call will do if approved — a unified diff for document edits, a
    # sentence for board moves. Computed before execution, so the approval card
    # can show the change rather than just the tool's name.
    proposal_preview: Mapped[str] = mapped_column(Text, default="")
    # Descriptors for the files this call produced — a matplotlib figure the
    # sandbox drew, saved as a workspace Source. Held as its own column rather
    # than parsed back out of `result_preview`, which is clipped to 500
    # characters and drops the artifact list first on a chatty run.
    artifacts_json: Mapped[str] = mapped_column(Text, default="[]")
    decided_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    @property
    def approved_by_mode(self) -> str:
        """The approval *mode* that let this call run, or "" when one did not.

        The readable half of `decided_by`, and the only half that leaves the
        API. The column holds either a user id or `mode:<mode>`, and a user id
        is not something a conversation needs to be told — `AuditEventOut`
        exposes no actor for the same reason. What a reader of a bypassed thread
        does need is which calls nobody looked at, which is exactly this.
        """
        value = self.decided_by or ""
        if not value.startswith(MODE_DECIDER_PREFIX):
            return ""
        return value[len(MODE_DECIDER_PREFIX) :]


class Space(Base):
    """A grouping of chat threads with standing context of its own.

    A space gives the threads inside it three things: custom instructions
    appended to every turn's system prompt, knowledge files whose retrieval is
    scoped to the space, and a memory shelf of its own (`MemoryItem.space_id`).
    Unlike a `Folder` it does hold content — the instructions — and unlike a
    subject it never narrows tools or leaves the rail.

    Workspace-shared, like folders and documents: every member sees every
    space. Privacy stays where ADR 0010 put it — on the thread
    (`Conversation.shared`) and the memory owner axis — so a space is a
    relevance boundary, not a visibility one.

    Deletion is destructive: the space's threads, sources and memories go with
    it (cascaded explicitly in `services/spaces.delete_space`). Detaching them
    instead would flip every space-scoped source to `space_id = ""` — the
    workspace library — silently widening retrieval, which is the one
    direction scoping must never fail toward.
    """

    __tablename__ = "spaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    #: Appended to the system prompt of every turn run in this space's threads,
    #: after the base/agent layer and before any skill injection. Blank means
    #: no injection — never an empty block.
    instructions: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SpaceTemplate(Base):
    """A reusable starting point for a space: instructions, written down once.

    A snapshot, not a link. The template copies a space's instructions at the
    moment it is saved, and instantiating it copies them forward into a new
    `Space` row — editing either afterwards moves neither, which is what makes
    "set up a client space the way we always do" reproducible rather than
    coupled. `agent_ids_json` follows the same rule: plain historical ids
    (never ForeignKeys, per the house convention for references that must
    outlive their target), recorded so the template can say which agents the
    playbook expects without a deleted agent breaking the template.
    """

    __tablename__ = "space_templates"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(500), default="")
    #: The standing instructions a space built from this template starts with.
    instructions: Mapped[str] = mapped_column(Text, default="")
    #: Agents this playbook expects, as a JSON list of plain ids ('[]' = none).
    agent_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Folder(Base):
    """Where a file is filed. Nests, and holds nothing of its own.

    A folder is a label on documents, not a container of them: the row carries
    no content, and `Document.title` stays unique per *workspace* rather than
    per folder. That is deliberate — the agent resolves a document by name
    (`documents.find_by_title`), so letting two folders each hold a "Notes"
    would make "edit Notes" a question the model cannot answer.
    """

    __tablename__ = "folders"
    __table_args__ = (Index("ix_folders_workspace_parent", "workspace_id", "parent_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    # Empty means the top level. Empty rather than NULL because every query here
    # groups by it, and NULL does not group with itself in SQL.
    parent_id: Mapped[str] = mapped_column(String(36), default="")
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Document(Base):
    """A document the agent can read and edit: plain text, or markdown+maths."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16), default="markdown")
    content: Mapped[str] = mapped_column(Text, default="")
    #: The folder this file sits in; empty is the top level, which is where
    #: everything written before folders existed — and everything the agent
    #: writes — lands. Filing is the user's job, so no tool sets this.
    folder_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DocumentVersion(Base):
    """A snapshot taken before each edit, so any agent change can be undone."""

    __tablename__ = "document_versions"
    __table_args__ = (Index("ix_document_versions_doc_created", "document_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(String(300), default="")
    #: Who caused this snapshot. Empty for rows written before the column
    #: existed, and for a workflow with no human behind it.
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Board(Base):
    """A kanban board. Columns are ordered names; cards live in one column."""

    __tablename__ = "boards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class BoardColumn(Base):
    __tablename__ = "board_columns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    board_id: Mapped[str] = mapped_column(ForeignKey("boards.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    position: Mapped[int] = mapped_column(Integer, default=0)


class BoardCard(Base):
    __tablename__ = "board_cards"
    __table_args__ = (Index("ix_board_cards_column_position", "column_id", "position"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    board_id: Mapped[str] = mapped_column(ForeignKey("boards.id"), index=True)
    column_id: Mapped[str] = mapped_column(ForeignKey("board_columns.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    labels_json: Mapped[str] = mapped_column(Text, default="[]")
    position: Mapped[int] = mapped_column(Integer, default=0)
    #: When this card was ticked off, or NULL for an open one. A timestamp
    #: rather than a boolean because "done" on its own cannot answer the
    #: question a checked list is actually asked — when did that happen, and was
    #: it before or after the thing that went wrong.
    #:
    #: It lives on every card, not only on the ones drawn as checkboxes, which
    #: is what lets a ticked item graduate into a kanban card without a
    #: migration: a todo list *is* a one-column board (services/artifacts/todos).
    done_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DbConnection(Base):
    """A user-configured database the agent can introspect and query."""

    __tablename__ = "db_connections"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    engine: Mapped[str] = mapped_column(String(20), default="postgres")
    # Fernet-encrypted SQLAlchemy URL; it carries the password, so it is never
    # returned by the API — only a redacted summary is.
    dsn_encrypted: Mapped[str] = mapped_column(Text, default="")
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(24), default="unknown")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Project(Base):
    """A multi-file code project the agent authors, bundled in the browser."""

    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    # "web" bundles with esbuild-wasm; "latex" compiles with the TeX engine.
    # Both are the same virtual filesystem — only the preview differs.
    kind: Mapped[str] = mapped_column(String(16), default="web")
    entry_path: Mapped[str] = mapped_column(String(400), default="index.tsx")
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ProjectFile(Base):
    """One file in a project's virtual filesystem."""

    __tablename__ = "project_files"
    __table_args__ = (UniqueConstraint("project_id", "path"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    path: Mapped[str] = mapped_column(String(400))
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class McpOAuthClient(Base):
    """What this deployment registered itself as, at one MCP server's auth server.

    MCP's auth story is OAuth 2.1 with *dynamic* client registration (RFC 7591):
    there is no console where an operator pastes a client id, because the whole
    point is that a user can add an arbitrary remote server and have it work. So
    the client credentials are discovered and minted at connect time, per server,
    and they have to be stored — which is what this table is.

    Keyed on (server_id, issuer) rather than server_id alone because a server is
    permitted to move its authorization server, and discovering a new issuer
    should mint a new registration rather than silently reuse credentials the new
    issuer never granted.
    """

    __tablename__ = "mcp_oauth_clients"
    __table_args__ = (UniqueConstraint("server_id", "issuer"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    server_id: Mapped[str] = mapped_column(ForeignKey("mcp_servers.id"), index=True)
    issuer: Mapped[str] = mapped_column(String(600))
    authorization_endpoint: Mapped[str] = mapped_column(String(600), default="")
    token_endpoint: Mapped[str] = mapped_column(String(600), default="")
    registration_endpoint: Mapped[str] = mapped_column(String(600), default="")
    client_id: Mapped[str] = mapped_column(String(400), default="")
    # Fernet, like every other credential in this schema. Public clients (PKCE,
    # no secret) leave this empty, which is the common case for MCP.
    client_secret_enc: Mapped[str] = mapped_column(Text, default="")
    # RFC 7592: lets us update or delete the registration we created.
    registration_access_token_enc: Mapped[str] = mapped_column(Text, default="")
    registration_client_uri: Mapped[str] = mapped_column(String(600), default="")
    redirect_uri: Mapped[str] = mapped_column(String(600), default="")
    scopes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class McpOAuthToken(Base):
    """One user's tokens for one MCP server.

    Per user, not per workspace, and that is the security-relevant part: an MCP
    server authorises the human, so sharing a workspace must not share a Linear
    account. Two people in one workspace get two rows and see their own issues.
    """

    __tablename__ = "mcp_oauth_tokens"
    __table_args__ = (UniqueConstraint("server_id", "user_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    server_id: Mapped[str] = mapped_column(ForeignKey("mcp_servers.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    access_token_enc: Mapped[str] = mapped_column(Text, default="")
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    scopes: Mapped[str] = mapped_column(Text, default="")
    # RFC 8707 resource indicator. Recorded because a token minted for this
    # resource must not be replayed against another one, and the only way to
    # check that later is to remember what it was minted for.
    resource: Mapped[str] = mapped_column(String(600), default="")
    status: Mapped[str] = mapped_column(String(24), default="connected")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SandboxSession(Base):
    """A server-side execution sandbox: one microVM, owned by one workspace.

    The row is the authority. `external_id` names a live machine at the provider,
    and the only way to reach one is to select this table by `workspace_id` —
    `sandbox.session.resolve_session` is the sole function that does so and it
    filters before it returns. Nothing accepts a provider id from a caller, which
    is what keeps one tenant from attaching to another tenant's sandbox.
    """

    __tablename__ = "sandbox_sessions"
    __table_args__ = (
        UniqueConstraint("provider", "external_id"),
        Index("ix_sandbox_sessions_workspace_status", "workspace_id", "status"),
        # The concurrency quota, enforced by the database rather than by counting.
        # A row that holds one of a workspace's slots names it here, and this
        # index is what refuses the second holder of the same slot — see
        # `sandbox.session._claim_a_slot` for why a count cannot do that job.
        # NULL is "holds no slot" and repeats freely, on both SQLite and
        # Postgres, so releasing a slot is writing NULL.
        Index(
            "uq_sandbox_sessions_workspace_slot",
            "workspace_id",
            "slot_index",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    # Optional: a session bound to a project mirrors that project's files.
    project_id: Mapped[str] = mapped_column(String(36), default="")
    created_by: Mapped[str] = mapped_column(String(36), default="")
    provider: Mapped[str] = mapped_column(String(24), default="e2b")
    external_id: Mapped[str] = mapped_column(String(120))
    template: Mapped[str] = mapped_column(String(80), default="")
    label: Mapped[str] = mapped_column(String(120), default="")
    # running -> paused (resumable) -> killed (terminal). "error" records a
    # provider failure at creation so the UI can explain it rather than retry.
    status: Mapped[str] = mapped_column(String(16), default="running")
    # Which of this workspace's numbered concurrency slots the row holds, or NULL
    # for a row that holds none (killed, errored, or a claim that was retired).
    # Unique per workspace, so two rows cannot hold the same slot however their
    # transactions interleave.
    slot_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # open | allowlist | none. Recorded per session, not just per workspace, so
    # changing the workspace default cannot retroactively widen a live sandbox.
    network_policy: Mapped[str] = mapped_column(String(16), default="open")
    allow_hosts_json: Mapped[str] = mapped_column(Text, default="[]")
    error: Mapped[str] = mapped_column(Text, default="")
    exec_count: Mapped[int] = mapped_column(Integer, default=0)
    wall_ms_used: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    killed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class SandboxExecution(Base):
    """One code or command execution, kept for the activity trail and for quotas.

    stdout/stderr are stored already clipped to `sandbox_max_output_bytes`: this
    table is a record of what happened, not a place to park megabytes of build log.
    """

    __tablename__ = "sandbox_executions"
    __table_args__ = (Index("ix_sandbox_executions_session", "session_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sandbox_sessions.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(36), default="")
    tool_call_id: Mapped[str] = mapped_column(String(36), default="")
    # "code" runs in the persistent interpreter kernel; "command" is a shell.
    kind: Mapped[str] = mapped_column(String(16), default="code")
    source: Mapped[str] = mapped_column(Text, default="")
    exit_code: Mapped[int] = mapped_column(Integer, nullable=True)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")
    # How many the provider handed back, including any the caps refused.
    artifact_count: Mapped[int] = mapped_column(Integer, default=0)
    # Descriptors for the ones that were actually stored, so reopening the panel
    # shows the figures again. Without this the console could only show a chart
    # in the seconds after it was drawn, and a reload lost it — which is the
    # same invisibility as never rendering it, arriving a minute later.
    artifacts_json: Mapped[str] = mapped_column(Text, default="[]")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SandboxTool(Base):
    """A workspace-defined tool the agent can call, executed in the session
    sandbox under this tool's own egress allowlist and approval policy.

    The slug ``name`` is unique per workspace and is the name the model calls, so
    it must not collide with a builtin (run_python, …) — enforced at the create
    route, which is the only writer. ``egress_hosts_json`` is the ONLY hosts an
    execution of this tool may reach: it tightens the workspace sandbox default
    and can never widen policy.py's metadata/RFC1918/loopback denials, which are
    reapplied unconditionally by egress_rules. ``approval`` may only tighten.
    """

    __tablename__ = "sandbox_tools"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    description: Mapped[str] = mapped_column(Text, default="")
    #: JSON Schema (object) shown to the model as the tool's parameters.
    input_schema_json: Mapped[str] = mapped_column(
        Text, default='{"type":"object","properties":{}}'
    )
    #: JSON list of argv tokens; a token may contain {{param}} placeholders.
    #: Executed as argv, never as a shell line — see services/sandbox/custom.py.
    argv_json: Mapped[str] = mapped_column(Text, default="[]")
    #: JSON list of hostnames. Empty = no egress (strictest). Non-empty = the
    #: only hosts this tool's execution may reach.
    egress_hosts_json: Mapped[str] = mapped_column(Text, default="[]")
    #: "inherit" | "always". "always" forces a human approval; tighten-only.
    approval: Mapped[str] = mapped_column(String(16), default="inherit")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


class McpServer(Base):
    """A configured MCP server: a stdio subprocess or a streamable HTTP endpoint."""

    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    # Short slug used to namespace this server's tools as mcp__<name>__<tool>.
    name: Mapped[str] = mapped_column(String(60))
    transport: Mapped[str] = mapped_column(String(16), default="stdio")
    command: Mapped[str] = mapped_column(String(400), default="")
    args_json: Mapped[str] = mapped_column(Text, default="[]")
    # Fernet-encrypted JSON: env vars for stdio, headers for HTTP. Both routinely
    # carry API keys, so neither is stored in the clear.
    secrets_encrypted: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(600), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(24), default="unknown")
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_connected_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class McpTool(Base):
    """A tool discovered on an MCP server, cached so the agent loop stays sync."""

    __tablename__ = "mcp_tools"
    __table_args__ = (UniqueConstraint("server_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    server_id: Mapped[str] = mapped_column(ForeignKey("mcp_servers.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    input_schema_json: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ToolPolicy(Base):
    """Per-workspace, *per-scope* approval policy for an agent tool.

    Absent a row, a tool's policy comes from ToolSpec.read_only: read-only tools
    run unattended, write-capable tools ask. A row set by the user — typically
    via "always allow" on an approval card — wins over that default.

    `scope` is the fix for the residual risk ADR 0007 called the sharpest. The
    grant used to be workspace-wide, so one click of "always allow" on
    `send_email` in a chat authorised every future *scheduled, unattended*
    workflow to send email forever — and a standing allow removes the approval
    park, which is the only containment prompt injection has to get past. A
    grant is now recorded against the situation it was given in:

    - ``chat``      a person is typing and will see what happens next.
    - ``workflow``  a compiled DAG is executing, possibly at 3am with nobody
                    watching.

    Every row written before this column existed is `chat`, which is exactly
    what those grants meant when they were made. `resolve_policy` reads them
    (agent_loop.py) and is the single place the two scopes are compared.

    `owner_id` is the second axis, added by ADR 0010 once a workspace could hold
    two people. `scope` says *when* a grant applies; `owner_id` says *to whom*.
    They are orthogonal — a personal workflow grant is a coherent thing to want —
    so it joins the key beside `scope` rather than becoming a third scope value.
    """

    __tablename__ = "tool_policies"
    # Scope joins the key rather than replacing `tool_name`: one tool can carry
    # a different verdict in each situation, which is the entire point. Owner
    # joins it for the same reason one axis further out.
    __table_args__ = (
        UniqueConstraint("workspace_id", "owner_id", "tool_name", "scope"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(120))
    policy: Mapped[str] = mapped_column(String(16), default="ask")
    #: Whose grant this is. "" is the workspace's, and only an owner may write
    #: one; anything else is a user id. "Always allow" on an approval card writes
    #: a personal row, so accepting the consequence of a standing grant no longer
    #: accepts it on every colleague's behalf. Same sentinel-not-NULL argument as
    #: `MemoryItem.owner_id`.
    owner_id: Mapped[str] = mapped_column(
        String(36), default="", server_default="", index=True
    )
    # chat | workflow. Defaults to chat so a caller that does not know about
    # scopes keeps writing the grant it always wrote.
    scope: Mapped[str] = mapped_column(String(16), default="chat", server_default="chat")
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class IntegrationAccount(Base):
    __tablename__ = "integration_accounts"
    __table_args__ = (UniqueConstraint("workspace_id", "provider", "external_account"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    provider: Mapped[str] = mapped_column(String(24))
    external_account: Mapped[str] = mapped_column(String(320), default="")
    scopes: Mapped[str] = mapped_column(Text, default="")
    access_token_enc: Mapped[str] = mapped_column(Text, default="")
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="connected")
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class OAuthState(Base):
    __tablename__ = "oauth_states"
    __table_args__ = (UniqueConstraint("state"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    provider: Mapped[str] = mapped_column(String(24))
    state: Mapped[str] = mapped_column(String(64))
    # MCP reuses this table rather than growing a parallel one, because the
    # CSRF-state machinery is identical and two implementations of it would be
    # one too many. `provider` is only 24 chars and a server id is 36, so the
    # server gets its own column instead of being packed into the provider slug.
    server_id: Mapped[str] = mapped_column(String(36), default="")
    # PKCE (RFC 7636) verifier, encrypted. It must live server-side: the whole
    # point of PKCE is that whoever intercepts the authorization code cannot
    # redeem it without this value, which fails if it round-trips via the browser.
    pkce_verifier_enc: Mapped[str] = mapped_column(Text, default="")
    redirect_uri: Mapped[str] = mapped_column(String(600), default="")
    # The authorization server this flow was started against. Without it the
    # callback has to guess which registration built the authorize URL, and a
    # server that rotates its issuer mid-flow gets handed the code and the PKCE
    # verifier for the issuer it replaced. See migration 0018.
    issuer: Mapped[str] = mapped_column(String(600), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SyncJob(Base):
    __tablename__ = "sync_jobs"
    __table_args__ = (Index("ix_sync_jobs_workspace_account", "workspace_id", "account_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("integration_accounts.id"), index=True)
    connector: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="queued")
    cursor_json: Mapped[str] = mapped_column(Text, default="{}")
    stats_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_workspace_created", "workspace_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(100))
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str] = mapped_column(String(36))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MemoryItem(Base):
    __tablename__ = "memory_items"
    __table_args__ = (
        # `owner_id` joins the key rather than replacing anything: my personal
        # value for a claim and the workspace's shared value are two rows on one
        # claim key, which is what makes supersession stay inside a scope. See
        # ADR 0010 — without it, `_retire` retires your fact when I correct mine.
        # `space_id` joins it for the same reason along the space axis: a
        # correction made inside a space retires the space's row, never the
        # workspace-global one.
        UniqueConstraint("workspace_id", "owner_id", "space_id", "kind", "normalized_key"),
        Index("ix_memory_items_workspace_status", "workspace_id", "status"),
        # Serves recall()'s capped vector scan: workspace + status + ORDER BY
        # updated_at DESC LIMIT n. Declared here as well as in migration
        # 0011_memory_depth, because development and test schemas come from
        # create_all — without it those environments run the scan unindexed, and
        # `alembic revision --autogenerate` would propose dropping the index for
        # not appearing in the model metadata.
        Index(
            "ix_memory_items_ws_status_updated",
            "workspace_id",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("conversations.id"), nullable=True, index=True
    )
    #: Whose memory this is. "" means the workspace's — shared with every member
    #: — and any other value is a user id. Stamped from the visibility of the
    #: conversation it was learned in (`Conversation.shared`), so a memory is
    #: exactly as visible as the thread it came from and no more.
    #:
    #: A sentinel rather than a nullable foreign key, and that is load-bearing:
    #: both SQLite and Postgres treat NULLs as distinct inside a unique index, so
    #: a nullable column would silently stop constraining the shared rows and let
    #: two live rows sit on one claim key. ADR 0005 chose NULL for
    #: `sandbox_sessions.slot_index` for exactly that property; here it is the
    #: bug. "" collides with "" on both engines.
    owner_id: Mapped[str] = mapped_column(
        String(36), default="", server_default="", index=True
    )
    #: The space this was learned in; "" means workspace-global. The second
    #: scope axis beside `owner_id`, same shape for the same ADR 0010 reasons:
    #: a sentinel and never NULL because it joins the unique key. Stamped from
    #: the conversation, never from a request field. Recall inside a space sees
    #: `IN ("", space)`; outside, `== ""` — space is a relevance scope, not a
    #: privacy scope, which stays the owner axis's job.
    space_id: Mapped[str] = mapped_column(
        String(36), default="", server_default="", index=True
    )
    run_id: Mapped[str] = mapped_column(String(36), default="")
    kind: Mapped[str] = mapped_column(String(24), default="fact")
    content: Mapped[str] = mapped_column(Text)
    normalized_key: Mapped[str] = mapped_column(String(200))
    entity_names_json: Mapped[str] = mapped_column(Text, default="[]")
    message_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    importance: Mapped[int] = mapped_column(Integer, default=1)
    # active | deleted (user tombstone) | superseded (a newer claim replaced it).
    # Superseded rows drop out of recall through the same _active() chokepoint as
    # deletions, so no scoring code has to learn about them.
    status: Mapped[str] = mapped_column(String(16), default="active")
    # The memory that replaced this one. Kept rather than discarded so the history
    # of a changing fact is auditable — "what did it used to think, and when".
    superseded_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    embedding: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class GraphProjection(Base):
    __tablename__ = "graph_projections"
    __table_args__ = (UniqueConstraint("workspace_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="stale")
    version: Mapped[str] = mapped_column(String(64), default="")
    entity_count: Mapped[int] = mapped_column(Integer, default=0)
    edge_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    built_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class GraphEntity(Base):
    __tablename__ = "graph_entities"
    __table_args__ = (
        UniqueConstraint("workspace_id", "normalized_name"),
        Index("ix_graph_entities_workspace_mentions", "workspace_id", "mention_count"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200))
    entity_type: Mapped[str] = mapped_column(String(40), default="concept")
    mention_count: Mapped[int] = mapped_column(Integer, default=0)
    source_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    chunk_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    memory_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class GraphEdge(Base):
    __tablename__ = "graph_edges"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "from_entity_id",
            "to_entity_id",
            "relation",
        ),
        Index("ix_graph_edges_workspace_weight", "workspace_id", "weight"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    from_entity_id: Mapped[str] = mapped_column(ForeignKey("graph_entities.id"), index=True)
    to_entity_id: Mapped[str] = mapped_column(ForeignKey("graph_entities.id"), index=True)
    relation: Mapped[str] = mapped_column(String(80), default="co_occurs")
    weight: Mapped[int] = mapped_column(Integer, default=1)
    confidence: Mapped[float] = mapped_column(Float, default=0.25)
    source_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    chunk_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    memory_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(500), default="")
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (
        UniqueConstraint("dataset_id", "version"),
        Index("ix_dataset_versions_workspace_dataset", "workspace_id", "dataset_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    format: Mapped[str] = mapped_column(String(20))
    schema_json: Mapped[str] = mapped_column(Text)
    row_count: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Skill(Base):
    """A reusable, named instruction block a person authors once and invokes per turn.

    Mirrors the `Dataset`/`DatasetVersion` split: this row holds the *current*
    content plus a `version` pointer, and each edit appends an immutable
    `SkillVersion` snapshot so prior versions stay restorable. An edit that
    changes nothing (same `content_hash`) writes no new version.

    `shared` is the sharing gate's one bit: a member may author a private skill,
    but flipping this True is owner/admin-only, enforced in `api/skills.py`. The
    slug `name` is unique per workspace and is what the composer's "/name" picker
    types.
    """

    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    #: The instructions injected into a turn, with literal `{{ name }}` placeholders.
    body: Mapped[str] = mapped_column(Text)
    #: JSON list of SkillArg-shaped param declarations. "[]" means no args.
    args_json: Mapped[str] = mapped_column(Text, default="[]")
    shared: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Current version number, bumped on every content edit; SkillVersion retains
    #: each one.
    version: Mapped[int] = mapped_column(Integer, default=1)
    #: sha256 of (title | description | body | args_json). An edit that leaves it
    #: unchanged is a no-op — no version, no new snapshot.
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SkillVersion(Base):
    """An immutable snapshot of a skill at one version, retained and restorable.

    Append-only, like `DatasetVersion`: a restore reads a snapshot and applies it
    as a *new* version rather than mutating history, so the trail of what a skill
    has been stays intact.
    """

    __tablename__ = "skill_versions"
    __table_args__ = (
        UniqueConstraint("skill_id", "version"),
        Index("ix_skill_versions_workspace_skill", "workspace_id", "skill_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    body: Mapped[str] = mapped_column(Text)
    args_json: Mapped[str] = mapped_column(Text, default="[]")
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    #: Who authored this version. Empty for rows predating the column, mirroring
    #: `DocumentVersion.created_by`.
    created_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Dashboard(Base):
    """One saved view over one dataset: a query and how to draw its answer.

    A dashboard is small on purpose. It holds a single visualization rather than
    a canvas of them, because the thing a user arranges is their *home screen* —
    see `DashboardPin`, where the grid lives — and a dashboard is one tile on it.
    That keeps the object the agent authors the same size as the object the user
    moves, so "make me a chart of X" produces exactly one new thing.
    """

    __tablename__ = "dashboards"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(500), default="")
    spec_json: Mapped[str] = mapped_column(Text)
    # Set when this dashboard was produced by binding a template. Kept with the
    # bindings that produced it because the stored spec names the *dataset's*
    # columns, not the template's: without the map, a spec grouping by
    # "territory" cannot be traced back to a template that declared "region",
    # and re-binding the definition to next quarter's data is guesswork.
    #
    # Deliberately not a ForeignKey. Deleting a template must not take the
    # working dashboards it produced off anyone's screen, so the delete clears
    # this column instead of cascading — and a constraint that the application
    # satisfies by nulling the value buys nothing over the index, at the cost of
    # a SQLite table rewrite in the migration that adds it.
    template_id: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )
    bindings_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DashboardTemplate(Base):
    """A dashboard definition that is not yet pointed at any data.

    The point of a template is the *contract*, not the layout. `required_columns`
    declares the shape a dataset must have for this definition to mean anything —
    column names and types — and `spec_json` is written against those declared
    names rather than against any real dataset's. Binding supplies the map from
    declared name to actual column, and a binding that does not satisfy the
    contract is refused there and then (see services/dashboards/binding.py).

    That is the whole reason the contract is stored rather than inferred. A
    template whose requirements were read off whichever dataset it was first
    built from would accept any dataset that happened to parse, and the mismatch
    would surface as an empty chart on Monday morning instead of as an error at
    the moment somebody asked for something impossible.
    """

    __tablename__ = "dashboard_templates"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(500), default="")
    required_columns_json: Mapped[str] = mapped_column(Text)
    spec_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DashboardPin(Base):
    """One tile on one person's home screen.

    Per *user*, not per workspace. A workspace's dashboards are shared — the
    agent authors them and anyone may run them — but which of them you keep in
    front of you, and where, is a personal arrangement: two people watching the
    same workspace should not fight over one layout, and one of them tidying
    their screen should not rearrange everybody else's.

    The grid geometry lives here rather than on the dashboard for the same
    reason. `grid_x`/`grid_w` are columns on a fixed 12-column grid and
    `grid_y`/`grid_h` are rows, which is the vocabulary every grid layout in the
    web app already speaks; storing them as integers rather than pixels is what
    lets the same arrangement render on a laptop and a wide monitor.
    """

    __tablename__ = "dashboard_pins"
    __table_args__ = (
        # One pin per person per dashboard: pinning is a fact, not a log, so a
        # second pin of the same dashboard has to update the first rather than
        # put a duplicate tile on the screen.
        UniqueConstraint("user_id", "dashboard_id"),
        Index("ix_dashboard_pins_workspace_user", "workspace_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    dashboard_id: Mapped[str] = mapped_column(ForeignKey("dashboards.id"), index=True)
    grid_x: Mapped[int] = mapped_column(Integer, default=0)
    grid_y: Mapped[int] = mapped_column(Integer, default=0)
    grid_w: Mapped[int] = mapped_column(Integer, default=6)
    grid_h: Mapped[int] = mapped_column(Integer, default=4)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class GeneratedApp(Base):
    __tablename__ = "generated_apps"
    __table_args__ = (UniqueConstraint("slug"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(500), default="")
    visibility: Mapped[str] = mapped_column(String(20), default="private")
    app_type: Mapped[str] = mapped_column(String(20), default="dashboard")
    current_release_id: Mapped[str] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AppRelease(Base):
    __tablename__ = "app_releases"
    __table_args__ = (
        UniqueConstraint("app_id", "version"),
        Index("ix_app_releases_workspace_app", "workspace_id", "app_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    app_id: Mapped[str] = mapped_column(ForeignKey("generated_apps.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    manifest_json: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class Workflow(Base):
    """A stored automation: natural language on one side, a validated DAG on the other.

    `graph_json` is the compiled DAG (see services/workflows/dag.py) and
    `source_prompt` is the sentence it was compiled from. Both are kept because
    they answer different questions — the graph says what will run, the prompt
    says what someone asked for — and a recompile that drifts from the ask is
    only visible when you still have the ask.

    A workflow is a *program a scheduler may run without a human present*, which
    is why nothing here carries authority of its own. Nodes name tools; the
    workspace's `tool_policies` decide at execution time whether each one runs or
    parks. Compiling a workflow grants nothing.
    """

    __tablename__ = "workflows"
    __table_args__ = (Index("ix_workflows_workspace_status", "workspace_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    source_prompt: Mapped[str] = mapped_column(Text, default="")
    graph_json: Mapped[str] = mapped_column(Text)
    # Bumped on every recompile. Runs record the version they executed, so a
    # workflow edited on Tuesday does not rewrite the history of Monday's run.
    version: Mapped[int] = mapped_column(Integer, default=1)
    # draft -> active -> disabled. Only `active` is eligible for a trigger.
    status: Mapped[str] = mapped_column(String(16), default="draft")
    # manual | schedule. The compiler extracts a cron from "every Monday";
    # services/workflows/schedule.py dispatches it when an external cron calls
    # POST /api/workflows/tick.
    trigger_kind: Mapped[str] = mapped_column(String(16), default="manual")
    schedule_cron: Mapped[str] = mapped_column(String(120), default="")
    schedule_timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    # The minute this workflow was last dispatched for, truncated to the minute.
    # It is the claim, not a log: the ticker advances it with a conditional
    # UPDATE, so two ticks landing in the same minute — a retry, an overlapping
    # cron, two web dynos — produce one run between them and not two.
    last_dispatched_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class WorkflowTemplate(Base):
    """A stored automation shape, detached from any schedule.

    A snapshot of a workflow's `graph_json` and `source_prompt` — the whole
    definition, per the Workflow docstring — with everything that could make it
    *fire* deliberately left behind. Instantiating one re-validates the graph
    and writes a fresh draft `Workflow` with empty schedule fields, so a
    template can never smuggle a live cron into a workspace; someone has to
    review and activate the copy, exactly as if they had compiled it themselves.
    """

    __tablename__ = "workflow_templates"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(500), default="")
    graph_json: Mapped[str] = mapped_column(Text)
    source_prompt: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WorkflowRun(Base):
    """One execution of one workflow version.

    Deliberately shaped like `Run`: the same status vocabulary, the same
    `waiting_for_approval` park, and an optional `run_id` so a workflow that
    parks on a tool call reuses the run/event/approval machinery verbatim rather
    than growing a second one beside it.
    """

    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("ix_workflow_runs_workspace_status", "workspace_id", "status"),
        Index("ix_workflow_runs_workflow_created", "workflow_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    workflow_id: Mapped[str] = mapped_column(ForeignKey("workflows.id"), index=True)
    created_by: Mapped[str] = mapped_column(String(36), default="")
    # The workflow version this run executed. Not a live join — the row is the
    # record of what actually ran.
    workflow_version: Mapped[int] = mapped_column(Integer, default=1)
    graph_json: Mapped[str] = mapped_column(Text, default="")
    # manual | schedule. `schedule` means no human was at the diff, which is what
    # makes the approval park load-bearing rather than a courtesy.
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    # queued | running | waiting_for_approval | succeeded | failed | cancelled
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    # Mirrors `Run.paused_reason`: approval | budget | "". Carried here as well
    # as on the backing run because the workflow surface reads this table, and a
    # graph stopped by a spend ceiling that renders as "waiting for approval"
    # sends its owner looking for a card nobody wrote.
    paused_reason: Mapped[str] = mapped_column(String(16), default="")
    # The chat Run backing agent-step nodes and carrying the approval + RunEvent
    # stream. Null until a node needs one.
    run_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("runs.id"), nullable=True, index=True
    )
    input_json: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class WorkflowNodeRun(Base):
    """Per-node state for one workflow run — the reason a resume is cheap.

    The unique constraint on (workflow_run_id, node_key) is what makes "skip the
    nodes that already finished" a database fact rather than a convention: a
    resumed executor selects this table, and a node that succeeded cannot be
    inserted twice or run twice. That is the same property `Run.agent_state_json`
    buys for a chat turn, expressed per node instead of per turn.

    `policy` records *what authorised this node* — `allow` because the tool is
    read-only or the workspace granted a standing permission, or `ask` because a
    human decided on this specific call. Without it the audit trail cannot tell a
    3am unattended write apart from one somebody approved, and those are very
    different events.
    """

    __tablename__ = "workflow_node_runs"
    __table_args__ = (
        UniqueConstraint("workflow_run_id", "node_key"),
        Index("ix_workflow_node_runs_run_status", "workflow_run_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    workflow_run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id"), index=True
    )
    # The node's id inside graph_json, not a foreign key — the graph is a
    # document, and this is the join back into it.
    node_key: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(16), default="tool")
    tool_name: Mapped[str] = mapped_column(String(120), default="")
    # pending | running | waiting_for_approval | succeeded | failed | skipped
    status: Mapped[str] = mapped_column(String(24), default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    # Arguments after upstream outputs were substituted — what actually ran, not
    # the template that produced it.
    arguments_json: Mapped[str] = mapped_column(Text, default="{}")
    output_json: Mapped[str] = mapped_column(Text, default="")
    policy: Mapped[str] = mapped_column(String(16), default="")
    agent_tool_call_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("agent_tool_calls.id"), nullable=True
    )
    error: Mapped[str] = mapped_column(Text, default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Cron(Base):
    """A personal recurring job an external cron fires unattended.

    Grounded on the workflow-schedule machinery (services/workflows/schedule.py):
    the same conditional-UPDATE claim on `last_dispatched_at`, the same CATCHUP
    window, armed by the same POST /api/workflows/tick. It carries no authority
    of its own — a `kind="task"` fire runs at WORKFLOW policy scope, so the first
    write-capable tool parks for a human exactly as a scheduled workflow does.
    """

    __tablename__ = "crons"
    __table_args__ = (Index("ix_crons_workspace_enabled", "workspace_id", "enabled"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(160))
    # task | message
    kind: Mapped[str] = mapped_column(String(16), default="task")
    # 5-field cron + IANA zone, validated exactly as a workflow trigger is
    # (services/workflows/validate.cron_error / cron_matches).
    schedule_cron: Mapped[str] = mapped_column(String(120), default="")
    schedule_timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # kind="task": re-run as a fresh unattended chat turn.
    prompt: Mapped[str] = mapped_column(Text, default="")
    # kind="message": posted verbatim into the target conversation.
    body: Mapped[str] = mapped_column(Text, default="")
    # The conversation kind="message" posts into, created-or-named on first fire.
    # "" until the first dispatch names one; not a FK for the same reason
    # Run.skill_id is not — the cron outlives a purged conversation.
    target_conversation_id: Mapped[str] = mapped_column(String(36), default="")
    # The atomic claim column, truncated to the minute. The ticker advances it
    # with a conditional UPDATE, so two ticks in one minute produce one fire.
    last_dispatched_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ModelUsage(Base):
    """One billable model call: what it spent, who caused it, and at what rate.

    Counts and identifiers only. No prompt, no completion, not one character of
    either — this table is read by an admin panel and kept far longer than a
    conversation is, so putting content here would quietly turn a cost ledger
    into a second, unregulated copy of everyone's messages.

    `run_id`, `conversation_id` and `user_id` are plain columns rather than
    foreign keys, for two reasons. An embedding of an uploaded document has no
    run and no conversation to point at, and a ledger row must outlive the run it
    describes — a deleted conversation must not be able to erase what it cost.
    `workspace_id` *is* a foreign key, because a row that belongs to no tenant
    can be neither shown nor scoped.

    The rate columns are the reason a historical row can be trusted. Cost is
    computed once, at write time, from the rate configured then; storing the rate
    beside it means a price change next quarter reprices nothing that already
    happened, and an operator can see which number produced which figure.
    `cost_usd` is null — never zero — when the model had no configured rate, so
    "we did not know the price" stays distinguishable from "it was free".
    """

    __tablename__ = "model_usage"
    __table_args__ = (
        # The admin panel's shape: one workspace, one time window, newest first.
        Index("ix_model_usage_workspace_created", "workspace_id", "created_at"),
        # "What did this run cost" — the runaway-loop question, asked by run.
        Index("ix_model_usage_workspace_run", "workspace_id", "run_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(36), default="")
    conversation_id: Mapped[str] = mapped_column(String(36), default="")
    user_id: Mapped[str] = mapped_column(String(36), default="")
    # What caused the call: chat | workflow_node | embedding | codegen |
    # context_blurb | memory_extraction | graph_extraction | workflow_compile.
    # Free text rather than an enum so a new caller records something honest
    # instead of failing a constraint mid-turn.
    operation: Mapped[str] = mapped_column(String(32), default="", index=True)
    provider: Mapped[str] = mapped_column(String(24), default="openai")
    model: Mapped[str] = mapped_column(String(120), default="")
    # `cached_input_tokens` is a subset of input_tokens and `reasoning_tokens` a
    # subset of output_tokens, exactly as the provider reports them. Summing the
    # four would double-count; they are kept apart so the split stays visible.
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    # USD per million tokens, frozen at write time. Null together with cost_usd.
    input_rate_usd_per_mtok: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cached_input_rate_usd_per_mtok: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    output_rate_usd_per_mtok: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class WorkspaceBudget(Base):
    """One workspace's spend ceiling, when an owner has set one (ADR 0008).

    A row here *replaces* the ceiling `Settings` configures, rather than
    narrowing it. That is what makes "raise the limit and resume" a thing an
    owner can do at 3am without a redeploy — which is the whole point of putting
    the ceiling somewhere writable — and it is defensible because
    `require_owner` in this product is the person paying the bill. A hosted
    multi-tenant deployment that wants an operator cap the tenant cannot lift
    should clamp with `min()` in `budget.effective_ceiling`; the ADR says so.

    A null ceiling column means *no limit of that kind*, exactly as an unset
    setting does. There is deliberately no third state meaning "inherit": the
    row is the whole answer, so reading one is never a question about what is
    configured somewhere else.
    """

    __tablename__ = "workspace_budgets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id"), unique=True, index=True
    )
    window_hours: Mapped[int] = mapped_column(Integer, default=24)
    usd_per_window: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    tokens_per_window: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Who last moved the ceiling. Plain column, not a foreign key: the record of
    # a limit change must outlive the account that made it.
    updated_by: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


# ---------------------------------------------------------------------------
# A human decision on a tool call is written once.
#
# The routes claim a parked call with `UPDATE ... WHERE status = 'proposed'` so
# that the database, not a Python `if`, picks the winner of two reviewers who
# both loaded the same open card (api/tools.py). This trigger states the same
# rule where nothing can route around it: once a row carries an answer, a later
# *contradicting* answer does not land. The direction is deliberate — the
# recorded decision survives — because the failure being prevented is an
# approval quietly replacing a denial, after which the tool the human refused
# runs anyway.
#
# Narrow on purpose. Only approved <-> denied is refused: `proposed -> approved`
# is the decision itself, and `approved -> executing | succeeded | failed` is
# the executor reporting on the very call that was approved.
_DECIDABLE_TABLES = ("tool_calls", "agent_tool_calls")


def _decision_is_final_sqlite(table: str) -> str:
    """SQLite cannot rewrite NEW, so the row is put back immediately after.

    That second UPDATE does not re-enter the trigger because SQLite leaves
    `PRAGMA recursive_triggers` off, which is also why the guard is written as
    AFTER rather than as a BEFORE ... RAISE(IGNORE): skipping the row would
    report 0 rows matched and every ORM flush would raise instead of the write
    simply having no effect.
    """
    return f"""
    CREATE TRIGGER IF NOT EXISTS {table}_decision_is_final
    AFTER UPDATE OF status ON {table}
    FOR EACH ROW
    WHEN OLD.status IN ('approved', 'denied')
     AND NEW.status IN ('approved', 'denied')
     AND NEW.status <> OLD.status
    BEGIN
        UPDATE {table}
           SET status = OLD.status,
               decided_by = OLD.decided_by,
               decided_at = OLD.decided_at
         WHERE id = OLD.id;
    END
    """


def _decision_is_final_postgresql(table: str) -> str:
    """Postgres rewrites NEW in place, so the row is only ever written once."""
    return f"""
    CREATE OR REPLACE FUNCTION {table}_decision_is_final() RETURNS trigger AS $$
    BEGIN
        IF OLD.status IN ('approved', 'denied')
           AND NEW.status IN ('approved', 'denied')
           AND NEW.status <> OLD.status THEN
            NEW.status := OLD.status;
            NEW.decided_by := OLD.decided_by;
            NEW.decided_at := OLD.decided_at;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    CREATE TRIGGER {table}_decision_is_final
    BEFORE UPDATE OF status ON {table}
    FOR EACH ROW EXECUTE FUNCTION {table}_decision_is_final();
    """


for _decidable in _DECIDABLE_TABLES:
    _table = Base.metadata.tables[_decidable]
    event.listen(
        _table,
        "after_create",
        DDL(_decision_is_final_sqlite(_decidable)).execute_if(dialect="sqlite"),
    )
    event.listen(
        _table,
        "after_create",
        DDL(_decision_is_final_postgresql(_decidable)).execute_if(
            dialect="postgresql"
        ),
    )


@event.listens_for(Session, "before_flush")
def _attach_orphan_workspaces(session: Session, _context: Any, _instances: Any) -> None:
    """Give every workspace being inserted an organization, if it arrived without one.

    `Workspace.organization_id` is NOT NULL because a workspace nothing governs
    is precisely the hole this tier closes, and a nullable column would let one
    back in every time somebody adds a `Workspace(name=...)` — of which there are
    already thirty in this repo, mostly in tests and eval scripts that have no
    opinion about organizations and should not have to acquire one.

    Enforcing the invariant here rather than at each of those sites is what makes
    it an invariant. There is no code path, present or future, that can insert an
    orphan: the constraint is satisfied where rows are born.

    The org this creates has **no members**, and that is the safe default rather
    than an oversight. An org with no admin cannot have its posture changed by
    anybody, so an accidentally-provisioned org grants no one power; the failure
    mode of the alternative — quietly enrolling whoever happens to be flushing —
    is somebody gaining authority they were never given. The real front door,
    `services.orgs.provision_org`, names a founder explicitly and is what signup
    and the backfill use.

    SQLAlchemy sorts inserts by inter-table foreign keys, so the organization row
    lands before the workspace that points at it, without a `relationship()`
    being declared.

    The id is passed explicitly rather than left to `Organization.id`'s column
    default, because a column default is evaluated *during* the INSERT — reading
    `org.id` here would yield None, and the workspace would be written pointing
    at nothing. That is a quiet failure on Postgres (a NULL that the NOT NULL
    catches) and it is the reason this is worth a sentence.
    """
    orphans = [
        obj
        for obj in session.new
        if isinstance(obj, Workspace) and not obj.organization_id
    ]
    for workspace in orphans:
        org_id = new_id()
        session.add(Organization(id=org_id, name=workspace.name))
        workspace.organization_id = org_id
