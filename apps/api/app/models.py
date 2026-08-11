from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
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
)
from sqlalchemy.orm import Mapped, mapped_column

# utcnow is re-exported so existing `from .models import utcnow` imports keep working.
from .clock import utcnow
from .database import Base


def new_id() -> str:
    return str(uuid.uuid4())


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
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
    """An invitation to join an existing workspace."""

    __tablename__ = "workspace_invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    email: Mapped[str] = mapped_column(String(320), index=True)
    role: Mapped[str] = mapped_column(String(24), default="member")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    invited_by: Mapped[str] = mapped_column(String(36), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Membership(Base):
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    instructions: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    citations_json: Mapped[str] = mapped_column(Text, default="[]")
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


class AgentToolCall(Base):
    """A function call issued by the LLM agent loop.

    Separate from ToolCall, which models the approval-gated external HTTP tool.
    Calls whose resolved policy is "ask" are written with status "proposed" and
    park the run until POST /api/agent-tool-calls/{id}/decision resolves them.
    """

    __tablename__ = "agent_tool_calls"

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
    decided_by: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Document(Base):
    """A text document the agent can read and edit: markdown or LaTeX."""

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16), default="markdown")
    content: Mapped[str] = mapped_column(Text, default="")
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
    artifact_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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
    """

    __tablename__ = "tool_policies"
    # Scope joins the key rather than replacing `tool_name`: one tool can carry
    # a different verdict in each situation, which is the entire point.
    __table_args__ = (UniqueConstraint("workspace_id", "tool_name", "scope"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(120))
    policy: Mapped[str] = mapped_column(String(16), default="ask")
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
        UniqueConstraint("workspace_id", "kind", "normalized_key"),
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


class Dashboard(Base):
    __tablename__ = "dashboards"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    dataset_id: Mapped[str] = mapped_column(ForeignKey("datasets.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(String(500), default="")
    spec_json: Mapped[str] = mapped_column(Text)
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
