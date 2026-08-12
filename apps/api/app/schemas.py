from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import ReasoningEffort


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Identity(ApiModel):
    user_id: str
    user_name: str
    workspace_id: str
    workspace_name: str
    role: str


class ModelProviderStatus(BaseModel):
    """What is answering. `scripted` is the offline test double, never production.

    `configured` no longer distinguishes a working install from a degraded one —
    the app refuses to start without a key — so it stays only because clients
    read it, and is now simply "a real provider is behind this".
    """

    provider: Literal["openai", "scripted"]
    configured: bool
    model: str
    # The per-turn controls the composer offers. `selectable_models` is the
    # deployment's allow-list (the scripted double lists only itself); the client
    # must not send a model outside it. `reasoning_efforts` is the effort ladder
    # for the dropdown and `default_effort` the one pre-selected — the deployment
    # default, so an untouched composer reproduces today's behaviour exactly.
    selectable_models: List[str] = []
    reasoning_efforts: List[str] = []
    default_effort: str = ""


class ScreenStatus(BaseModel):
    """The prompt-injection screen's posture — a minimal admin indicator.

    Deliberately does NOT carry `screen_proxy_url`: the proxy endpoint is a
    server-side destination, in the same class as WORKFLOW_CRON_SECRET, and
    bootstrap is a client surface. Enabled/mode/backend is all a client needs to
    show whether untrusted content is being screened and how hard.
    """

    enabled: bool
    mode: Literal["shadow", "enforce"]
    backend: Literal["builtin", "proxy"]


class BootstrapResponse(ApiModel):
    identity: Identity
    feature_flags: Dict[str, bool]
    default_agent_id: str
    model_provider: ModelProviderStatus
    screen: ScreenStatus


class SignupIn(ApiModel):
    email: str = Field(min_length=3, max_length=320)
    # Bounds only; the real policy lives in services/auth/passwords.py so the
    # signup and reset paths cannot drift apart.
    password: str = Field(min_length=1, max_length=4096)
    name: str = Field(default="", max_length=120)


class LoginIn(ApiModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=4096)


class PasswordResetRequestIn(ApiModel):
    email: str = Field(min_length=3, max_length=320)


class PasswordResetConfirmIn(ApiModel):
    token: str = Field(min_length=1, max_length=512)
    password: str = Field(min_length=1, max_length=4096)


class VerifyEmailIn(ApiModel):
    token: str = Field(min_length=1, max_length=512)


class AuthAcknowledgement(ApiModel):
    """The deliberately uninformative answer to signup and reset requests.

    Byte-identical whether or not the address has an account: any difference —
    body, status, or a materially different response time — turns the endpoint
    into an account-enumeration oracle.
    """

    status: Literal["ok"] = "ok"
    detail: str


class AuthSessionOut(ApiModel):
    user_id: str
    user_name: str
    user_email: str
    email_verified: bool
    workspace_id: str
    workspace_name: str
    role: str
    # The double-submit value the client must echo in the CSRF header on every
    # unsafe request. Readable only by our own origin's JavaScript (CORS), which
    # is exactly what makes it unavailable to a cross-site attacker.
    csrf_token: str


class WorkspaceMembershipOut(ApiModel):
    """One workspace the caller may select with ``X-Workspace-Id``.

    A membership, not a workspace: the list is what the caller belongs to, so
    it doubles as the set of ids the header will be believed for.
    """

    id: str
    name: str
    role: str
    # True for the workspace this very request resolved to, so a client can
    # render the current selection without repeating the API's tie-break.
    is_current: bool


class DevOverrideOut(ApiModel):
    """Whether the local one-click sign-in override is available.

    Public and unauthenticated on purpose: the login screen has to know whether
    to render the button before any session exists. Outside development/test,
    or when DEV_USER is unset, `enabled` is always false.
    """

    enabled: bool
    handle: str = ""


