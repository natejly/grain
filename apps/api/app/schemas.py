from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Identity(ApiModel):
    user_id: str
    user_name: str
    workspace_id: str
    workspace_name: str
    role: str


class ModelProviderStatus(BaseModel):
    provider: Literal["deterministic", "openai"]
    configured: bool
    model: str


class BootstrapResponse(ApiModel):
    identity: Identity
    feature_flags: Dict[str, bool]
    default_agent_id: str
    model_provider: ModelProviderStatus


class AgentOut(ApiModel):
    id: str
    name: str
    instructions: str


class ConversationCreate(BaseModel):
    title: str = Field(default="New conversation", max_length=200)


class ConversationOut(ApiModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime


class Citation(BaseModel):
    chunk_id: str
    source_id: str
    filename: str
    ordinal: int
    excerpt: str
    score: float


class MessageOut(ApiModel):
    id: str
    run_id: str
    role: str
    content: str
    citations: List[Citation] = []
    created_at: datetime


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    agent_id: Optional[str] = None


class RunOut(ApiModel):
    id: str
    conversation_id: str
    agent_id: str
    status: str
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
    created_at: datetime
    updated_at: datetime


class DashboardRunOut(BaseModel):
    dashboard: DashboardOut
    result: DatasetQueryResult


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


class AppPreviewOut(BaseModel):
    name: str
    slug: str
    description: str
    version: int
    status: str
    manifest: Dict[str, Any]
