export type Identity = {
  user_id: string;
  user_name: string;
  workspace_id: string;
  workspace_name: string;
  role: string;
};

/** Who the session cookie resolves to, plus the CSRF value to echo back. */
export type AuthSession = {
  user_id: string;
  user_name: string;
  user_email: string;
  email_verified: boolean;
  workspace_id: string;
  workspace_name: string;
  role: string;
  /**
   * Double-submit token for unsafe methods. Held in memory only — it is
   * per-session and re-read from `GET /api/auth/me` after every page load.
   */
  csrf_token: string;
};

/**
 * The deliberately uninformative answer to signup and password-reset requests:
 * identical whether or not the address has an account, so the UI must never try
 * to read an outcome out of it.
 */
export type AuthAcknowledgement = {
  status: "ok";
  detail: string;
};

/**
 * One workspace the signed-in user belongs to. The id is safe to put in
 * `X-Workspace-Id`: the API returns memberships, and it re-checks the header
 * against those same rows on every request regardless.
 */
export type WorkspaceMembership = {
  id: string;
  name: string;
  role: string;
  /** True for the workspace the listing request itself resolved to. */
  is_current: boolean;
};

export type DevOverride = {
  enabled: boolean;
  handle: string;
};

/**
 * The prompt-injection screen's public posture — enough for a status
 * indicator, and no more. The proxy URL is a server secret and is deliberately
 * absent, the same discipline that keeps the cron secret out of bootstrap.
 *
 * - `enabled` off ⇒ untrusted content is never screened; behaviour is today's.
 * - `mode` "shadow" records verdicts and changes nothing; "enforce" escalates a
 *   flagged turn to the strictest approval posture so an injection cannot drive
 *   an auto-approved write.
 * - `backend` is the classifier answering: the built-in cheap model, or an
 *   external screen proxy.
 */
export type ScreenStatus = {
  enabled: boolean;
  mode: "shadow" | "enforce";
  backend: "builtin" | "proxy";
};

export type Bootstrap = {
  identity: Identity;
  default_agent_id: string;
  feature_flags: Record<string, boolean>;
  model_provider: {
    /** "scripted" is the test double; it never reaches a provider. */
    provider: "openai" | "scripted";
    /** True only when a real provider is answering. */
    configured: boolean;
    model: string;
    /** The deployment's allow-list a per-turn model override must be drawn from. */
    selectable_models: string[];
    /** The reasoning-effort Literal values, for the composer's effort dropdown. */
    reasoning_efforts: string[];
    /** The deployment default effort — what an unset per-turn effort resolves to. */
    default_effort: string;
  };
  /** The prompt-injection screen's posture, for a status indicator. */
  screen: ScreenStatus;
};

/**
 * Per-turn overrides for one message: a model off the deployment allow-list, a
 * reasoning effort, and the "fast" shortcut. All optional — absent fields leave
 * the deployment defaults in force, so an unchanged composer sends exactly what
 * it always did.
 */
export type MessageControls = {
  model?: string;
  effort?: string;
  fast?: boolean;
  /**
   * A skill to inject into this one turn, and the values for its declared args.
   * Per-turn like the model/effort above: absent means today's behaviour, and
   * the skill never becomes part of the conversation. `skillArgs` only rides the
   * wire when `skillId` is set.
   */
  skillId?: string;
  skillArgs?: Record<string, unknown>;
};

export type Conversation = {
  id: string;
  title: string;
  /**
   * The document this thread belongs to, or "" for an ordinary chat.
   *
   * `listConversations` only ever returns the empty case — a document's thread
   * is reached through its document, not through the rail — so this is the one
   * field on a Conversation that says which of the two surfaces it came from.
   */
  document_id: string;
  approval_mode: ApprovalMode;
  created_at: string;
  updated_at: string;
};

/**
 * How much one thread asks before acting. Per conversation, never per
 * workspace: a bypass switched on to get through one refactor must not still
 * be on next week, governing chats nobody turned it on for.
 *
 * - `ask_writes` — read-only tools run, writes park. The default.
 * - `ask_all` — everything parks, searches included.
 * - `auto_writes` — writes execute unattended. The bypass.
 */
export type ApprovalMode = "ask_writes" | "ask_all" | "auto_writes";

export type Citation = {
  chunk_id: string;
  source_id: string;
  filename: string;
  ordinal: number;
  excerpt: string;
  score: number;
};

/**
 * The citation validator's verdict on one answer — exactly the report in
 * apps/api/app/services/citations.py, no more.
 *
 * The contract the model is held to is "attach [n] after each claim supported
 * by passage n, and only use [n] markers that match supplied passages". This
 * says whether it did.
 */
export type CitationCheck = {
  /** How many passages the model was handed. */
  evidence_count: number;
  /** How many [n] markers the answer contained, duplicates included. */
  marker_count: number;
  /** Passage numbers cited that exist. */
  cited: number[];
  /** Passage numbers cited that do not exist — fabricated. */
  out_of_range: number[];
  /** Supplied passages never cited. Not a violation. */
  uncited: number[];
  /** Citation-shaped brackets that could not be parsed, e.g. "[1,]". */
  malformed: string[];
  /** False when anything was fabricated or malformed. */
  valid: boolean;
  summary: string;
};

export type Message = {
  id: string;
  run_id: string;
  role: "user" | "assistant" | "tool";
  content: string;
  citations: Citation[];
  /**
   * null means this answer was never checked — a denied tool call, a budget
   * park, a message written before the check existed. Not the same as "checked
   * and clean", and must never be rendered as one.
   */
  citation_report: CitationCheck | null;
  created_at: string;
};

export type Run = {
  id: string;
  conversation_id: string;
  agent_id: string;
  status: string;
  /**
   * Why a `waiting_for_approval` run stopped: `approval` for a proposed write,
   * `budget` for the spend ceiling, `""` when it is not parked at all.
   *
   * ADR 0008 made a budget park reuse the approval status on purpose, so this
   * is the *only* field that tells the two apart — and only one of them has a
   * card to click. A reader that branches on `status` alone will offer an
   * approve/deny decision for a run with nothing to approve.
   */
  paused_reason: string;
  created_at: string;
};

export type SendMessageResponse = {
  message: Message;
  run: Run;
  replayed: boolean;
};

export type Source = {
  id: string;
  filename: string;
  media_type: string;
  byte_size: number;
  /**
   * "stored" is a file the workspace holds but never ingested — a figure a
   * sandbox run drew. Deliberately not "ready", which means indexed and
   * quotable everywhere else, and which retrieval must not be led to believe
   * about a PNG.
   */
  status: "queued" | "processing" | "ready" | "stored" | "failed" | "deleted";
  error: string;
  chunk_count: number;
  created_at: string;
};

export type ProvenanceChunk = {
  id: string;
  source_id: string;
  ordinal: number;
  content: string;
  char_start: number;
  char_end: number;
  filename: string;
};

export type ToolCall = {
  id: string;
  run_id: string;
  conversation_id: string;
  tool_id: string;
  tool_name: string;
  status: string;
  request_url: string;
  response_status: number | null;
  response_body: string;
  error: string;
  created_at: string;
};

/** A function call issued by the agent loop, as opposed to the legacy HTTP tool. */
export type AgentToolCall = {
  id: string;
  run_id: string;
  conversation_id: string;
  name: string;
  arguments_json: string;
  /** What the call will do if approved: a unified diff, or a one-line summary. */
  proposal_preview: string;
  status: string;
  result_preview: string;
  error: string;
  latency_ms: number;
  /** Files the call left behind — the chart a `run_python` call drew. */
  artifacts: ToolArtifact[];
  /**
   * The approval mode that let this call run with nobody looking — the string
   * `"auto_writes"` — or "" when a person, a standing policy or the tool's own
   * read-only default decided it.
   *
   * The mode and never the decider's id: this is what makes a bypassed thread
   * readable in the conversation, so a user can see which writes went through
   * unreviewed without opening an audit table.
   */
  approved_by_mode: string;
  created_at: string;
};

export type ToolPolicy = {
  tool_name: string;
  policy: "ask" | "allow" | "deny";
};

// --- Agents (authored system prompts + provisioned tools) ---

/**
 * An agent a person authored: a name, a system prompt, and — optionally — a
 * provisioned subset of the tool registry. `allowed_tools: null` means the
 * whole registry; a list (empty included) is an explicit grant. The subset
 * narrows what the model is offered; workspace tool policies still gate what
 * any surviving tool may do.
 */
export type AgentInfo = {
  id: string;
  name: string;
  description: string;
  instructions: string;
  enabled: boolean;
  allowed_tools: string[] | null;
  created_at: string;
  updated_at: string;
};

export type AgentCreateBody = {
  name: string;
  instructions: string;
  description?: string;
  allowed_tools?: string[] | null;
};

/**
 * Every field optional; omitted means "leave alone". `clear_allowed_tools`
 * exists because `allowed_tools: null` cannot distinguish "reset to all tools"
 * from "not editing tools" in a PATCH.
 */
export type AgentUpdateBody = {
  name?: string;
  description?: string;
  instructions?: string;
  enabled?: boolean;
  allowed_tools?: string[] | null;
  clear_allowed_tools?: boolean;
};

/**
 * One parameter a skill declares. The workflow InputSpec shape, minus the
 * workflow-only bits — a name, a type the value is coerced to, and the usual
 * label/required/default/choices affordances an editor needs to render a field.
 */
export type SkillArg = {
  name: string;
  type: "string" | "number" | "integer" | "boolean";
  label: string;
  description: string;
  required: boolean;
  default: unknown;
  choices: unknown[];
};

/**
 * A reusable, named instruction block a user authors once and invokes per turn.
 *
 * `can_share`/`can_edit` are the caller's rights on this row, resolved by the
 * server rather than guessed by the client: a member sees a shared skill but may
 * not edit it or flip its `shared` flag, so the editor disables those controls
 * instead of surprising them with a 403.
 */
export type Skill = {
  id: string;
  name: string;
  title: string;
  description: string;
  body: string;
  args: SkillArg[];
  shared: boolean;
  version: number;
  can_share: boolean;
  can_edit: boolean;
  created_at: string;
  updated_at: string;
};

/** One retained snapshot of a skill, for the versions list and its restore. */
export type SkillVersion = {
  id: string;
  version: number;
  title: string;
  created_at: string;
};

export type SkillCreateBody = {
  name: string;
  title: string;
  description?: string;
  body: string;
  args?: SkillArg[];
  /** Ignored server-side for a non-owner; the editor hides it unless permitted. */
  shared?: boolean;
};

/** Every field optional; omitted means "leave alone". A member sending
 * `shared` is refused — sharing is gated to an owner/admin. */
export type SkillUpdateBody = {
  title?: string;
  description?: string;
  body?: string;
  args?: SkillArg[];
  shared?: boolean;
};

/** One registry tool, as the provisioning checklist renders it. */
export type ToolInfo = {
  name: string;
  description: string;
  read_only: boolean;
  /** The registry family it ships with ("core", "mcp", "sandbox", …). */
  family: string;
};

/**
 * "markdown" is rendered, maths and all; "text" is shown exactly as typed.
 *
 * There is no "latex" document. There used to be, and it rendered identically
 * to markdown and compiled nothing — LaTeX that produces a PDF is a *project*
 * kind (`ProjectKind`), which is a different feature with a different editor.
 */
export type DocumentKind = "text" | "markdown";

export type DocumentSummary = {
  id: string;
  title: string;
  kind: DocumentKind;
  characters: number;
  /** The folder this file is in; "" is the top level. */
  folder_id: string;
  updated_at: string;
};

export type WorkspaceDocument = {
  id: string;
  title: string;
  kind: DocumentKind;
  content: string;
  folder_id: string;
  updated_at: string;
};

/**
 * A place to put files. The server returns the whole tree in one list — it is
 * capped at a couple of hundred rows — and the parent link is what makes it a
 * tree; "" is the top level, and is deliberately not `null`, so "no parent" has
 * one spelling in the wire format, the database and the client.
 */
export type Folder = {
  id: string;
  name: string;
  parent_id: string;
  updated_at: string;
};