class AgentOut(ApiModel):
    id: str
    name: str
    description: str = ""
    #: The system prompt. Blank means the stock instructions are used.
    instructions: str
    enabled: bool = True
    #: The provisioned tool subset. None means every registry tool; a list —
    #: empty included — is an explicit grant. Serialized from
    #: `Agent.allowed_tools_json`, where "" spells None.
    allowed_tools: Optional[List[str]] = None
    created_at: datetime
    updated_at: datetime


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=2000)
    instructions: str = Field(min_length=1, max_length=20000)
    allowed_tools: Optional[List[str]] = None


class AgentUpdate(BaseModel):
    """A partial edit. None means "leave alone" on every field, which is why
    resetting the tool subset to "all tools" needs its own flag: a None
    `allowed_tools` cannot mean both."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=2000)
    instructions: Optional[str] = Field(default=None, min_length=1, max_length=20000)
    enabled: Optional[bool] = None
    allowed_tools: Optional[List[str]] = None
    clear_allowed_tools: bool = False


class SkillArg(BaseModel):
    """One skill parameter: the workflow `InputSpec` shape, minus workflow-only bits.

    A skill runs inside one chat turn, so it needs no `object`/`array` inputs and
    no reference grammar — only the scalar kinds a person types into a composer
    field. `required`/`default`/`choices` carry the same meaning they do for a
    workflow input: whether a run may omit it, what pre-fills it, and the closed
    set it must belong to.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    type: Literal["string", "number", "integer", "boolean"] = "string"
    label: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=500)
    required: bool = True
    default: Any = None
    choices: List[Any] = Field(default_factory=list)


class SkillOut(ApiModel):
    id: str
    name: str
    title: str
    description: str = ""
    body: str
    #: Deserialized from `Skill.args_json`.
    args: List[SkillArg] = []
    shared: bool = False
    version: int
    #: True when the caller may toggle `shared` (owner/admin). Lets the editor
    #: disable the control rather than surprise a member with a 403.
    can_share: bool = False
    #: True when the caller authored it; a member sees shared skills read-only.
    can_edit: bool = True
    created_at: datetime
    updated_at: datetime


class SkillCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=500)
    body: str = Field(min_length=1, max_length=20000)
    args: List[SkillArg] = Field(default_factory=list, max_length=20)
    #: Owner/admin only. A non-owner sending shared=True is refused 403 by
    #: create_skill — the sharing gate is enforced server-side, not silently.
    shared: bool = False


class SkillUpdate(BaseModel):
    """A partial edit. None means "leave alone" on every field."""

    title: Optional[str] = Field(default=None, min_length=1, max_length=160)
    description: Optional[str] = Field(default=None, max_length=500)
    body: Optional[str] = Field(default=None, min_length=1, max_length=20000)
    args: Optional[List[SkillArg]] = Field(default=None, max_length=20)
    #: Owner-only; a member sending it is refused 403.
    shared: Optional[bool] = None


class SkillVersionOut(ApiModel):
    id: str
    version: int
    title: str
    created_at: datetime


class ToolInfoOut(ApiModel):
    """One registry tool, for the provisioning checklist."""

    name: str
    description: str
    read_only: bool
    #: The registry family the tool ships with ("core", "mcp", "sandbox", …),
    #: so the checklist can group rather than dump sixty checkboxes.
    family: str


class ConversationCreate(BaseModel):
    title: str = Field(default="New conversation", max_length=200)


#: How much a conversation asks before acting. Spelled out here rather than
#: imported from `services.agent_loop`, matching `ToolPolicyRequest.scope`: the
#: wire contract and the policy engine are allowed to be described twice, and
#: schemas do not reach into services.
ApprovalMode = Literal["ask_writes", "ask_all", "auto_writes"]


class ConversationOut(ApiModel):
    id: str
    title: str
    #: The document this thread belongs to, or empty for an ordinary chat.
    #: `GET /api/conversations` only ever returns the empty case; the field is
    #: here because `POST /api/documents/{id}/conversation` returns the other.
    document_id: str = ""
    #: ask_writes | ask_all | auto_writes, governing this thread and no other.
    approval_mode: ApprovalMode = "ask_writes"
    created_at: datetime
    updated_at: datetime


