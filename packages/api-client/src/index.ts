export type Identity = {
  user_id: string;
  user_name: string;
  workspace_id: string;
  workspace_name: string;
  role: string;
};

export type Bootstrap = {
  identity: Identity;
  default_agent_id: string;
  feature_flags: Record<string, boolean>;
  model_provider: {
    provider: "deterministic" | "openai";
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

function makeKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}

export class WorkspaceApi {
  constructor(
    public readonly baseUrl = "http://localhost:8000",
    private readonly headers: Record<string, string> = {},
  ) {}

  private async request<T>(
    path: string,
    init: RequestInit = {},
    mutation = false,
  ): Promise<T> {
    const headers = new Headers(this.headers);
    if (init.body && !(init.body instanceof FormData)) {
      headers.set("Content-Type", "application/json");
    }
    if (mutation && !headers.has("Idempotency-Key")) {
      headers.set("Idempotency-Key", makeKey());
    }
    const response = await fetch(`${this.baseUrl}${path}`, {
      ...init,
      headers,
      credentials: "include",
    });
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try {
        const body = (await response.json()) as { detail?: string };
        message = body.detail || message;
      } catch {
        // Keep the status-based message.
      }
      throw new ApiError(message, response.status);
    }
    if (response.status === 204) {
      return undefined as T;
    }
    return (await response.json()) as T;
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
      { method: "POST", body: JSON.stringify({ content, agent_id: agentId }) },
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

  async *streamRun(runId: string, after = 0): AsyncGenerator<RunEvent> {
    const response = await fetch(
      `${this.baseUrl}/api/runs/${runId}/events?after=${after}`,
      {
        headers: { ...this.headers, Accept: "text/event-stream" },
        credentials: "include",
      },
    );
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