export type DocumentVersion = {
  id: string;
  summary: string;
  created_at: string;
};

export type BoardCard = {
  id: string;
  title: string;
  body: string;
  labels: string[];
  /**
   * Ticked off. Every card carries it, not only the ones a one-column board
   * draws as checkboxes — which is what lets a checked item graduate into a
   * kanban card by growing a second column, same id, tick intact.
   */
  done: boolean;
};

export type BoardColumn = {
  id: string;
  name: string;
  cards: BoardCard[];
};

export type Board = {
  id: string;
  name: string;
  columns: BoardColumn[];
};

/**
 * A todo list, as the API returns one.
 *
 * A list is a board with exactly one column — derived, not stored — so nearly
 * everything you can do to one is done through the board routes above. Only
 * two operations have no board equivalent, and they are the only two this
 * client carries: making a list (`createTodoList`, which knows the shape a list
 * has to be born in) and ticking an item off without naming its board
 * (`setTodoItemDone`).
 */
export type TodoList = {
  id: string;
  name: string;
  items: TodoItem[];
};

export type TodoItem = {
  id: string;
  /** Carried on the item because the route that ticks one does not take it. */
  list_id: string;
  title: string;
  body: string;
  done: boolean;
  /** When it was ticked, or null while it is open. */
  done_at: string | null;
};

export type DbEngine = "postgres" | "mysql" | "sqlite" | "duckdb";

export type DbConnection = {
  id: string;
  name: string;
  engine: string;
  read_only: boolean;
  status: string;
  last_error: string;
  /** The DSN with its password replaced — the real one is never returned. */
  dsn_summary: string;
  created_at: string;
};

export type DbConnectionInput = {
  name: string;
  engine: DbEngine;
  dsn: string;
  read_only: boolean;
};

export type DbColumn = {
  name: string;
  type: string;
  nullable: boolean;
  primary_key: boolean;
};

export type DbForeignKey = {
  columns: string[];
  references: string;
  referred_columns: string[];
};

export type DbTable = {
  name: string;
  schema_name: string;
  /** False on large databases, where columns arrive only when asked for by name. */
  columns_loaded: boolean;
  columns: DbColumn[];
  columns_omitted: number;
  foreign_keys: DbForeignKey[];
};

export type DbSchema = {
  connection_id: string;
  connection: string;
  engine: string;
  table_count: number;
  tables: DbTable[];
  tables_omitted: number;
  note: string;
};

export type ProjectFile = { path: string; content: string; bytes: number };

export type ProjectKind = "web" | "latex";

export type ProjectSummary = {
  id: string;
  name: string;
  description: string;
  kind: ProjectKind;
  entry_path: string;
  file_count: number;
  total_bytes: number;
  updated_at: string;
};

export type WorkspaceProject = {
  id: string;
  name: string;
  description: string;
  kind: ProjectKind;
  entry_path: string;
  files: ProjectFile[];
  total_bytes: number;
  updated_at: string;
};

/**
 * One stretch of a document, as it is now and as a proposal wants it.
 *
 * `index` is -1 for a stretch the edit does not touch, and otherwise the hunk
 * number `decideAgentToolCall` accepts by. The segments cover the whole
 * document in order, which is what lets a client render the text and the
 * proposal as one thing rather than as a document and a diff card beside it.
 */
export type ReviewSegment = {
  index: number;
  before: string[];
  after: string[];
};

/** A document write the agent proposed and the user has not decided yet. */
export type PendingDocumentEdit = {
  id: string;
  run_id: string;
  name: string;
  document_id: string;
  title: string;
  proposal_preview: string;
  /**
   * Empty when there is nothing to review hunk by hunk — a create, a target
   * that no longer resolves, a `find` that no longer matches, or a document too
   * long to ship line by line. Empty is the signal to fall back to the
   * all-or-nothing card rather than to render a document with no decisions.
   */
  segments: ReviewSegment[];
  created_at: string;
};

export type McpTool = {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
};

export type McpServer = {
  id: string;
  name: string;
  transport: "stdio" | "http";
  command: string;
  args: string[];
  url: string;
  enabled: boolean;
  status: string;
  last_error: string;
  has_secrets: boolean;
  tools: McpTool[];
  created_at: string;
};

/**
 * Whether *this* user has authorised a remote MCP server.
 *
 * Per user rather than per workspace, deliberately: an MCP server authorises a
 * person, so two people sharing a workspace each connect their own account and
 * neither can see the other's. Carries no token, and never will.
 */
export type McpAuthStatus = {
  server_id: string;
  /** The server sits behind OAuth, so tools do nothing until it is connected. */
  required: boolean;
  connected: boolean;
  issuer: string;
  scopes: string;
  expires_in_seconds: number | null;
};

export type McpServerInput = {
  name: string;
  transport: "stdio" | "http";
  command?: string;
  args?: string[];
  url?: string;
  /** stdio env vars or HTTP headers; write-only, never returned by the API. */
  secrets?: Record<string, string>;
};

/**
 * A workspace-defined tool the agent can call, executed in the session sandbox
 * under this tool's own egress allowlist and approval policy.
 *
 * `name` is the slug the model calls; `argv` is the command template whose
 * `{{param}}` placeholders are filled from a validated call and run as argv
 * (never a shell line). `egress_hosts` is the ONLY hosts an execution may
 * reach — it tightens the workspace sandbox default and can never widen the
 * sandbox's metadata/RFC1918/loopback denials. `approval="always"` forces a
 * human approval even where the workspace policy would allow; it may only
 * tighten, never loosen a deny.
 */
export type SandboxTool = {
  id: string;
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
  argv: string[];
  egress_hosts: string[];
  approval: "inherit" | "always";
  enabled: boolean;
  created_at: string;
};

export type SandboxToolInput = {
  name: string;
  description?: string;
  input_schema?: Record<string, unknown>;
  argv?: string[];
  egress_hosts?: string[];
  approval?: "inherit" | "always";
  enabled?: boolean;
};

export type AuditEvent = {
  id: string;
  action: string;
  resource_type: string;
  resource_id: string;
  detail: Record<string, unknown>;
  created_at: string;
};

export type GraphEntity = {
  id: string;
  name: string;
  entity_type: string;
  mention_count: number;
  source_ids: string[];
  chunk_ids: string[];
  memory_ids: string[];
};

export type GraphEdge = {
  id: string;
  from_entity_id: string;
  to_entity_id: string;
  relation: string;
  /** Trust in `relation`: co-occurrence carries a low floor, a named relation its own score. */
  confidence: number;
  weight: number;
  source_ids: string[];
  chunk_ids: string[];
  memory_ids: string[];
};

export type MemoryItem = {
  id: string;
  conversation_id: string | null;
  kind: string;
  content: string;
  entity_names: string[];
  message_ids: string[];
  importance: number;
  created_at: string;
  updated_at: string;
};

export type KnowledgeGraph = {
  status: "empty" | "queued" | "building" | "ready" | "stale" | "failed";
  version: string;
  built_at: string | null;
  entities: GraphEntity[];
  edges: GraphEdge[];
};

export type DatasetColumn = {
  name: string;
  type: "boolean" | "integer" | "number" | "string" | "date" | "datetime";
  nullable: boolean;
};

export type Dataset = {
  id: string;
  name: string;
  description: string;
  current_version: number;
  version_id: string;
  source_id: string;
  format: string;
  columns: DatasetColumn[];
  row_count: number;
  content_hash: string;
  created_at: string;
  updated_at: string;
};

export type DatasetFilter = {
  field: string;
  operator: "eq" | "ne" | "gt" | "gte" | "lt" | "lte" | "contains";
  value: unknown;
};

export type DatasetMetric = {
  field?: string | null;
  operation: "count" | "sum" | "avg" | "min" | "max";
  label: string;
};

export type DatasetQuery = {
  filters?: DatasetFilter[];
  group_by?: string | null;
  metrics?: DatasetMetric[];
  order_by?: string | null;
  order_direction?: "asc" | "desc";
  limit?: number;
};

export type DatasetQueryResult = {
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
  truncated: boolean;
  elapsed_ms: number;
};

export type DashboardSpec = {
  visualization: "table" | "bar" | "line" | "donut";
  query: DatasetQuery;
  x_field?: string | null;
  y_fields: string[];
};

export type Dashboard = {
  id: string;
  name: string;
  description: string;
  dataset_id: string;
  spec: DashboardSpec;
  /** The template this was bound from, or "" for one the agent wrote directly. */
  template_id?: string;
  /** Declared column name → the dataset column it was bound to. Empty if none. */
  bindings?: Record<string, string>;
  created_at: string;
  updated_at: string;
};

export type DashboardRun = {
  dashboard: Dashboard;
  result: DatasetQueryResult;
};

/** One column a template requires of whatever dataset it is bound to. */
export type DashboardColumnRequirement = {
  name: string;
  type: DatasetColumn["type"];
  description?: string;
};

/**
 * A dashboard definition with no data yet: what it needs, and what it draws.
 *
 * The requirements are the whole point. A template that declares `region:
 * string, amount: number` can be bound to any dataset that has those shapes
 * under some names, and refused — legibly, naming the column — by any dataset
 * that does not.
 */
export type DashboardTemplate = {
  id: string;
  name: string;
  description: string;
  required_columns: DashboardColumnRequirement[];
  spec: DashboardSpec;
  created_at: string;
  updated_at: string;
};

/**
 * A dashboard on *this* user's home screen, and where it sits.
 *
 * A workspace shares its dashboards and does not share your home screen: the
 * pin, and every coordinate on it, belongs to one person.
 */
export type DashboardPin = {
  dashboard: Dashboard;
  grid_x: number;
  grid_y: number;
  grid_w: number;
  grid_h: number;
  pinned_at: string;
};

/** One tile's placement on the 12-column grid. */
export type DashboardLayoutTile = {
  dashboard_id: string;
  grid_x: number;
  grid_y: number;
  grid_w: number;
  grid_h: number;
};

export type AppRelease = {
  id: string;
  version: number;
  status: "draft" | "published" | "superseded";
  content_hash: string;
  manifest: AppManifest;
  created_at: string;
  published_at: string | null;
};

export type AppDashboardSnapshot = {
  id: string;
  name: string;
  description: string;
  visualization: DashboardSpec["visualization"];
  x_field: string | null;
  y_fields: string[];
  result: DatasetQueryResult;
};

export type AppDataBinding = {
  dataset_id: string;
  name: string;
};

export type AppManifest = {
  schema_version: number;
  generated_at: string;
  dashboards?: AppDashboardSnapshot[];
  kind?: "code";
  prompt?: string;
  html?: string;
  data_bindings?: AppDataBinding[];
  snapshots?: Record<string, DatasetQueryResult>;
};

export type GeneratedApp = {
  id: string;
  name: string;
  slug: string;
  description: string;
  visibility: "private" | "public";
  app_type: "dashboard" | "code";
  current_release_id: string | null;
  releases: AppRelease[];
  created_at: string;
  updated_at: string;
};

export type PublishedApp = {
  name: string;
  slug: string;
  description: string;
  version: number;
  published_at: string;
  manifest: AppManifest;
};

export type IntegrationAccount = {
  id: string;
  provider: string;
  external_account: string;
  scopes: string;
  status: string;
  last_sync_at: string | null;
  created_at: string;
};

export type IntegrationProvider = {
  provider: string;
  configured: boolean;
  account: IntegrationAccount | null;
};

export type SyncJob = {
  id: string;
  connector: string;
  status: string;
  stats: Record<string, number>;
  error: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
};

export type RunEvent = {
  id: number;
  event: string;
  data: Record<string, unknown>;
};

/**
 * Egress policy for a sandbox, frozen onto the session when it was created.
 * "open" reaches the internet, "allowlist" reaches only `allow_hosts`, "none"
 * reaches nothing. The UI must show this before anyone pastes data in.
 */
export type SandboxNetworkPolicy = "open" | "allowlist" | "none";

export type SandboxSessionStatus = "running" | "paused" | "killed" | "error";

/**
 * One microVM, addressed only by this id. The provider-side id is deliberately
 * absent from the payload — the server resolves it from the row.
 */