class ApprovalModeRequest(BaseModel):
    """The body of `PUT /api/conversations/{id}/approval-mode`.

    No `remember` and no scope: a mode is already as narrow as it gets, and the
    only thing it can be remembered against is the conversation it is set on.
    """

    mode: ApprovalMode


class Citation(BaseModel):
    chunk_id: str
    source_id: str
    filename: str
    ordinal: int
    excerpt: str
    score: float
    #: Set only for a web source, whose provenance is a page rather than an
    #: indexed passage. `chunk_id` is then a synthetic "web:<digest>" that names
    #: no Chunk row, so this URL is the only address a reader can follow — and
    #: without the field declared here FastAPI strips it from the response and
    #: the citation becomes unverifiable, which is the one thing this product
    #: promises its citations never are.
    url: Optional[str] = None


class CitationCheck(ApiModel):
    """The citation validator's verdict on one answer.

    Exactly `services.citations.CitationReport.to_dict()` plus its one-line
    summary. Nothing is added here: the validator is deliberately a pure
    function with its own tests, and a field invented at the API boundary would
    be a claim no test covers.
    """

    #: How many passages the model was actually handed.
    evidence_count: int
    #: How many [n] markers the answer contained, duplicates included.
    marker_count: int
    #: Passage numbers the answer cited that exist. Sorted, deduplicated.
    cited: List[int]
    #: Passage numbers the answer cited that do not exist — fabricated.
    out_of_range: List[int]
    #: Supplied passages the answer never cited. Not a violation.
    uncited: List[int]
    #: Citation-shaped brackets that could not be parsed, e.g. "[1,]".
    malformed: List[str]
    #: False when anything was fabricated or malformed. Uncited does not fail.
    valid: bool
    summary: str


class MessageOut(ApiModel):
    id: str
    run_id: str
    role: str
    content: str
    citations: List[Citation] = []
    #: None means this answer was never checked — a denied tool call, a budget
    #: park — which is a different fact from "checked and found clean".
    citation_report: Optional[CitationCheck] = None
    created_at: datetime


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    agent_id: Optional[str] = None
    # Per-turn overrides, all optional — absent means the deployment defaults, so
    # a client that never sends them behaves byte-identically to today.
    #: Model to run this turn on. Free string here (pydantic cannot see the
    #: allow-list), validated against `settings.selectable_models` in the endpoint.
    model: Optional[str] = None
    #: Reasoning effort for this turn. A `ReasoningEffort` Literal, so a value off
    #: the ladder is refused as 422 by pydantic with no endpoint code.
    effort: Optional[ReasoningEffort] = None
    #: Shortcut for the lowest-latency *honest* effort. It maps to "low", not
    #: "none": the small models reject "none" outright (see services/model.py), so
    #: "fast" must not promise a setting some models cannot honour. An explicit
    #: `effort` always wins over `fast`.
    fast: bool = False
    #: Invoke this skill for this turn only. Must be visible to the caller (own or
    #: shared). Absent = today's behaviour exactly; the skill's body is spliced
    #: into the turn's instructions and does not change the conversation.
    skill_id: Optional[str] = None
    #: Values for the skill's declared args, keyed by name. Validated against the
    #: skill's `args` at send time, then substituted into the body before injection.
    skill_args: Optional[Dict[str, Any]] = None


class RunOut(ApiModel):
    id: str
    conversation_id: str
    agent_id: str
    status: str
    # Why a `waiting_for_approval` run is parked: `approval` for a proposed tool
    # call, `budget` for the spend ceiling (ADR 0008), "" when it is not parked.
    # The status alone cannot tell them apart, and they are resolved by different
    # people doing different things.
    paused_reason: str = ""
    created_at: datetime


class SendMessageResponse(BaseModel):
    message: MessageOut
    run: RunOut
    replayed: bool = False


class SourceOut(ApiModel):
    id: str
    filename: str
    media_type: str
    byte_size: int
    status: str
    error: str
    chunk_count: int
    created_at: datetime


