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
  };
};

export type Conversation = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
};

export type Citation = {
  chunk_id: string;
  source_id: string;
  filename: string;
  ordinal: number;
  excerpt: string;
  score: number;
};

export type Message = {
  id: string;
  run_id: string;
  role: "user" | "assistant" | "tool";
  content: string;
  citations: Citation[];
  created_at: string;
};

export type Run = {
  id: string;
  conversation_id: string;
  agent_id: string;
  status: string;
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
  status: "queued" | "processing" | "ready" | "failed" | "deleted";
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
  created_at: string;
};

export type ToolPolicy = {
  tool_name: string;
  policy: "ask" | "allow" | "deny";
};

export type DocumentKind = "markdown" | "latex";

export type DocumentSummary = {
  id: string;
  title: string;
  kind: DocumentKind;
  characters: number;
  updated_at: string;
};

export type WorkspaceDocument = {
  id: string;
  title: string;
  kind: DocumentKind;
  content: string;
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

/** A document write the agent proposed and the user has not decided yet. */
export type PendingDocumentEdit = {
  id: string;
  run_id: string;
  name: string;
  document_id: string;
  title: string;
  proposal_preview: string;
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
  created_at: string;
  updated_at: string;
};

export type DashboardRun = {
  dashboard: Dashboard;
  result: DatasetQueryResult;
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

export type AppPreview = {
  name: string;
  slug: string;
  description: string;
  version: number;
  status: string;
  manifest: AppManifest;
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
  artifact_count: number;
  duration_ms: number;
  created_at: string;
};

/**
 * A chart or image an execution produced. Every field past `kind`/`mime` is
 * optional because which ones are populated is the server's artifact layer's
 * choice: large payloads are written to the object store and named by `url`,
 * small ones ride back inline as base64 in `data`.
 */
export type SandboxArtifact = {
  kind: string;
  mime: string;
  url?: string;
  data?: string;
  is_main?: boolean;
  /** Structured chart description, strictly better than a PNG when present. */
  chart_json?: string;
};

export type SandboxRun = {
  execution: SandboxExecution;
  artifacts: SandboxArtifact[];
  /** True when output was clipped, so the panel can say so. */
  truncated: boolean;
  /** The session as it stands after the run — status and counters included. */
  session: SandboxSession;
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
  ): Promise<SendMessageResponse> {
    return this.request(
      `/api/conversations/${conversationId}/messages`,
      {
        method: "POST",
        // A workspace with no agent bootstraps as "", and an empty agent_id is
        // not a valid selection — omitting the field lets the API pick the
        // workspace's own agent instead of failing the turn.
        body: JSON.stringify({ content, ...(agentId ? { agent_id: agentId } : {}) }),
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

  /** Approve or deny a parked agent tool call; `remember` stores it as a policy. */
  decideAgentToolCall(
    toolCallId: string,
    decision: "approved" | "denied",
    remember = false,
  ): Promise<AgentToolCall> {
    return this.request(
      `/api/agent-tool-calls/${toolCallId}/decision`,
      { method: "POST", body: JSON.stringify({ decision, remember }) },
      true,
    );
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
  ): Promise<WorkspaceDocument> {
    return this.request("/api/documents", {
      method: "POST",
      body: JSON.stringify({ title, content, kind }),
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

  previewApp(appId: string): Promise<AppPreview> {
    return this.request(`/api/apps/${appId}/preview`);
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