export type SandboxSession = {
  id: string;
  project_id: string;
  label: string;
  /** "e2b", or "fake" in development. */
  provider: string;
  template: string;
  status: SandboxSessionStatus;
  network_policy: SandboxNetworkPolicy;
  allow_hosts: string[];
  error: string;
  exec_count: number;
  wall_ms_used: number;
  created_at: string;
  last_used_at: string;
};

export type SandboxExecutionKind = "code" | "command";

export type SandboxExecution = {
  id: string;
  kind: SandboxExecutionKind;
  source: string;
  exit_code: number | null;
  stdout: string;
  stderr: string;
  /** Non-empty when the *user's* code failed. Infrastructure failure is a 5xx. */
  error: string;
  /** How many the provider produced, including any the storage caps refused. */
  artifact_count: number;
  /** The ones that were stored, and so can be shown. Survives a reload. */
  artifacts: ToolArtifact[];
  duration_ms: number;
  created_at: string;
};

/**
 * A file a tool call or an execution produced — a matplotlib figure, a PDF —
 * saved as a workspace `Source`.
 *
 * This used to describe an inline-base64 contract the server stopped honouring:
 * it declared `data` and `url` as optional and the server sent neither, so
 * every consumer's "do I have bytes?" check answered no and every chart the
 * sandbox drew was silently dropped on the floor. It now mirrors
 * `ToolArtifact` in apps/api/app/schemas.py exactly, and `url` is required
 * because an artifact nobody can address is an artifact nobody can see.
 */
export type ToolArtifact = {
  /** The `Source` row holding the bytes. */
  id: string;
  /** "png", "jpeg", "svg", "chart", … — what the sandbox called it. */
  kind: string;
  mime: string;
  bytes: number;
  /**
   * Path under the API root that streams the bytes. Relative: pass it to
   * `api.sourceContent`, which resolves it against the base URL and carries the
   * session. It is deliberately not usable as an `<img src>` — see that method.
   */
  url: string;
  /** Read off the file header, when the format carries one. */
  width?: number | null;
  height?: number | null;
  /** A structured chart description, when the provider supplied one. */
  chart?: unknown;
};

export type SandboxRun = {
  execution: SandboxExecution;
  artifacts: ToolArtifact[];
  /** True when output was clipped, so the panel can say so. */
  truncated: boolean;
  /** The session as it stands after the run — status and counters included. */
  session: SandboxSession;
};

/* --- Workspace administration ---------------------------------------------
 *
 * Every shape below mirrors a response model in apps/api/app/api/admin.py, and
 * every route behind them is owner-only (403 otherwise) and scoped to the
 * caller's workspace. None of them carries a credential: an MCP server reports
 * `has_secrets`, never a secret, and a sandbox never reports the provider-side
 * id that names its live machine.
 */

export type AdminMember = {
  user_id: string;
  membership_id: string;
  name: string;
  email: string;
  role: string;
  status: string;
  joined_at: string;
  /** True for the caller's own row — an owner cannot act on themselves. */
  is_self: boolean;
};

export type AdminAuditEntry = {
  id: string;
  action: string;
  resource_type: string;
  resource_id: string;
  detail: Record<string, unknown>;
  created_at: string;
  actor_id: string;
  /** Empty when a background worker wrote the row, or the user is gone. */
  actor_name: string;
  actor_email: string;
};