class ChunkOut(ApiModel):
    id: str
    source_id: str
    ordinal: int
    content: str
    char_start: int
    char_end: int
    filename: str


class ApprovalRequest(BaseModel):
    decision: Literal["approved", "denied"]


class ToolCallOut(ApiModel):
    id: str
    run_id: str
    conversation_id: str
    tool_id: str
    tool_name: str
    status: str
    request_url: str
    response_status: Optional[int]
    response_body: str
    error: str
    created_at: datetime


class AgentApprovalRequest(BaseModel):
    decision: Literal["approved", "denied"]
    # "Always allow this tool" — persists the decision as a workspace policy so
    # future calls to the same tool skip the prompt.
    remember: bool = False
    # A partial approval of a document edit: the indices of the hunks the
    # reviewer accepted, in the order `GET /api/documents-pending` returned
    # them. Omitted means the whole proposal, which is what every other tool
    # and every all-or-nothing card sends. Rejecting every hunk is a denial and
    # must be sent as one, so an empty list is refused rather than quietly
    # applying nothing under an "approved" decision.
    accepted_hunks: Optional[List[int]] = Field(default=None, min_length=1)
    # The values a person typed at a workflow `manual` node, keyed by field name.
    # They ride the same decision the Proceed button sends, become that node's
    # output object, and are read downstream as `{{ node.output.field }}`. Only a
    # manual node's parked call reads them; an ordinary tool approval ignores the
    # field, exactly as a tool approval ignores `accepted_hunks`.
    inputs: Optional[Dict[str, Any]] = None


class ToolArtifact(ApiModel):
    """A file a tool call produced, saved as a workspace `Source`.

    One descriptor from `services/sandbox/outputs.persist_artifacts`. Typed here
    rather than passed through as a free dict now that a client *renders* it: an
    untyped blob is a contract nobody can check, and the field that went missing
    for a year was `url`.
    """

    #: The `Source` row holding the bytes, in this workspace.
    id: str
    #: "png", "jpeg", "svg", "chart", … — what the sandbox called it.
    kind: str
    mime: str
    bytes: int
    #: Path under the API root that streams the bytes, e.g.
    #: `/api/sources/{id}/content`. Relative: the origin is the client's.
    url: str
    #: Read from the file header when the format carries one, so a caller can
    #: reserve the right box before the image arrives.
    width: Optional[int] = None
    height: Optional[int] = None
    #: A structured chart description, when the provider supplied one.
    chart: Optional[Any] = None

    @classmethod
    def collect(cls, items: Any) -> List[ToolArtifact]:
        """Descriptors to models, dropping whatever does not fit.

        Tolerant on purpose, and in both directions: these arrive from a stored
        JSON column that predates this model, and from a provider that may one
        day describe something new. A listing of tool calls must not 500 because
        one row from last year has no `url` — the card just shows no picture,
        which is what every card did before this existed.
        """
        if not isinstance(items, list):
            return []
        kept: List[ToolArtifact] = []
        for item in items:
            try:
                kept.append(cls.model_validate(item))
            except ValidationError:
                continue
        return kept


class AgentToolCallOut(ApiModel):
    id: str
    run_id: str
    conversation_id: str
    name: str
    arguments_json: str
    proposal_preview: str
    status: str
    result_preview: str
    error: str
    latency_ms: int
    #: What the call left behind that is a file rather than a sentence.
    artifacts: List[ToolArtifact] = []
    #: The approval mode that let this call run unattended — "auto_writes" — or
    #: "" when a person, a policy row or the tool's own default decided it.
    #:
    #: Here so a bypassed thread carries its own trail: the conversation is
    #: where the writes happened, so it is where "nobody saw this one" has to be
    #: legible, rather than only in an audit table a chat user never opens. The
    #: *mode* and not `decided_by`, which would put a user id on the wire.
    approved_by_mode: str = ""
    created_at: datetime