export type AdminAuditPage = {
  entries: AdminAuditEntry[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
};

export type AdminRun = {
  id: string;
  conversation_id: string;
  agent_id: string;
  created_by: string;
  status: string;
  prompt_preview: string;
  error: string;
  created_at: string;
  updated_at: string;
};

export type AdminApproval = {
  id: string;
  run_id: string;
  name: string;
  status: string;
  proposal_preview: string;
  created_at: string;
};

export type AdminActivity = {
  run_status_counts: Record<string, number>;
  tool_call_status_counts: Record<string, number>;
  recent_runs: AdminRun[];
  /** Only the calls actually parked on a human decision. */
  pending_approvals: AdminApproval[];
};

export type AdminSandboxSession = {
  id: string;
  project_id: string;
  label: string;
  provider: string;
  status: string;
  network_policy: SandboxNetworkPolicy;
  exec_count: number;
  wall_ms_used: number;
  error: string;
  created_at: string;
  last_used_at: string;
  killed_at: string | null;
};

export type AdminMcpServer = {
  id: string;
  name: string;
  transport: string;
  enabled: boolean;
  status: string;
  last_error: string;
  last_connected_at: string | null;
  /** Whether env/headers are stored — never the values. */
  has_secrets: boolean;
  tool_count: number;
  created_at: string;
};

export type AdminStorage = {
  sources_by_status: Record<string, number>;
  source_count: number;
  source_bytes: number;
  chunk_count: number;
  memory_by_status: Record<string, number>;
  memory_item_count: number;
  graph_entity_count: number;
  graph_edge_count: number;
};

/**
 * Model spend, and the two fields that say how far to trust it.
 *
 * `cost_usd` is summed over *priced* rows only. A call whose model has no rate
 * in the deployment's MODEL_PRICES contributes its tokens and no cost, and is
 * counted in `unpriced_calls` instead of being folded in as zero — so a reader
 * that shows `cost_usd` without reading `unpriced_calls` next to it is showing a
 * floor while implying a total. `pricing_configured` is false when MODEL_PRICES
 * is empty, which is the shipped default: there, every cost here is zero by
 * arithmetic and none of it may be rendered as money.
 */
export type AdminUsageTotals = {
  calls: number;
  input_tokens: number;
  /** A subset of input_tokens, billed at its own rate — not an extra amount. */
  cached_input_tokens: number;
  output_tokens: number;
  /** Likewise a subset of output_tokens. */
  reasoning_tokens: number;
  total_tokens: number;
  cost_usd: number;
  priced_calls: number;
  unpriced_calls: number;
};

/** One row of a breakdown: a model, a user, or an operation. */
export type AdminUsageGroup = {
  key: string;
  /** The key itself for models and operations; a name or email for a user. */
  label: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  cost_usd: number;
  unpriced_calls: number;
};

/** One run's spend — the runaway-loop question, asked in money. */
export type AdminUsageRun = {
  run_id: string;
  conversation_id: string;
  calls: number;
  total_tokens: number;
  cost_usd: number;
  unpriced_calls: number;
  last_call_at: string;
};

export type AdminUsage = {
  window_days: number;
  since: string;
  totals: AdminUsageTotals;
  by_model: AdminUsageGroup[];
  by_user: AdminUsageGroup[];
  by_operation: AdminUsageGroup[];
  /** Ordered by cost, then tokens, so an unpriced run still surfaces. */
  top_runs: AdminUsageRun[];
  /** Models seen in this window with no configured rate. */
  unpriced_models: string[];
  /** False when MODEL_PRICES is empty: no cost figure can be anything but zero. */
  pricing_configured: boolean;
};

/**
 * One metric's latency distribution, in milliseconds. Percentiles are computed
 * server-side over the runs in the window (capped), and every one is null when
 * no run contributed a sample — null means "nothing measured", never zero,
 * because a zero here would read as an instantaneous response.
 */
export type AdminLatencyStats = {
  samples: number;
  p50_ms: number | null;
  p90_ms: number | null;
  p99_ms: number | null;
  max_ms: number | null;
};

/** Run count in one equal-width time bucket of the window. */
export type AdminThroughputBucket = {
  start: string;
  count: number;
};

/** A run that has not reached a terminal state — queued, running, cancelling. */
export type AdminLiveRun = {
  run_id: string;
  conversation_id: string;
  agent_id: string;
  status: string;
  created_at: string;
  age_seconds: number;
};

/** A recently failed run and the reason it gives, clipped server-side. */
export type AdminFailedRun = {
  run_id: string;
  conversation_id: string;
  error: string;
  created_at: string;
  updated_at: string;
};

/**
 * Active distinct members over the three standard windows. Counts only — the
 * endpoint never returns user ids, which keeps it a workspace aggregate rather
 * than a roster.
 */
export type AdminRetention = {
  dau: number;
  wau: number;
  mau: number;
};

/**
 * Admin observability: how fast runs answer, how many there are, how often they
 * fail, what is live right now, and whether people keep coming back — all
 * derived from timing the app already records, scoped to the workspace.
 */
export type AdminObservability = {
  window_hours: number;
  since: string;
  /** Time-to-first-token, over completed runs that produced a delta. */
  ttft: AdminLatencyStats;
  /** Total wall latency, over terminal runs. */
  total: AdminLatencyStats;
  /** True count in the window, before the percentile-sampling cap. */
  runs_in_window: number;
  throughput: AdminThroughputBucket[];
  completed: number;
  failed: number;
  cancelled: number;
  /** failed / (completed + failed + cancelled); 0 when the window is empty. */
  error_rate: number;
  /**
   * How many turns the prompt-injection screen flagged in the window — the
   * count of `screen.flagged` run events. Optional: absent on a deployment
   * whose backend predates the screen, which reads the same as zero.
   */
  screen_flags?: number;
  recent_failures: AdminFailedRun[];
  live_runs: AdminLiveRun[];
  retention: AdminRetention;
};

/**
 * The spend ceiling (ADR 0008): what a workspace *may* spend, next to what it
 * already has.
 *
 * Every limit is nullable and null means **no limit of that kind**, never zero
 * — a zero ceiling stops the next call, and rendering an absent limit as `$0`
 * would tell an owner their workspace is capped when nothing is capping it.
 */
export type AdminBudgetCeiling = {
  window_hours: number;
  usd_per_window: number | null;
  tokens_per_window: number | null;
  /** `workspace` — an owner set it here; `settings` — the deployment did. */
  source: string;
};

/** What one window has already cost. Same shape `readSpend` reads elsewhere. */
export type AdminBudgetSpend = {
  calls: number;
  cost_usd: number;
  total_tokens: number;
  /**
   * Calls whose model had no rate. Above zero, `cost_usd` is a floor and a USD
   * ceiling standing alone cannot bound this workspace at all.
   */
  unpriced_calls: number;
};

export type AdminBudgetParkedRun = {
  run_id: string;
  conversation_id: string;
  created_at: string;
};

export type AdminBudget = {
  ceiling: AdminBudgetCeiling;
  /** The same ceiling scaled by UNATTENDED_BUDGET_FRACTION, for workflows. */
  unattended_ceiling: AdminBudgetCeiling;
  spend: AdminBudgetSpend;
  unattended_spend: AdminBudgetSpend;
  /** False when neither limit is set: this workspace has no ceiling at all. */
  enforced: boolean;
  pricing_configured: boolean;
  runs_parked_on_budget: AdminBudgetParkedRun[];
  /** Populated by the PUT only: runs the new ceiling released. Empty on GET. */
  resumed_run_ids: string[];
};

/**
 * Replace, not patch. An omitted limit is *no limit of that kind*, so the body
 * always states the whole ceiling and there is no way to raise one number while
 * silently keeping a second one you have forgotten about.
 */
export type AdminBudgetInput = {
  window_hours: number;
  usd_per_window: number | null;
  tokens_per_window: number | null;
};

function makeKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export class ApiError extends Error {
  constructor(
    message: string,
    /** HTTP status, or 0 when the request never reached the API. */
    public readonly status: number,
  ) {
    super(message);
  }

  /** True when the API could not be reached at all (offline, wrong port, CORS). */
  get offline(): boolean {
    return this.status === 0;
  }

  /** True when the API says nobody is signed in. */
  get unauthenticated(): boolean {
    return this.status === 401;
  }

  /** True when the caller is signed in but lacks the role for this route. */
  get forbidden(): boolean {
    return this.status === 403;
  }
}

/** Must match Settings.csrf_header_name. */
const CSRF_HEADER = "X-CSRF-Token";
/** Optional workspace *selection*; membership is still verified server-side. */
const WORKSPACE_HEADER = "X-Workspace-Id";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS", "TRACE"]);

/**
 * The auth routes report their own 401 to whoever called them: a wrong password
 * is an answer, not an expired session, and routing it through the global
 * "you have been signed out" hook would fight the login form for the screen.
 */
function ownsItsOwn401(path: string): boolean {
  return path.startsWith("/api/auth/");
}

export class WorkspaceApi {
  /**
   * Session state lives here and nowhere else: views call methods, and the
   * cookie, the CSRF header and the workspace selection are this class's job.
   */
  private csrfToken = "";
  private workspaceId = "";
  private unauthorizedHandlers = new Set<() => void>();

  constructor(
    public readonly baseUrl = "http://localhost:8000",
    private readonly headers: Record<string, string> = {},
  ) {}

  /**
   * Called whenever the API answers 401 to anything outside /api/auth. Returns
   * an unsubscribe so a React effect can clean up after itself.
   */
  onUnauthorized(handler: () => void): () => void {
    this.unauthorizedHandlers.add(handler);
    return () => {
      this.unauthorizedHandlers.delete(handler);
    };
  }

  /** Which workspace subsequent requests are about. "" means "the first one". */
  setWorkspaceId(workspaceId: string): void {
    this.workspaceId = workspaceId;
  }

  get csrf(): string {
    return this.csrfToken;
  }

  private buildHeaders(init: RequestInit, mutation: boolean): Headers {
    const headers = new Headers(this.headers);
    if (init.body && !(init.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
    }
    if (mutation && !headers.has("Idempotency-Key")) {
      headers.set("Idempotency-Key", makeKey());
    }
    if (this.workspaceId) headers.set(WORKSPACE_HEADER, this.workspaceId);
    return headers;
  }

  private async dispatch(
    path: string,
    init: RequestInit,
    headers: Headers,
    unsafe: boolean,
  ): Promise<Response> {
    // Copied per attempt so a retry picks up a freshly fetched CSRF token while
    // reusing the same Idempotency-Key — the retry is the same operation.
    const attemptHeaders = new Headers(headers);
    if (unsafe && this.csrfToken) attemptHeaders.set(CSRF_HEADER, this.csrfToken);
    try {
      return await fetch(`${this.baseUrl}${path}`, {
        ...init,
        headers: attemptHeaders,
        credentials: "include",
      });
    } catch {
      // fetch rejects with an opaque "Failed to fetch" TypeError; the caller
      // only needs to know the API was unreachable.
      throw new ApiError(`Cannot reach the API at ${this.baseUrl}`, 0);
    }
  }

  private static async detailOf(response: Response): Promise<string> {
    try {
      const body = (await response.clone().json()) as { detail?: unknown };
      // Validation errors put a list here; only a string is meant for a human.
      return typeof body.detail === "string" ? body.detail : "";
    } catch {
      return "";
    }
  }

  private signalUnauthorized(): void {
    for (const handler of [...this.unauthorizedHandlers]) handler();
  }

  private async request<T>(
    path: string,
    init: RequestInit = {},
    mutation = false,
  ): Promise<T> {
    const headers = this.buildHeaders(init, mutation);
    const unsafe = !SAFE_METHODS.has((init.method || "GET").toUpperCase());

    let response = await this.dispatch(path, init, headers, unsafe);
    let detail = response.ok ? "" : await WorkspaceApi.detailOf(response);

    if (unsafe && response.status === 403 && /csrf/i.test(detail)) {
      // The token is per-session and rotates on login, so a stale one means the
      // page is holding a value from before a rotation. Re-read it and retry
      // exactly once; a second failure is a real refusal.
      try {
        await this.me();
      } catch {
        // Leave the retry to produce the authoritative error.
      }
      response = await this.dispatch(path, init, headers, unsafe);
      detail = response.ok ? "" : await WorkspaceApi.detailOf(response);
    }

    if (response.status === 401 && !ownsItsOwn401(path)) this.signalUnauthorized();

    if (!response.ok) {
      throw new ApiError(detail || `Request failed (${response.status})`, response.status);
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
  }

  // --- Authentication -------------------------------------------------------
  // Every method that returns a session also adopts its CSRF token, so no view
  // ever handles one.

  private adopt(session: AuthSession): AuthSession {
    this.csrfToken = session.csrf_token;
    // Name the workspace the session resolved to rather than leaning on "the
    // first one"; the API still checks the header against memberships.
    this.workspaceId = session.workspace_id;
    return session;
  }

  /** The current session, or a 401 ApiError when signed out. */
  async me(): Promise<AuthSession> {
    return this.adopt(await this.request<AuthSession>("/api/auth/me"));
  }

  async login(email: string, password: string): Promise<AuthSession> {
    return this.adopt(
      await this.request<AuthSession>("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      }),
    );
  }

  /** Local one-click override when the API has DEV_USER configured. */
  async devLogin(): Promise<AuthSession> {
    return this.adopt(
      await this.request<AuthSession>("/api/auth/dev-login", {
        method: "POST",
      }),
    );
  }

  /** Every workspace the signed-in user may select, oldest membership first. */
  listWorkspaces(): Promise<WorkspaceMembership[]> {
    return this.request<WorkspaceMembership[]>("/api/auth/workspaces");
  }

  async devOverride(): Promise<DevOverride> {
    try {
      return await this.request<DevOverride>("/api/auth/dev-override");
    } catch {
      return { enabled: false, handle: "" };
    }
  }

  /**
   * True when the API has anonymous playground mode turned on. Public and
   * unauthenticated like `devOverride`, so the sign-in screen can decide
   * whether to show a "Try it" button before any session exists. A swallowed
   * error is the disabled answer — a missing or 404 endpoint is not a button.
   */
  async playgroundAvailable(): Promise<boolean> {
    try {
      return (
        await this.request<{ enabled: boolean }>("/api/auth/playground")
      ).enabled;
    } catch {
      return false;
    }
  }

  /**
   * Mints an ephemeral anonymous session over a throwaway, fully-isolated
   * playground workspace. No credentials — the visitor is trying the app.
   */
  async playgroundLogin(): Promise<AuthSession> {
    return this.adopt(
      await this.request<AuthSession>("/api/auth/playground", {
        method: "POST",
      }),
    );
  }

  /** Does not sign the caller in — the account still has to confirm by email. */
  signup(
    email: string,
    password: string,
    name = "",
  ): Promise<AuthAcknowledgement> {
    return this.request("/api/auth/signup", {
      method: "POST",
      body: JSON.stringify({ email, password, name }),
    });
  }

  async logout(): Promise<void> {
    try {
      await this.request<void>("/api/auth/logout", { method: "POST" });
    } finally {
      // Local state goes either way: a logout the server refused still means
      // this tab should stop acting signed in.
      this.csrfToken = "";
      this.workspaceId = "";
    }
  }

  requestPasswordReset(email: string): Promise<AuthAcknowledgement> {
    return this.request("/api/auth/password/reset/request", {
      method: "POST",
      body: JSON.stringify({ email }),
    });
  }

  /**
   * Asks the API to email a one-time sign-in link. Returns the same
   * deliberately uninformative acknowledgement as signup and reset — identical
   * whether or not the address has an account — so callers must render its
   * `detail` verbatim and never read an outcome out of it.
   */
  requestLoginLink(email: string): Promise<AuthAcknowledgement> {
    return this.request("/api/auth/login-link/request", {
      method: "POST",
      body: JSON.stringify({ email }),
    });
  }

  /**
   * Redeems a magic-link token for a real session, exactly like `login`. The
   * link is single-use and short-lived; a spent or expired token is a 400.
   */
  async consumeLoginLink(token: string): Promise<AuthSession> {
    return this.adopt(
      await this.request<AuthSession>("/api/auth/login-link/consume", {
        method: "POST",
        body: JSON.stringify({ token }),
      }),
    );
  }

  confirmPasswordReset(
    token: string,
    password: string,
  ): Promise<AuthAcknowledgement> {
    return this.request("/api/auth/password/reset/confirm", {
      method: "POST",
      body: JSON.stringify({ token, password }),
    });
  }

  verifyEmail(token: string): Promise<AuthAcknowledgement> {
    return this.request("/api/auth/verify-email", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
  }

  /**
   * Must be assigned to `window.location.href`: the endpoint sets the OAuth
   * state cookie and Google refuses to render inside an iframe or an XHR.
   */
  googleLoginUrl(): string {
    return `${this.baseUrl}/api/auth/google/start`;
  }

  /**
   * True when the API has a Google login client configured. `redirect: "manual"`
   * turns the configured case into an unreadable opaque redirect (which is the
   * answer) and leaves the unconfigured 503 readable.
   */
  async googleLoginAvailable(): Promise<boolean> {
    try {
      const response = await fetch(this.googleLoginUrl(), {
        redirect: "manual",
        credentials: "include",
      });
      return response.status !== 503;
    } catch {
      return false;
    }
  }

  health(): Promise<{ status: string }> {
    return this.request("/health");
  }

  bootstrap(): Promise<Bootstrap> {
    return this.request("/api/bootstrap");
  }

  listConversations(): Promise<Conversation[]> {
    return this.request("/api/conversations");
  }

  createConversation(title = "New conversation"): Promise<Conversation> {
    return this.request(
      "/api/conversations",
      { method: "POST", body: JSON.stringify({ title }) },
      true,
    );
  }

  /**
   * The thread for the chat panel beside a document, created on first open.
   *
   * A POST that carries no `Idempotency-Key` on purpose — the document id *is*
   * the key. There is one thread per document, so a retry, a second tab and a
   * React remount all land on the same conversation, and nothing here needs the
   * caller to remember a nonce. Passing `false` for `mutation` is what keeps a
   * fresh key from being minted per call for an operation that cannot double.
   */
  documentConversation(documentId: string): Promise<Conversation> {
    return this.request(
      `/api/documents/${documentId}/conversation`,
      { method: "POST" },
      false,
    );
  }

  /**
   * Change how much this thread asks before acting.
   *
   * A PUT of a value rather than the creation of one, so a retry lands on the
   * same state by construction and no `Idempotency-Key` rides along — the
   * server audits every call, including the no-op, which is the correct record
   * of a request that really was made twice.
   */
  setApprovalMode(conversationId: string, mode: ApprovalMode): Promise<Conversation> {
    return this.request(`/api/conversations/${conversationId}/approval-mode`, {
      method: "PUT",
      body: JSON.stringify({ mode }),
    });
  }

  deleteConversation(conversationId: string): Promise<void> {
    return this.request(
      `/api/conversations/${conversationId}`,
      { method: "DELETE" },
      true,
    );
  }

  listMessages(conversationId: string): Promise<Message[]> {
    return this.request(`/api/conversations/${conversationId}/messages`);
  }

  sendMessage(
    conversationId: string,
    content: string,
    agentId?: string,
    controls?: MessageControls,
  ): Promise<SendMessageResponse> {
    return this.request(
      `/api/conversations/${conversationId}/messages`,
      {
        method: "POST",
        // A workspace with no agent bootstraps as "", and an empty agent_id is
        // not a valid selection — omitting the field lets the API pick the
        // workspace's own agent instead of failing the turn. The per-turn
        // controls follow the same conditional-spread rule: an absent field
        // means "deployment default", so only present overrides go on the wire.
        body: JSON.stringify({
          content,
          ...(agentId ? { agent_id: agentId } : {}),
          ...(controls?.model ? { model: controls.model } : {}),
          ...(controls?.effort ? { effort: controls.effort } : {}),
          ...(controls?.fast ? { fast: true } : {}),
          // A skill is injected for this turn only, so it rides the send like the
          // other per-turn controls. `skill_args` only travels with a skill.
          ...(controls?.skillId ? { skill_id: controls.skillId } : {}),
          ...(controls?.skillId && controls?.skillArgs
            ? { skill_args: controls.skillArgs }
            : {}),
        }),
      },
      true,
    );
  }

  cancelRun(runId: string): Promise<Run> {
    return this.request(`/api/runs/${runId}/cancel`, { method: "POST" }, true);
  }

  listSources(): Promise<Source[]> {
    return this.request("/api/sources");
  }

  uploadSource(file: File): Promise<Source> {
    const body = new FormData();
    body.set("file", file);
    return this.request("/api/sources", { method: "POST", body }, true);
  }

  deleteSource(sourceId: string): Promise<void> {
    return this.request(`/api/sources/${sourceId}`, { method: "DELETE" }, true);
  }

  /**
   * One source's stored bytes — an uploaded PDF, a chart a sandbox run drew.
   *
   * Fetched here and handed back as a Blob rather than pointed at with an
   * `<img src>`, which does not work and cannot be made to work:
   *
   * 1. An `<img>` request carries no `X-Workspace-Id`, and the API falls back
   *    to the caller's *oldest* membership when the header is absent. A user in
   *    two workspaces would find every image in the newer one 404ing. That is
   *    deterministic, not a browser quirk.
   * 2. The API is a different site from the web app, so an `<img>` to it is a
   *    third-party subresource. Safari blocks those cookies outright and Chrome
   *    is removing them; `SameSite=None` is permission to send the cookie, not
   *    a promise the browser will. The picture would vanish for some users and
   *    nobody would be able to say why.
   *
   * `request` cannot be reused because it parses JSON. This goes through
   * `dispatch`, which is the same credentialed, workspace-scoped fetch every
   * other call uses — so this needs no CORS change of any kind.
   */
  async sourceContent(sourceId: string): Promise<Blob> {
    const path = `/api/sources/${sourceId}/content`;
    const init: RequestInit = {};
    const response = await this.dispatch(path, init, this.buildHeaders(init, false), false);
    if (response.status === 401) this.signalUnauthorized();
    if (!response.ok) {
      throw new ApiError(
        (await WorkspaceApi.detailOf(response)) || `Request failed (${response.status})`,
        response.status,
      );
    }
    return response.blob();
  }

  getChunk(chunkId: string): Promise<ProvenanceChunk> {
    return this.request(`/api/chunks/${chunkId}`);
  }

  listToolCalls(): Promise<ToolCall[]> {
    return this.request("/api/tool-calls");
  }

  decideToolCall(
    toolCallId: string,
    decision: "approved" | "denied",
  ): Promise<ToolCall> {
    return this.request(
      `/api/tool-calls/${toolCallId}/decision`,
      { method: "POST", body: JSON.stringify({ decision }) },
      true,
    );
  }

  listAgentToolCalls(): Promise<AgentToolCall[]> {
    return this.request("/api/agent-tool-calls");
  }

  /**
   * What a reviewer changed about the call before allowing it.
   *
   * Both fields say the same kind of thing — the human's answer is not simply
   * yes to what the model asked — which is why they travel together rather than
   * as two more positional arguments. `inputs` carries the values typed into a
   * paused manual node, which the server validates against that node's declared
   * fields and turns into its output. `accepted_hunks` names the changes of a
   * document edit the reviewer took, by the `index` on `ReviewSegment`; omitting
   * it means the proposal was taken whole. The server records the amendment
   * without rewriting `arguments_json`, so the audit row keeps saying what the
   * model asked for and the amendment says what the human allowed.
   */
  decideAgentToolCall(
    toolCallId: string,
    decision: "approved" | "denied",
    remember = false,
    amendment: {
      inputs?: Record<string, unknown>;
      accepted_hunks?: number[];
    } = {},
  ): Promise<AgentToolCall> {
    return this.request(
      `/api/agent-tool-calls/${toolCallId}/decision`,
      {
        method: "POST",
        body: JSON.stringify({ decision, remember, ...amendment }),
      },
      true,
    );
  }

  // --- Agents (authored system prompts + provisioned tools) ---

  listAgents(): Promise<AgentInfo[]> {
    return this.request("/api/agents");
  }

  createAgent(body: AgentCreateBody): Promise<AgentInfo> {
    return this.request("/api/agents", { method: "POST", body: JSON.stringify(body) }, true);
  }

  updateAgent(agentId: string, body: AgentUpdateBody): Promise<AgentInfo> {
    return this.request(`/api/agents/${agentId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  deleteAgent(agentId: string): Promise<undefined> {
    return this.request(`/api/agents/${agentId}`, { method: "DELETE" });
  }

  // --- Skills (authored instruction blocks, invoked per turn) ---

  /** The skills visible to the caller: their own, plus the workspace's shared. */
  listSkills(): Promise<Skill[]> {
    return this.request("/api/skills");
  }

  getSkill(skillId: string): Promise<Skill> {
    return this.request(`/api/skills/${skillId}`);
  }

  createSkill(body: SkillCreateBody): Promise<Skill> {
    return this.request("/api/skills", { method: "POST", body: JSON.stringify(body) }, true);
  }

  updateSkill(skillId: string, body: SkillUpdateBody): Promise<Skill> {
    return this.request(`/api/skills/${skillId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  deleteSkill(skillId: string): Promise<undefined> {
    return this.request(`/api/skills/${skillId}`, { method: "DELETE" });
  }

  listSkillVersions(skillId: string): Promise<SkillVersion[]> {
    return this.request(`/api/skills/${skillId}/versions`);
  }

  /** Apply a retained snapshot as a new version; returns the skill at that new head. */
  restoreSkillVersion(skillId: string, versionId: string): Promise<Skill> {
    return this.request(
      `/api/skills/${skillId}/versions/${versionId}/restore`,
      { method: "POST" },
    );
  }

  /** The live tool registry, for the agent editor's provisioning checklist. */
  listTools(): Promise<ToolInfo[]> {
    return this.request("/api/tools");
  }

  listToolPolicies(): Promise<ToolPolicy[]> {
    return this.request("/api/tool-policies");
  }

  setToolPolicy(
    toolName: string,
    policy: "ask" | "allow" | "deny",
  ): Promise<ToolPolicy> {
    return this.request("/api/tool-policies", {
      method: "PUT",
      body: JSON.stringify({ tool_name: toolName, policy }),
    });
  }

  listFolders(): Promise<Folder[]> {
    return this.request("/api/folders");
  }

  createFolder(name: string, parentId = ""): Promise<Folder> {
    return this.request(
      "/api/folders",
      { method: "POST", body: JSON.stringify({ name, parent_id: parentId }) },
      true,
    );
  }

  /**
   * Rename, move, or both. An omitted field is left alone, which is why the
   * body is built from what the caller actually passed: sending `parent_id`
   * as undefined-turned-null would mean "move this to the top level".
   */
  updateFolder(
    folderId: string,
    changes: { name?: string; parent_id?: string },
  ): Promise<Folder> {
    return this.request(
      `/api/folders/${folderId}`,
      { method: "PATCH", body: JSON.stringify(changes) },
      true,
    );
  }

  /** 409 with a sentence if the folder still holds files; it never cascades. */
  deleteFolder(folderId: string): Promise<void> {
    return this.request(`/api/folders/${folderId}`, { method: "DELETE" }, true);
  }

  /** File a document, or return it to the top level with an empty folder id. */
  moveDocument(documentId: string, folderId: string): Promise<WorkspaceDocument> {
    return this.request(
      `/api/documents/${documentId}/folder`,
      { method: "PUT", body: JSON.stringify({ folder_id: folderId }) },
      true,
    );
  }

  listDocuments(): Promise<DocumentSummary[]> {
    return this.request("/api/documents");
  }

  getDocument(documentId: string): Promise<WorkspaceDocument> {
    return this.request(`/api/documents/${documentId}`);
  }

  createDocument(
    title: string,
    content = "",
    kind: DocumentKind = "markdown",
    folderId = "",
  ): Promise<WorkspaceDocument> {
    return this.request("/api/documents", {
      method: "POST",
      body: JSON.stringify({ title, content, kind, folder_id: folderId }),
    });
  }

  saveDocument(documentId: string, content: string): Promise<WorkspaceDocument> {
    return this.request(`/api/documents/${documentId}`, {
      method: "PUT",
      body: JSON.stringify({ content }),
    });
  }

  listDocumentVersions(documentId: string): Promise<DocumentVersion[]> {
    return this.request(`/api/documents/${documentId}/versions`);
  }

  restoreDocumentVersion(
    documentId: string,
    versionId: string,
  ): Promise<WorkspaceDocument> {
    return this.request(
      `/api/documents/${documentId}/versions/${versionId}/restore`,
      { method: "POST" },
    );
  }

  deleteDocument(documentId: string): Promise<void> {
    return this.request(`/api/documents/${documentId}`, { method: "DELETE" }, true);
  }

  listBoards(): Promise<Board[]> {
    return this.request("/api/boards");
  }

  createBoard(name: string, columns: string[] = []): Promise<Board> {
    return this.request("/api/boards", {
      method: "POST",
      body: JSON.stringify({ name, columns }),
    });
  }

  addBoardCard(
    boardId: string,
    column: string,
    title: string,
    body = "",
  ): Promise<Board> {
    return this.request(`/api/boards/${boardId}/cards`, {
      method: "POST",
      body: JSON.stringify({ column, title, body, labels: [] }),
    });
  }

  moveBoardCard(boardId: string, cardId: string, column: string): Promise<Board> {
    return this.request(
      `/api/boards/${boardId}/cards/${cardId}/move?column=${encodeURIComponent(column)}`,
      { method: "POST" },
    );
  }

  deleteBoardCard(boardId: string, cardId: string): Promise<Board> {
    return this.request(`/api/boards/${boardId}/cards/${cardId}`, {
      method: "DELETE",
    });
  }

  deleteBoard(boardId: string): Promise<void> {
    return this.request(`/api/boards/${boardId}`, { method: "DELETE" }, true);
  }

  /**
   * Start a todo list — a board born with the single column that shape needs.
   *
   * Through `/api/todos` rather than `createBoard(name, ["To do"])` so the
   * column a list is made with is named on the server and nowhere else; a
   * client that spelled it would be a second definition of what a list is.
   */
  createTodoList(name: string): Promise<TodoList> {
    return this.request(
      "/api/todos",
      { method: "POST", body: JSON.stringify({ name }) },
      true,
    );
  }

  /**
   * Tick an item off, or reopen it. Takes no board id, which is the entire
   * point: checking something off should not require knowing which list it
   * came from, and that is the one interaction a kanban cannot offer.
   *
   * Returns the item rather than its list, so a long list does not get slower
   * to tick the longer it gets.
   */
  setTodoItemDone(itemId: string, done: boolean): Promise<TodoItem> {
    return this.request(
      `/api/todos/items/${itemId}`,
      { method: "PATCH", body: JSON.stringify({ done }) },
      true,
    );
  }

  listDbConnections(): Promise<DbConnection[]> {
    return this.request("/api/db/connections");
  }

  createDbConnection(input: DbConnectionInput): Promise<DbConnection> {
    return this.request("/api/db/connections", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** Dial the database; an unreachable one comes back with status "error". */
  testDbConnection(connectionId: string): Promise<DbConnection> {
    return this.request(`/api/db/connections/${connectionId}/test`, {
      method: "POST",
    });
  }

  /** Omit `table` for the table list; pass one for its columns and foreign keys. */
  getDbSchema(connectionId: string, table?: string): Promise<DbSchema> {
    const query = table ? `?table=${encodeURIComponent(table)}` : "";
    return this.request(`/api/db/connections/${connectionId}/schema${query}`);
  }

  deleteDbConnection(connectionId: string): Promise<void> {
    return this.request(
      `/api/db/connections/${connectionId}`,
      { method: "DELETE" },
      true,
    );
  }

  listProjects(): Promise<ProjectSummary[]> {
    return this.request("/api/projects");
  }

  getProject(projectId: string): Promise<WorkspaceProject> {
    return this.request(`/api/projects/${projectId}`);
  }

  createProject(
    name: string,
    description = "",
    kind: ProjectKind = "web",
  ): Promise<WorkspaceProject> {
    // entry_path is omitted so the server picks the default for the kind
    // (index.tsx for web, main.tex for latex).
    return this.request("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name, description, kind }),
    });
  }

  /** Returns the stored file, whose `path` is normalized and may differ from `path`. */
  saveProjectFile(
    projectId: string,
    path: string,
    content: string,
  ): Promise<ProjectFile> {
    return this.request(`/api/projects/${projectId}/files`, {
      method: "PUT",
      body: JSON.stringify({ path, content }),
    });
  }

  deleteProjectFile(projectId: string, path: string): Promise<void> {
    return this.request(
      `/api/projects/${projectId}/files?path=${encodeURIComponent(path)}`,
      { method: "DELETE" },
      true,
    );
  }

  deleteProject(projectId: string): Promise<void> {
    return this.request(`/api/projects/${projectId}`, { method: "DELETE" }, true);
  }

  addBoardColumn(boardId: string, name: string, index?: number): Promise<Board> {
    return this.request(`/api/board-ops/${boardId}/columns`, {
      method: "POST",
      body: JSON.stringify({ name, index: index ?? null }),
    });
  }

  renameBoardColumn(boardId: string, columnId: string, name: string): Promise<Board> {
    return this.request(`/api/board-ops/${boardId}/columns/${columnId}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    });
  }

  /** `moveCardsTo` rehomes the column's cards; without it a non-empty column is refused. */
  deleteBoardColumn(
    boardId: string,
    columnId: string,
    moveCardsTo?: string,
  ): Promise<Board> {
    const query = moveCardsTo ? `?move_cards_to=${encodeURIComponent(moveCardsTo)}` : "";
    return this.request(`/api/board-ops/${boardId}/columns/${columnId}${query}`, {
      method: "DELETE",
    });
  }

  /** `order` must be a permutation of the board's column ids. */
  reorderBoardColumns(boardId: string, order: string[]): Promise<Board> {
    return this.request(`/api/board-ops/${boardId}/columns/reorder`, {
      method: "POST",
      body: JSON.stringify({ order }),
    });
  }

  reorderBoardCard(
    boardId: string,
    cardId: string,
    index: number,
    columnId = "",
  ): Promise<Board> {
    return this.request(`/api/board-ops/${boardId}/cards/${cardId}/reorder`, {
      method: "POST",
      body: JSON.stringify({ index, column_id: columnId }),
    });
  }

  listPendingDocumentEdits(): Promise<PendingDocumentEdit[]> {
    return this.request("/api/documents-pending");
  }

  listMcpServers(): Promise<McpServer[]> {
    return this.request("/api/mcp/servers");
  }

  createMcpServer(input: McpServerInput): Promise<McpServer> {
    return this.request("/api/mcp/servers", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** Reconnect and re-enumerate tools; an unreachable server returns status "error". */
  refreshMcpServer(serverId: string): Promise<McpServer> {
    return this.request(`/api/mcp/servers/${serverId}/refresh`, { method: "POST" });
  }

  setMcpServerEnabled(serverId: string, enabled: boolean): Promise<McpServer> {
    return this.request(
      `/api/mcp/servers/${serverId}?enabled=${enabled}`,
      { method: "PATCH" },
    );
  }

  setMcpToolEnabled(toolId: string, enabled: boolean): Promise<McpTool> {
    return this.request(`/api/mcp/tools/${toolId}?enabled=${enabled}`, {
      method: "PATCH",
    });
  }

  deleteMcpServer(serverId: string): Promise<void> {
    return this.request(`/api/mcp/servers/${serverId}`, { method: "DELETE" }, true);
  }

  getMcpAuthStatus(serverId: string): Promise<McpAuthStatus> {
    return this.request(`/api/mcp/servers/${serverId}/auth`);
  }

  /**
   * Discover, register, and get the URL to send the browser to.
   *
   * The caller navigates to `authorize_url`; it must not be opened in a hidden
   * frame, because the user has to see which authorization server they are
   * granting access at. The API keeps the PKCE verifier, so nothing secret is
   * in the URL this returns.
   */
  connectMcpServer(serverId: string): Promise<{ authorize_url: string }> {
    return this.request(`/api/mcp/servers/${serverId}/connect`, { method: "POST" });
  }

  /** Forget this user's token. Other members of the workspace keep theirs. */
  disconnectMcpServer(serverId: string): Promise<McpAuthStatus> {
    return this.request(`/api/mcp/servers/${serverId}/disconnect`, { method: "POST" });
  }

  listSandboxTools(): Promise<SandboxTool[]> {
    return this.request("/api/sandbox-tools");
  }

  createSandboxTool(input: SandboxToolInput): Promise<SandboxTool> {
    return this.request("/api/sandbox-tools", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  /** Partial update — the toggle sends only `{ enabled }`, the editor the rest. */
  updateSandboxTool(id: string, input: Partial<SandboxToolInput>): Promise<SandboxTool> {
    return this.request(`/api/sandbox-tools/${id}`, {
      method: "PATCH",
      body: JSON.stringify(input),
    });
  }

  deleteSandboxTool(id: string): Promise<void> {
    return this.request(`/api/sandbox-tools/${id}`, { method: "DELETE" }, true);
  }

  listAuditEvents(): Promise<AuditEvent[]> {
    return this.request("/api/audit-events");
  }

  listIntegrations(): Promise<IntegrationProvider[]> {
    return this.request("/api/integrations");
  }

  connectIntegration(provider: string): Promise<{ authorize_url: string }> {
    return this.request(
      `/api/integrations/${provider}/connect`,
      { method: "POST" },
      true,
    );
  }

  connectGarmin(email: string, password: string): Promise<IntegrationAccount> {
    return this.request(
      "/api/integrations/garmin/credentials",
      { method: "POST", body: JSON.stringify({ email, password }) },
      true,
    );
  }

  disconnectIntegration(accountId: string): Promise<void> {
    return this.request(
      `/api/integrations/${accountId}`,
      { method: "DELETE" },
      true,
    );
  }

  syncIntegration(accountId: string): Promise<SyncJob> {
    return this.request(
      `/api/integrations/${accountId}/sync`,
      { method: "POST" },
      true,
    );
  }

  listSyncJobs(accountId: string): Promise<SyncJob[]> {
    return this.request(`/api/integrations/${accountId}/jobs`);
  }

  listMemory(): Promise<MemoryItem[]> {
    return this.request("/api/memory");
  }

  deleteMemory(memoryId: string): Promise<void> {
    return this.request(`/api/memory/${memoryId}`, { method: "DELETE" }, true);
  }

  getGraph(limit = 100): Promise<KnowledgeGraph> {
    return this.request(`/api/graph?limit=${limit}`);
  }

  rebuildGraph(): Promise<KnowledgeGraph> {
    return this.request("/api/graph/rebuild", { method: "POST" }, true);
  }

  listDatasets(): Promise<Dataset[]> {
    return this.request("/api/datasets");
  }

  createDataset(
    name: string,
    sourceId: string,
    description = "",
  ): Promise<Dataset> {
    return this.request(
      "/api/datasets",
      {
        method: "POST",
        body: JSON.stringify({
          name,
          description,
          source_id: sourceId,
        }),
      },
      true,
    );
  }

  createDatasetVersion(datasetId: string, sourceId: string): Promise<Dataset> {
    return this.request(
      `/api/datasets/${datasetId}/versions`,
      {
        method: "POST",
        body: JSON.stringify({ source_id: sourceId }),
      },
      true,
    );
  }

  queryDataset(
    datasetId: string,
    query: DatasetQuery,
  ): Promise<DatasetQueryResult> {
    return this.request(`/api/datasets/${datasetId}/query`, {
      method: "POST",
      body: JSON.stringify(query),
    });
  }

  listDashboards(): Promise<Dashboard[]> {
    return this.request("/api/dashboards");
  }

  createDashboard(payload: {
    name: string;
    description?: string;
    dataset_id: string;
    spec: DashboardSpec;
  }): Promise<Dashboard> {
    return this.request(
      "/api/dashboards",
      { method: "POST", body: JSON.stringify(payload) },
      true,
    );
  }

  runDashboard(dashboardId: string): Promise<DashboardRun> {
    return this.request(`/api/dashboards/${dashboardId}/run`, {
      method: "POST",
    });
  }

  deleteDashboard(dashboardId: string): Promise<void> {
    return this.request(`/api/dashboards/${dashboardId}`, { method: "DELETE" }, true);
  }

  // --- Dashboard templates --------------------------------------------------
  // The two writes below go through `workflowWrite`, and throw
  // `WorkflowCompileError`, on purpose rather than by accident. A template that
  // will not compile and a graph that will not compile are the same mistake — a
  // declared contract the supplied thing does not satisfy — and the API returns
  // the identical body for both (`api/dashboards.py:_bind_failure` says so in
  // as many words). One error shape, one renderer, one repair prompt; a second
  // near-identical class here would be a second thing to keep in step.

  listDashboardTemplates(): Promise<DashboardTemplate[]> {
    return this.request("/api/dashboard-templates");
  }

  createDashboardTemplate(payload: {
    name: string;
    description?: string;
    required_columns: DashboardColumnRequirement[];
    spec: DashboardSpec;
  }): Promise<DashboardTemplate> {
    return this.workflowWrite("/api/dashboard-templates", "POST", payload);
  }

  deleteDashboardTemplate(templateId: string): Promise<void> {
    return this.request(
      `/api/dashboard-templates/${templateId}`,
      { method: "DELETE" },
      true,
    );
  }

  /** Point a template at real data, or be told exactly what the data lacks. */
  bindDashboardTemplate(
    templateId: string,
    payload: {
      name: string;
      description?: string;
      dataset_id: string;
      column_bindings?: Record<string, string>;
    },
  ): Promise<Dashboard> {
    return this.workflowWrite(
      `/api/dashboard-templates/${templateId}/bind`,
      "POST",
      payload,
    );
  }

  // --- Pins — the caller's own home screen, and nobody else's ---------------

  listDashboardPins(): Promise<DashboardPin[]> {
    return this.request("/api/dashboard-pins");
  }

  /**
   * Pin, or move a pin. Omitting the placement puts the tile below everything
   * already there, which is what "pin this" means before anyone has dragged.
   */
  pinDashboard(
    dashboardId: string,
    placement: Partial<Omit<DashboardLayoutTile, "dashboard_id">> = {},
  ): Promise<DashboardPin> {
    return this.request(
      `/api/dashboards/${dashboardId}/pin`,
      { method: "PUT", body: JSON.stringify(placement) },
      true,
    );
  }

  unpinDashboard(dashboardId: string): Promise<void> {
    return this.request(
      `/api/dashboards/${dashboardId}/pin`,
      { method: "DELETE" },
      true,
    );
  }

  /** The whole grid in one write, because dragging one tile moves its neighbours. */
  saveDashboardLayout(tiles: DashboardLayoutTile[]): Promise<DashboardPin[]> {
    return this.request(
      "/api/dashboard-pins/layout",
      { method: "PUT", body: JSON.stringify({ tiles }) },
      true,
    );
  }

  listApps(): Promise<GeneratedApp[]> {
    return this.request("/api/apps");
  }

  createApp(payload: {
    name: string;
    slug: string;
    description?: string;
    visibility: "private" | "public";
    app_type?: "dashboard" | "code";
    dashboard_ids: string[];
  }): Promise<GeneratedApp> {
    return this.request(
      "/api/apps",
      { method: "POST", body: JSON.stringify(payload) },
      true,
    );
  }

  generateApp(
    appId: string,
    payload: { prompt: string; dataset_ids: string[] },
  ): Promise<GeneratedApp> {
    return this.request(
      `/api/apps/${appId}/generate`,
      { method: "POST", body: JSON.stringify(payload) },
      true,
    );
  }

  frameUrl(appId: string, releaseId: string): string {
    return `${this.baseUrl}/api/apps/${appId}/releases/${releaseId}/frame`;
  }

  publishedFrameUrl(slug: string): string {
    return `${this.baseUrl}/published/apps/${encodeURIComponent(slug)}/frame`;
  }

  createAppRelease(appId: string, dashboardIds: string[]): Promise<GeneratedApp> {
    return this.request(
      `/api/apps/${appId}/releases`,
      { method: "POST", body: JSON.stringify({ dashboard_ids: dashboardIds }) },
      true,
    );
  }

  publishAppRelease(appId: string, releaseId: string): Promise<GeneratedApp> {
    return this.request(
      `/api/apps/${appId}/releases/${releaseId}/publish`,
      { method: "POST" },
      true,
    );
  }

  rollbackAppRelease(appId: string, releaseId: string): Promise<GeneratedApp> {
    return this.request(
      `/api/apps/${appId}/rollback/${releaseId}`,
      { method: "POST" },
      true,
    );
  }

  // --- Workspace administration ---------------------------------------------
  // Owner-only, workspace-scoped. There is no listAdminWorkspaces(): the
  // workspaces panel reads `listWorkspaces()` above, because a second endpoint
  // over the same memberships would be a second place for the filter to be
  // wrong.

  listAdminMembers(limit = 100): Promise<AdminMember[]> {
    return this.request(`/api/admin/members?limit=${limit}`);
  }

  listAdminAuditEvents(limit = 50, offset = 0): Promise<AdminAuditPage> {
    return this.request(`/api/admin/audit-events?limit=${limit}&offset=${offset}`);
  }

  getAdminActivity(limit = 20): Promise<AdminActivity> {
    return this.request(`/api/admin/activity?limit=${limit}`);
  }

  listAdminSandboxSessions(limit = 50): Promise<AdminSandboxSession[]> {
    return this.request(`/api/admin/sandbox-sessions?limit=${limit}`);
  }

  /** Destroy a machine. Answers with the killed row, not 204. */
  killAdminSandboxSession(sessionId: string): Promise<AdminSandboxSession> {
    return this.request(`/api/admin/sandbox-sessions/${sessionId}`, {
      method: "DELETE",
    }, true);
  }

  listAdminMcpServers(limit = 50): Promise<AdminMcpServer[]> {
    return this.request(`/api/admin/mcp-servers?limit=${limit}`);
  }

  getAdminStorage(): Promise<AdminStorage> {
    return this.request("/api/admin/storage");
  }

  /**
   * Token and cost accounting over a window. The API caps `days` at 365 and
   * rejects anything outside 1–365, because every figure is an aggregate over
   * the fastest-growing table in the schema.
   */
  getAdminUsage(days = 30): Promise<AdminUsage> {
    return this.request(`/api/admin/usage?days=${days}`);
  }

  /**
   * Latency, throughput, error rate, live runs, and retention over a window.
   *
   * Owner-only like the rest of `/api/admin`; a non-owner gets the same 403 the
   * usage and budget reads give. The API caps `hours` at 720 (30 days), because
   * time-to-first-token is derived from the run-events table and the window is
   * what bounds that scan.
   */
  getAdminObservability(hours = 24): Promise<AdminObservability> {
    return this.request(`/api/admin/observability?hours=${hours}`);
  }

  /**
   * The spend ceiling, the spend against it, and the runs it is holding.
   *
   * Owner-only like the rest of `/api/admin`, and a member who is not an owner
   * gets a 403 with "Owner role required". That answer is worth catching rather
   * than shouting about: it is what tells a surface to offer "ask an owner"
   * instead of a control the caller cannot use.
   */
  getAdminBudget(): Promise<AdminBudget> {
    return this.request("/api/admin/budget");
  }

  /**
   * Set the ceiling — and release whatever the new one no longer stops.
   *
   * One gesture, because they are one intention: the API re-evaluates every
   * parked run against the same predicate the loop enforces and resumes the
   * ones that now fit, reporting them in `resumed_run_ids`. A raise that is
   * still not enough is not a special case; those runs simply stay parked.
   */
  setAdminBudget(input: AdminBudgetInput): Promise<AdminBudget> {
    // No idempotency key: the route stores an absolute ceiling rather than
    // applying a delta, so replaying the same body is the same state.
    return this.request("/api/admin/budget", {
      method: "PUT",
      body: JSON.stringify(input),
    });
  }

  // --- Sandbox (server-side execution) --------------------------------------
  // Every method here names a session by its workspace-scoped row id. There is
  // no method that takes a provider id, because the API accepts none.

  listSandboxSessions(): Promise<SandboxSession[]> {
    return this.request("/api/sandbox");
  }

  /** Ensure a session exists for `project_id`; the server reuses a live one. */
  createSandboxSession(projectId = "", label = ""): Promise<SandboxSession> {
    return this.request(
      "/api/sandbox",
      { method: "POST", body: JSON.stringify({ project_id: projectId, label }) },
      true,
    );
  }

  /**
   * Run in the session's persistent kernel ("code") or its shell ("command").
   * Failing *user* code resolves with `execution.error` set; only a provider
   * failure rejects, so the caller must check both.
   */
  runInSandbox(
    sessionId: string,
    source: string,
    kind: SandboxExecutionKind = "code",
  ): Promise<SandboxRun> {
    return this.request(
      `/api/sandbox/${sessionId}/run`,
      { method: "POST", body: JSON.stringify({ source, kind, language: "python" }) },
      true,
    );
  }

  listSandboxExecutions(sessionId: string, limit = 50): Promise<SandboxExecution[]> {
    return this.request(`/api/sandbox/${sessionId}/executions?limit=${limit}`);
  }

  pauseSandboxSession(sessionId: string): Promise<SandboxSession> {
    return this.request(`/api/sandbox/${sessionId}/pause`, { method: "POST" }, true);
  }

  /** Destroy the machine. Answers with the killed row, not 204. */
  killSandboxSession(sessionId: string): Promise<SandboxSession> {
    return this.request(`/api/sandbox/${sessionId}`, { method: "DELETE" }, true);
  }

  // --- Workflow automations (ADR 0007) --------------------------------------
  // Three of these do not go through `request`, and the reason is the feature's
  // central claim: a graph that names a tool this workspace does not have is
  // refused at compile time, with *every* finding collected rather than the
  // first. That report is a list, `request` keeps only a string, and a screen
  // that has to read like a helpful message cannot be built on "Request failed
  // (422)". `workflowWrite` walks the same dispatch and CSRF-retry path and
  // keeps the report.

  private async workflowWrite<T>(
    path: string,
    method: string,
    body: unknown,
  ): Promise<T> {
    const init: RequestInit = { method, body: JSON.stringify(body) };
    const headers = this.buildHeaders(init, true);
    let response = await this.dispatch(path, init, headers, true);
    if (
      response.status === 403 &&
      /csrf/i.test(await WorkspaceApi.detailOf(response))
    ) {
      try {
        await this.me();
      } catch {
        // Leave the retry to produce the authoritative error.
      }
      response = await this.dispatch(path, init, headers, true);
    }
    if (response.status === 401) this.signalUnauthorized();
    if (!response.ok) throw await workflowFailure(response);
    return (await response.json()) as T;
  }

  /** Natural language to a validated DAG. Saves nothing; grants nothing. */
  compileWorkflow(sourcePrompt: string): Promise<WorkflowCompileResult> {
    return this.workflowWrite("/api/workflows/compile", "POST", {
      source_prompt: sourcePrompt,
    });
  }

  listWorkflows(): Promise<Workflow[]> {
    return this.request("/api/workflows");
  }

  /** Either a sentence to compile or an already-formed graph, never neither. */
  createWorkflow(payload: WorkflowCreateInput): Promise<Workflow> {
    return this.workflowWrite("/api/workflows", "POST", payload);
  }

  getWorkflow(workflowId: string): Promise<Workflow> {
    return this.request(`/api/workflows/${workflowId}`);
  }

  /** Enable, disable, or recompile — a recompile bumps `version`. */
  updateWorkflow(workflowId: string, payload: WorkflowUpdateInput): Promise<Workflow> {
    return this.workflowWrite(`/api/workflows/${workflowId}`, "PATCH", payload);
  }

  /** Deletes the run history with it; the API says so and means it. */
  deleteWorkflow(workflowId: string): Promise<void> {
    return this.request(`/api/workflows/${workflowId}`, { method: "DELETE" }, true);
  }

  /**
   * Start a run now. 202: the graph executes on a background task.
   *
   * Routed through `workflowWrite` rather than `request` for the same reason
   * the compile is: a payload that does not satisfy the graph's declared inputs
   * comes back as a *list* of problems, one per field, and `request` keeps only
   * a string. A form that can only say "Request failed (422)" cannot put the
   * error next to the box that caused it.
   */
  runWorkflow(
    workflowId: string,
    payload: Record<string, unknown> = {},
  ): Promise<WorkflowRun> {
    return this.workflowWrite(`/api/workflows/${workflowId}/run`, "POST", {
      payload,
    });
  }

  listWorkflowRuns(workflowId: string, limit = 50): Promise<WorkflowRun[]> {
    return this.request(`/api/workflows/${workflowId}/runs?limit=${limit}`);
  }

  /** One run with every node's state, including the ones that never ran. */
  getWorkflowRun(workflowRunId: string): Promise<WorkflowRunDetail> {
    return this.request(`/api/workflows/runs/${workflowRunId}`);
  }

  // --- Personal crons (automation) ------------------------------------------
  // Plain `this.request`, not `workflowWrite`: a cron does not compile, so
  // there is no findings report to preserve — a bad schedule or IANA zone is a
  // single-sentence 422 that `request` already surfaces next to the form. The
  // ticker that dispatches these is the same one workflows use, so its armed
  // state is read through `workflowSchedulingEnabled()` above, not a second
  // probe.

  listCrons(): Promise<Cron[]> {
    return this.request("/api/crons");
  }

  createCron(payload: CronCreateInput): Promise<Cron> {
    return this.request(
      "/api/crons",
      { method: "POST", body: JSON.stringify(payload) },
      true,
    );
  }

  getCron(cronId: string): Promise<Cron> {
    return this.request(`/api/crons/${cronId}`);
  }

  /** Edit fields or flip `enabled`; a disabled cron never fires. */
  updateCron(cronId: string, payload: CronUpdateInput): Promise<Cron> {
    return this.request(
      `/api/crons/${cronId}`,
      { method: "PATCH", body: JSON.stringify(payload) },
      true,
    );
  }

  deleteCron(cronId: string): Promise<void> {
    return this.request(`/api/crons/${cronId}`, { method: "DELETE" }, true);
  }

  /**
   * Fire this cron now, ignoring its schedule. 202: a task cron enqueues a
   * fresh unattended run, a message cron posts synchronously. The claim column
   * is untouched, so the next scheduled minute still fires on its own.
   */
  runCronNow(cronId: string): Promise<void> {
    return this.request(`/api/crons/${cronId}/run-now`, { method: "POST" }, true);
  }

  /**
   * Can a stored cron actually fire? Asked of the ticker, which is the only
   * thing that knows.
   *
   * ADR 0007 forbids the UI from describing a schedule as active when nothing
   * dispatches it, and no other endpoint reports that: `WORKFLOW_CRON_SECRET`
   * is a server secret and is deliberately absent from `/api/bootstrap`. So
   * this asks `POST /api/workflows/tick` without presenting the secret. Unset
   * → 503, "scheduling is not configured". Set → 401, because the endpoint is
   * armed and we did not prove we are the cron. Neither answer dispatches
   * anything: the secret comparison happens before `dispatch_due`, and the
   * route takes no arguments, so there is nothing to name and nothing to fire.
   *
   * It must not go through `request`, because a 401 here means the opposite of
   * what it means everywhere else — the ticker is *working* — and signalling it
   * would sign the user out of a configured deployment on sight.
   */
  async workflowSchedulingEnabled(): Promise<boolean> {
    const init: RequestInit = { method: "POST" };
    const response = await this.dispatch(
      "/api/workflows/tick",
      init,
      this.buildHeaders(init, true),
      true,
    );
    return response.status !== 503;
  }

  async *streamRun(runId: string, after = 0): AsyncGenerator<RunEvent> {
    // A long-lived cross-origin GET still needs the cookie, or chat goes quiet
    // the moment authentication lands. GET is CSRF-exempt, so no header here.
    const headers = new Headers(this.headers);
    headers.set("Accept", "text/event-stream");
    if (this.workspaceId) headers.set(WORKSPACE_HEADER, this.workspaceId);
    let response: Response;
    try {
      response = await fetch(
        `${this.baseUrl}/api/runs/${runId}/events?after=${after}`,
        { headers, credentials: "include" },
      );
    } catch {
      throw new ApiError(`Cannot reach the API at ${this.baseUrl}`, 0);
    }
    if (response.status === 401) this.signalUnauthorized();
    if (!response.ok || !response.body) {
      throw new ApiError("Could not open the run event stream", response.status);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
        if (!frame || frame.startsWith(":")) continue;
        let id = 0;
        let event = "message";
        let data = "{}";
        for (const line of frame.split("\n")) {
          if (line.startsWith("id:")) id = Number(line.slice(3).trim());
          if (line.startsWith("event:")) event = line.slice(6).trim();
          if (line.startsWith("data:")) data = line.slice(5).trim();
        }
        yield { id, event, data: JSON.parse(data) as Record<string, unknown> };
      }
    }
  }
}

// --- Workflow automations (ADR 0007) ----------------------------------------
// The graph is a document, and these mirror `services/workflows/dag.py` field
// for field. Every node object carries every field — the API dumps the whole
// pydantic model — so a tool node still arrives with `prompt: ""` and an agent
// node with `tool: ""`. `kind` is what tells them apart, never the emptiness.

export type WorkflowTriggerKind = "manual" | "schedule";
export type WorkflowStatus = "draft" | "active" | "disabled";
export type WorkflowNodeKind = "tool" | "agent" | "manual";

/** The comparisons a node's `when` guard may make. Mirrors dag.GUARD_OPERATORS. */
export type WorkflowGuardOperator =
  | "eq"
  | "ne"
  | "gt"
  | "lt"
  | "gte"
  | "lte"
  | "truthy"
  | "falsy"
  | "present"
  | "absent"
  | "in";

/**
 * A structured condition on whether a node runs. `left` is a reference
 * (`{{ node.output.field }}` / `{{ input.field }}`) or a literal; `right` is the
 * value it is compared against, unused by the unary operators (truthy/falsy/
 * present/absent). Data, not an expression — see dag.GuardSpec.
 */
export type WorkflowGuard = {
  left: string;
  op: WorkflowGuardOperator;
  right?: unknown;
};

export type WorkflowTrigger = {
  kind: WorkflowTriggerKind;
  /** Five fields, or "" for a manual trigger. No @daily nicknames. */
  cron: string;
  timezone: string;
};

/** JSON Schema's own type names, which is what the declaration stores. */
export type WorkflowInputType =
  | "string"
  | "number"
  | "integer"
  | "boolean"
  | "object"
  | "array";

/**
 * One declared parameter of a workflow: what a run must supply, and how to ask.
 *
 * This is the form. `type` decides the control, `choices` turns it into a
 * select, `label`/`description` are what the field says to a person, and
 * `default` pre-fills it — and, for a scheduled run with nobody there to type
 * one, *is* the value. `default: null` means no default, not "defaults to null".
 */
export type WorkflowInputSpec = {
  name: string;
  type: WorkflowInputType;
  label: string;
  description: string;
  required: boolean;
  default: unknown;
  choices: unknown[];
};

export type WorkflowGraphNode = {
  id: string;
  kind: WorkflowNodeKind;
  description: string;
  /** Tool nodes only. Statically reviewable: this is every call it can make. */
  tool: string;
  arguments: Record<string, unknown>;
  /** Agent nodes only. Which tools it calls is decided at run time. */
  prompt: string;
  /**
   * Manual nodes only. The fields a person is asked to fill when the run
   * pauses; their submitted values become this node's output object. Absent on
   * a row written before manual nodes existed, and on the other two kinds.
   */
  fields?: WorkflowInputSpec[];
  /**
   * Optional condition on whether this node runs. When it is false the node —
   * and anything reachable only through it — is skipped. Absent on a node with
   * no condition and on rows written before guards existed.
   */
  when?: WorkflowGuard | null;
  /**
   * Agent nodes only: the authored agent this step runs as, by id. "" (or
   * absent, on rows written before agents were assignable) means the workspace
   * default. Set by the editor's picker — the compiler never emits ids.
   */
  agent?: string;
};

/** Spelled from/to on the wire, which is why this is not {source,target}. */
export type WorkflowGraphEdge = { from: string; to: string };

export type WorkflowGraph = {
  name: string;
  description: string;
  trigger: WorkflowTrigger;
  /**
   * Optional on the wire, and not because the API omits it — a compile always
   * emits the key. `Workflow.graph` is the JSON that was stored when the
   * workflow was saved, so a row written before inputs existed arrives without
   * it, and a required field here would make the type lie about those rows.
   */
  inputs?: WorkflowInputSpec[];
  nodes: WorkflowGraphNode[];
  edges: WorkflowGraphEdge[];
};

export type Workflow = {
  id: string;
  name: string;
  description: string;
  /** The sentence the graph was compiled from; kept so drift stays visible. */
  source_prompt: string;
  graph: WorkflowGraph;
  version: number;
  status: WorkflowStatus;
  trigger_kind: WorkflowTriggerKind;
  schedule_cron: string;
  schedule_timezone: string;
  /** Answered against the live registry: will this park, or complete alone? */
  requires_approval: boolean;
  last_dispatched_at: string | null;
  created_at: string;
  updated_at: string;
};

/** One finding from the validator. `node` is "" for a graph-level problem. */
export type WorkflowCompileFinding = {
  code: string;
  message: string;
  node: string;
};

export type WorkflowCompileSummary = {
  name: string;
  description: string;
  trigger: WorkflowTrigger;
  /** The whole declaration, not a count, so a preview can draw the run form. */
  inputs?: WorkflowInputSpec[];
  node_count: number;
  edge_count: number;
  requires_approval: boolean;
  /** 1 on a first-pass success, 2 when the repair pass rescued it. */
  attempts: number;
  warnings: WorkflowCompileFinding[];
};

export type WorkflowCompileResult = {
  graph: WorkflowGraph;
  summary: WorkflowCompileSummary;
};

export type WorkflowCreateInput = {
  source_prompt?: string;
  graph?: WorkflowGraph;
  status?: WorkflowStatus;
};

export type WorkflowUpdateInput = {
  status?: WorkflowStatus;
  /** Either recompiles the sentence or re-validates the graph; bumps version. */
  source_prompt?: string;
  graph?: WorkflowGraph;
};

export type WorkflowRunStatus =
  | "queued"
  | "running"
  | "waiting_for_approval"
  | "succeeded"
  | "failed"
  | "cancelled";

export type WorkflowNodeRunStatus =
  | "pending"
  | "running"
  | "waiting_for_approval"
  | "succeeded"
  | "failed"
  | "skipped";

export type WorkflowNodeRun = {
  node_key: string;
  kind: WorkflowNodeKind;
  tool_name: string;
  status: WorkflowNodeRunStatus;
  attempt: number;
  arguments: Record<string, unknown>;
  /**
   * A scalar tool result comes back as a string; a `manual` node's output is
   * the JSON object of collected fields. Both are stored server-side as JSON
   * and returned parsed, so this is whichever shape the node produced.
   */
  output: unknown;
  /**
   * What authorised this node: "allow" for a standing grant or a read-only
   * tool, "ask" for a human decision on this specific call, "agent" for an
   * agent node, "" when it never reached a decision.
   */
  policy: string;
  /** The parked approval card, when this node stopped for one. */
  agent_tool_call_id: string | null;
  error: string;
  latency_ms: number;
  started_at: string | null;
  finished_at: string | null;
};

export type WorkflowRun = {
  id: string;
  workflow_id: string;
  workflow_version: number;
  /** "manual" or "schedule" — whether anybody was watching. */
  trigger: string;
  status: WorkflowRunStatus;
  /**
   * Mirrors `Run.paused_reason`, carried up from the backing run by the
   * executor: `approval` | `budget` | `""`. A graph stopped by the spend
   * ceiling and one waiting on a proposed write are both
   * `waiting_for_approval`, and only this says which.
   */
  paused_reason: string;
  /** The chat run carrying this workflow's events and approval records. */
  run_id: string | null;
  error: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
};

export type WorkflowRunDetail = WorkflowRun & { nodes: WorkflowNodeRun[] };

/**
 * A compile the validator refused, carrying every finding rather than the first.
 *
 * ADR 0007: "every check runs and every finding is collected, because the
 * caller's next move is either to show a person the whole list or to hand it
 * back to the model as a repair prompt, and both are worse one error at a time."
 */
export class WorkflowCompileError extends ApiError {
  constructor(
    message: string,
    public readonly errors: WorkflowCompileFinding[],
    public readonly warnings: WorkflowCompileFinding[],
  ) {
    super(message, 422);
  }
}

/**
 * A run the API refused because its payload does not satisfy the declaration.
 *
 * Separate from `WorkflowCompileError` because it is a different moment with a
 * different fix: the graph is fine, the form is not. `problems` is the server's
 * own list, one sentence per offending field, each naming its input.
 */
export class WorkflowInputError extends ApiError {
  constructor(public readonly problems: string[]) {
    super(problems.join("; ") || "Those inputs are not valid", 422);
  }
}

function findings(value: unknown): WorkflowCompileFinding[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const row = item as Partial<WorkflowCompileFinding>;
    if (typeof row.message !== "string") return [];
    return [
      {
        code: typeof row.code === "string" ? row.code : "",
        message: row.message,
        node: typeof row.node === "string" ? row.node : "",
      },
    ];
  });
}

/** The error a workflow write failed with, keeping a compile report intact. */
async function workflowFailure(response: Response): Promise<ApiError> {
  let detail: unknown = "";
  try {
    detail = ((await response.clone().json()) as { detail?: unknown }).detail;
  } catch {
    detail = "";
  }
  if (response.status === 422 && detail && typeof detail === "object") {
    const report = detail as { errors?: unknown; warnings?: unknown; inputs?: unknown };
    // `POST /run` refuses with `{inputs: [...]}` and nothing else, so this is
    // unambiguous — a compile report never carries the key.
    if (Array.isArray(report.inputs)) {
      return new WorkflowInputError(
        report.inputs.filter((item): item is string => typeof item === "string"),
      );
    }
    const errors = findings(report.errors);
    if (errors.length > 0) {
      return new WorkflowCompileError(
        errors.map((item) => item.message).join("; "),
        errors,
        findings(report.warnings),
      );
    }
  }
  return new ApiError(
    typeof detail === "string" && detail
      ? detail
      : `Request failed (${response.status})`,
    response.status,
  );
}

// --- Personal crons (automation) --------------------------------------------
// A recurring job an external cron fires unattended, grounded on the same
// schedule machinery as workflows (services/workflows/schedule.py) and armed by
// the same POST /api/workflows/tick. A `kind="task"` fire re-runs `prompt` as a
// fresh chat turn at WORKFLOW policy scope — so its first write-capable tool
// parks for a human, exactly as a scheduled workflow does — and a `kind="message"`
// fire posts `body` verbatim into a create-or-named conversation. Mirrors
// services/crons.py and api/crons.py field for field.

export type CronKind = "task" | "message";

export type Cron = {
  id: string;
  name: string;
  /** "task" re-runs `prompt` as a turn; "message" posts `body` to a conversation. */
  kind: CronKind;
  /** 5-field cron expression, shown verbatim — never reworded into prose. */
  schedule_cron: string;
  schedule_timezone: string;
  enabled: boolean;
  /** kind="task": re-run as a fresh unattended chat turn. "" for a message cron. */
  prompt: string;
  /** kind="message": posted verbatim into the target conversation. "" for a task. */
  body: string;
  /** The conversation a message cron posts into; "" until the first fire names one. */
  target_conversation_id: string;
  /** The atomic claim column: when the ticker last fired this cron, null if never. */
  last_dispatched_at: string | null;
  created_at: string;
  updated_at: string;
};

export type CronCreateInput = {
  name: string;
  kind: CronKind;
  schedule_cron: string;
  schedule_timezone: string;
  /** Supply for kind="task"; ignored for a message cron. */
  prompt?: string;
  /** Supply for kind="message"; ignored for a task cron. */
  body?: string;
};

export type CronUpdateInput = {
  name?: string;
  schedule_cron?: string;
  schedule_timezone?: string;
  /** The enable/disable toggle: a disabled cron never fires. */
  enabled?: boolean;
  prompt?: string;
  body?: string;
};