class ToolPolicyOut(ApiModel):
    tool_name: str
    policy: str
    #: Which situation this verdict answers for. Two rows can name the same tool,
    #: so a client that drops this field cannot tell them apart.
    scope: str
    #: When the grant was first made, and when its verdict last changed. A
    #: standing "always allow" is unattended write authority; reviewing one means
    #: knowing how old it is, not just that it exists.
    created_at: datetime
    updated_at: datetime


class ToolPolicyRequest(BaseModel):
    tool_name: str = Field(min_length=1, max_length=120)
    policy: Literal["ask", "allow", "deny"]
    # Defaults to chat, so a client written before scopes existed keeps setting
    # the grant it always set. Authorising unattended execution has to be asked
    # for by name — that is the entire point of the split.
    scope: Literal["chat", "workflow"] = "chat"


class FolderOut(ApiModel):
    id: str
    name: str
    #: Empty is the top level. See `models.Folder.parent_id` for why not null.
    parent_id: str
    updated_at: datetime


class FolderRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    parent_id: str = ""


class FolderUpdateRequest(BaseModel):
    """Rename, move, or both. Omitted means "leave it"; `parent_id: ""` is a move
    to the top level, which is why neither field can default to a usable value.
    """

    name: Optional[str] = Field(default=None, max_length=120)
    parent_id: Optional[str] = None


class DocumentFolderRequest(BaseModel):
    #: Always supplied. Empty files the document at the top level.
    folder_id: str = ""


class DocumentSummaryOut(ApiModel):
    id: str
    title: str
    kind: str
    characters: int
    folder_id: str
    updated_at: datetime


class DocumentOut(ApiModel):
    id: str
    title: str
    kind: str
    content: str
    folder_id: str
    updated_at: datetime


class DocumentRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = ""
    kind: Literal["text", "markdown"] = "markdown"
    folder_id: str = ""


class DocumentContentRequest(BaseModel):
    content: str


class DocumentVersionOut(ApiModel):
    id: str
    summary: str
    created_at: datetime


class BoardCardOut(ApiModel):
    id: str
    title: str
    body: str
    labels: List[str]
    #: Ticked off. Every card carries it, not only the ones a todo list draws as
    #: checkboxes, which is what lets a checked item graduate into a kanban card
    #: without changing anything about it. Defaulted, so a caller that predates
    #: the column keeps parsing.
    done: bool = False


class BoardColumnOut(ApiModel):
    id: str
    name: str
    cards: List[BoardCardOut]


class BoardOut(ApiModel):
    id: str
    name: str
    columns: List[BoardColumnOut]


class BoardRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    columns: List[str] = Field(default_factory=list)


class BoardCardRequest(BaseModel):
    column: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=300)
    body: str = ""
    labels: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Todo lists — the checkbox reading of a one-column board. See
# services/artifacts/todos.py for why these are a view and not an entity.


class TodoItemOut(ApiModel):
    id: str
    #: The board this item belongs to. Carried on the item because the endpoint
    #: that ticks one does not take it: checking something off should not
    #: require knowing which list it was on.
    list_id: str
    title: str
    body: str
    done: bool
    #: When it was ticked, or null while it is open.
    done_at: Optional[datetime] = None


class TodoListOut(ApiModel):
    id: str
    name: str
    items: List[TodoItemOut]


class TodoListCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class TodoItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = ""


class TodoItemUpdate(BaseModel):
    """A partial update. Every field omitted is a field left alone.

    `done` is the reason this route exists, and it is optional rather than
    required so that renaming an item does not make a caller restate whether it
    was finished — a PATCH that silently unticks something is exactly the bug a
    checklist cannot afford.
    """

    done: Optional[bool] = None
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    body: Optional[str] = None


class McpToolOut(ApiModel):
    id: str
    name: str
    description: str
    enabled: bool


class McpServerOut(ApiModel):
    id: str
    name: str
    transport: str
    command: str
    args: List[str]
    url: str
    enabled: bool
    status: str
    last_error: str
    # Whether env/headers are stored, never the values themselves.
    has_secrets: bool
    tools: List[McpToolOut]
    created_at: datetime


class McpServerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    transport: Literal["stdio", "http"] = "stdio"
    command: str = Field(default="", max_length=400)
    args: List[str] = Field(default_factory=list)
    url: str = Field(default="", max_length=600)
    # stdio env vars or HTTP headers; encrypted at rest and never read back.
    secrets: Dict[str, str] = Field(default_factory=dict)


class AuditEventOut(ApiModel):
    id: str
    action: str
    resource_type: str
    resource_id: str
    detail: Dict[str, Any]
    created_at: datetime


class HealthResponse(BaseModel):
    status: str
    database: str
    version: str


class GraphEntityOut(ApiModel):
    id: str
    name: str
    entity_type: str
    mention_count: int
    source_ids: List[str]
    chunk_ids: List[str]
    memory_ids: List[str] = []


class GraphEdgeOut(ApiModel):
    id: str
    from_entity_id: str
    to_entity_id: str
    relation: str
    # How much to trust `relation`. Co-occurrence edges carry a low floor; a
    # named relation from the extractor carries its own score. Without this a
    # client cannot tell "these words co-occur" from "A reports to B".
    confidence: float = 0.0
    weight: int
    source_ids: List[str]
    chunk_ids: List[str]
    memory_ids: List[str] = []


class IntegrationAccountOut(ApiModel):
    id: str
    provider: str
    external_account: str
    scopes: str
    status: str
    last_sync_at: Optional[datetime]
    created_at: datetime


class IntegrationProviderOut(BaseModel):
    provider: str
    configured: bool
    account: Optional[IntegrationAccountOut]


class IntegrationConnectOut(BaseModel):
    authorize_url: str


class GarminCredentialsIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class SyncJobOut(ApiModel):
    id: str
    connector: str
    status: str
    stats: Dict[str, int] = {}
    error: str
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
    created_at: datetime


class MemoryItemOut(ApiModel):
    id: str
    conversation_id: Optional[str]
    kind: str
    content: str
    entity_names: List[str]
    message_ids: List[str]
    importance: int
    created_at: datetime
    updated_at: datetime


class GraphOut(BaseModel):
    status: str
    version: str
    built_at: Optional[datetime]
    entities: List[GraphEntityOut]
    edges: List[GraphEdgeOut]


class DatasetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=500)
    source_id: str


class DatasetVersionCreate(BaseModel):
    source_id: str


class DatasetColumn(BaseModel):
    name: str
    type: Literal["boolean", "integer", "number", "string", "date", "datetime"]
    nullable: bool


class DatasetOut(ApiModel):
    id: str
    name: str
    description: str
    current_version: int
    version_id: str
    source_id: str
    format: str
    columns: List[DatasetColumn]
    row_count: int
    content_hash: str
    created_at: datetime
    updated_at: datetime


class DatasetFilter(BaseModel):
    field: str = Field(min_length=1, max_length=160)
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains"]
    value: Any


class DatasetMetric(BaseModel):
    field: Optional[str] = Field(default=None, max_length=160)
    operation: Literal["count", "sum", "avg", "min", "max"]
    label: str = Field(min_length=1, max_length=80)


class DatasetQuery(BaseModel):
    filters: List[DatasetFilter] = Field(default_factory=list, max_length=20)
    group_by: Optional[str] = Field(default=None, max_length=160)
    metrics: List[DatasetMetric] = Field(default_factory=list, max_length=10)
    order_by: Optional[str] = Field(default=None, max_length=160)
    order_direction: Literal["asc", "desc"] = "asc"
    limit: int = Field(default=100, ge=1, le=500)


class DatasetQueryResult(BaseModel):
    columns: List[str]
    rows: List[Dict[str, Any]]
    row_count: int
    truncated: bool
    elapsed_ms: float


class DashboardSpec(BaseModel):
    visualization: Literal["table", "bar", "line", "donut"] = "table"
    query: DatasetQuery = Field(default_factory=DatasetQuery)
    x_field: Optional[str] = Field(default=None, max_length=160)
    y_fields: List[str] = Field(default_factory=list, max_length=10)


class DashboardCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=500)
    dataset_id: str
    spec: DashboardSpec


class DashboardOut(ApiModel):
    id: str
    name: str
    description: str
    dataset_id: str
    spec: DashboardSpec
    template_id: str = ""
    bindings: Dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class DashboardRunOut(BaseModel):
    dashboard: DashboardOut
    result: DatasetQueryResult


class DashboardColumnRequirement(BaseModel):
    """One column a template requires of whatever dataset it is bound to."""

    name: str = Field(min_length=1, max_length=160)
    type: Literal["boolean", "integer", "number", "string", "date", "datetime"]
    description: str = Field(default="", max_length=240)


class DashboardTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=500)
    required_columns: List[DashboardColumnRequirement] = Field(
        min_length=1, max_length=20
    )
    spec: DashboardSpec


class DashboardTemplateOut(ApiModel):
    id: str
    name: str
    description: str
    required_columns: List[DashboardColumnRequirement]
    spec: DashboardSpec
    created_at: datetime
    updated_at: datetime


class DashboardTemplateBind(BaseModel):
    """Point a template at real data.

    `column_bindings` maps a name the template *declared* to a column the dataset
    actually has. A declared name absent from the map binds to the identical name
    in the dataset, so the common case — a dataset that already speaks the
    template's vocabulary — needs no map at all.
    """

    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=500)
    dataset_id: str
    column_bindings: Dict[str, str] = Field(default_factory=dict)


class DashboardPinUpdate(BaseModel):
    """Where a tile sits. Every field optional: pinning without a position puts
    the tile at the bottom of the grid, which is what "pin this" means before
    anyone has dragged anything."""

    grid_x: Optional[int] = Field(default=None, ge=0, le=11)
    grid_y: Optional[int] = Field(default=None, ge=0, le=200)
    grid_w: Optional[int] = Field(default=None, ge=1, le=12)
    grid_h: Optional[int] = Field(default=None, ge=1, le=12)


class DashboardPinOut(ApiModel):
    dashboard: DashboardOut
    grid_x: int
    grid_y: int
    grid_w: int
    grid_h: int
    pinned_at: datetime


class DashboardLayoutTile(BaseModel):
    dashboard_id: str
    grid_x: int = Field(ge=0, le=11)
    grid_y: int = Field(ge=0, le=200)
    grid_w: int = Field(ge=1, le=12)
    grid_h: int = Field(ge=1, le=12)


class DashboardLayoutUpdate(BaseModel):
    """A whole home screen in one write.

    Dragging a tile moves its neighbours, so a layout save that took one tile at
    a time would leave the grid inconsistent between requests — and a save that
    failed halfway would leave two tiles claiming one cell.
    """

    tiles: List[DashboardLayoutTile] = Field(max_length=60)


class GeneratedAppCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    slug: str = Field(min_length=3, max_length=80, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    description: str = Field(default="", max_length=500)
    visibility: Literal["private", "public"] = "private"
    app_type: Literal["dashboard", "code"] = "dashboard"
    dashboard_ids: List[str] = Field(default_factory=list, max_length=12)


class AppGenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    dataset_ids: List[str] = Field(default_factory=list, max_length=8)


class AppReleaseCreate(BaseModel):
    dashboard_ids: List[str] = Field(min_length=1, max_length=12)


class AppReleaseOut(ApiModel):
    id: str
    version: int
    status: str
    content_hash: str
    manifest: Dict[str, Any]
    created_at: datetime
    published_at: Optional[datetime]


class GeneratedAppOut(ApiModel):
    id: str
    name: str
    slug: str
    description: str
    visibility: Literal["private", "public"]
    app_type: str = "dashboard"
    current_release_id: Optional[str]
    releases: List[AppReleaseOut]
    created_at: datetime
    updated_at: datetime


class PublishedAppOut(BaseModel):
    name: str
    slug: str
    description: str
    version: int
    published_at: datetime
    manifest: Dict[str, Any]


