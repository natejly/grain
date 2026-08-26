"""A reusable cross-tenant isolation harness.

Every table in ``app/models.py`` carries a ``workspace_id`` and every query is
*supposed* to filter on it. This module exists so that claim is proved by
experiment rather than by reading, and so it keeps being proved as the app
grows.

It gives you two things:

``build_tenant`` fully populates a workspace — a user, a session, and one row in
every workspace-scoped table the product has — and hands back a client
authenticated as that user plus a dict of the ids it created. Two of those are
all a leak test needs: act as A, name B's id, and see what happens.

``ROUTE_CASES`` is the enumeration of *every* operation the app exposes, each one
written as a request that points at another tenant's resource. ``test_tenant
_isolation.py`` drives it. The coverage test in that module compares the table
against ``app.openapi()`` and fails when a route is added without a case, so a
new endpoint cannot quietly join the app without an isolation verdict.

The verdicts a case can carry:

``DENY``    the route names a foreign resource and must refuse it with exactly
            the status in ``expect`` (404 unless stated). Pinning the code, not
            just "some 4xx", is what makes the check bite: a filter dropped from
            a *later* query in the same route often still refuses, but with a
            different reason — and "422 dataset not found" versus "404 dashboard
            not found" tells the caller the dashboard exists.
``SCOPED``  the route takes no foreign id; it must answer only with the caller's
            own rows. The test asserts none of tenant B's ids appear in the body.
``PUBLIC``  the route is deliberately unauthenticated (health, the published-app
            surface, the auth entry points). Body assertions live in the
            targeted tests instead.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Dict, List, Optional

from conftest import (
    TEST_BASE_URL,
    Identity,
    authenticate,
    create_identity,
    issue_session,
)
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.clock import utcnow
from app.config import get_settings
from app.database import SessionLocal
from app.models import (
    AgentToolCall,
    ApiToken,
    AppRelease,
    AuditEvent,
    Board,
    BoardCard,
    BoardColumn,
    Chunk,
    Comment,
    Conversation,
    Cron,
    Dashboard,
    DashboardPin,
    DashboardSubscription,
    DashboardTemplate,
    Dataset,
    DbConnection,
    Document,
    DocumentVersion,
    Folder,
    GeneratedApp,
    GraphEdge,
    GraphEntity,
    GraphProjection,
    InboundAddress,
    IntegrationAccount,
    Listing,
    ListingVersion,
    McpServer,
    McpTool,
    Membership,
    MemoryItem,
    Message,
    ModelUsage,
    Monitor,
    Notification,
    OAuthState,
    OrgMembership,
    OrgToolPolicy,
    Project,
    ProjectFile,
    Run,
    RunCheckpoint,
    RunEvent,
    SandboxExecution,
    SandboxSecret,
    SandboxSession,
    SandboxTool,
    ShareLink,
    Skill,
    SkillVersion,
    Source,
    Space,
    SpaceTemplate,
    SyncJob,
    Tool,
    ToolCall,
    ToolGrant,
    ToolPolicy,
    User,
    WebhookDelivery,
    WebhookEndpoint,
    Workflow,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowTemplate,
    Workspace,
    WorkspaceInvite,
    new_id,
)
from app.services.analytics import create_dataset_version
from app.services.api_tokens import SECRET_PREFIX
from app.services.api_tokens import _digest as api_token_digest
from app.services.auth.invites import hash_token as invite_hash
from app.services.crypto import encrypt_secret
from app.services.inbound_email import hash_token as inbound_address_hash
from app.services.ingestion import object_path
from app.services.share_links import hash_token as share_link_hash

DENY = "deny"
SCOPED = "scoped"
PUBLIC = "public"

# A CSV small enough to parse instantly and wide enough to group and aggregate.
# Every cell carries the tenant's label so a cross-tenant *query* — which returns
# rows, not ids — is caught by the same leak check as everything else.
def dataset_csv(label: str) -> bytes:
    return (
        "region,amount\n"
        f"{label} secret north,10\n"
        f"{label} secret south,20\n"
        f"{label} secret north,30\n"
    ).encode()


@dataclass
class Tenant:
    """One fully populated workspace, a client logged in as its owner, and a
    second member of that same workspace who owns some personal state.

    The roommate is the *cross-person* axis, which only started existing when a
    workspace could hold two people. Every isolation question this module asks
    has two versions now — "can A see workspace B's rows" and "can A see their
    own colleague's personal rows" — and the second is not implied by the first:
    a query filtered correctly on `workspace_id` passes every cross-tenant check
    ever written here and still hands one member another member's memory.
    """

    label: str
    identity: Identity
    client: TestClient
    ids: Dict[str, str] = field(default_factory=dict)
    #: The second member, and the rows only they should be able to see.
    roommate: Optional[Identity] = None
    roommate_ids: Dict[str, str] = field(default_factory=dict)

    @property
    def workspace_id(self) -> str:
        return self.identity.workspace_id

    @property
    def user_id(self) -> str:
        return self.identity.user_id

    def id(self, kind: str) -> str:
        return self.ids[kind]

    def roommate_id(self, kind: str) -> str:
        return self.roommate_ids[kind]

    def all_ids(self) -> List[str]:
        """Every id this tenant owns, for "did any of B leak into A's body"."""
        return [self.workspace_id, self.user_id, *self.ids.values()]

    def roommate_client(self) -> TestClient:
        """A client authenticated as the second member of this same workspace."""
        assert self.roommate is not None
        client = TestClient(app=_app(), base_url=TEST_BASE_URL)
        return authenticate(client, self.roommate)


def build_tenant(label: str) -> Tenant:
    """Create a workspace with a row in every workspace-scoped table.

    Rows are written directly rather than through the API on purpose: the point
    is to have data that exists, not to re-test the create endpoints, and going
    through HTTP would make the fixture depend on the very isolation it is meant
    to measure.
    """
    identity = create_identity(
        name=f"{label} owner",
        workspace_name=f"{label} workspace",
    )
    client = TestClient(app=_app(), base_url=TEST_BASE_URL)
    authenticate(client, identity)
    ids: Dict[str, str] = {}
    workspace_id = identity.workspace_id
    user_id = identity.user_id
    settings = get_settings()

    db = SessionLocal()
    try:
        agent_id = _agent_id(db, workspace_id)
        ids["agent"] = agent_id

        conversation = Conversation(
            workspace_id=workspace_id,
            created_by=user_id,
            title=f"{label} conversation",
        )
        db.add(conversation)
        db.flush()
        ids["conversation"] = conversation.id

        run = Run(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            agent_id=agent_id,
            created_by=user_id,
            status="waiting_for_approval",
            prompt=f"{label} secret prompt",
        )
        db.add(run)
        db.flush()
        ids["run"] = run.id

        message = Message(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            run_id=run.id,
            role="user",
            content=f"{label} secret message",
        )
        db.add(message)
        db.flush()
        ids["message"] = message.id

        event = RunEvent(
            workspace_id=workspace_id,
            run_id=run.id,
            sequence=1,
            event_type="run.queued",
            payload_json=json.dumps({"status": "queued", "secret": label}),
        )
        db.add(event)
        db.flush()
        ids["run_event"] = event.id

        # The undo trail: naming another tenant's run must not revert (or even
        # acknowledge) it, and the before-state carries content, so the marker
        # rides in `before_json` where a leaked checkpoint would quote it.
        checkpoint = RunCheckpoint(
            workspace_id=workspace_id,
            run_id=run.id,
            tool_call_id="",
            tool_name="edit_document",
            kind="document",
            reversible=True,
            before_json=json.dumps(
                {
                    "existed": True,
                    "document_id": "",
                    "title": f"{label} brief",
                    "kind": "markdown",
                    "content": f"{label} secret checkpoint body",
                }
            ),
        )
        db.add(checkpoint)
        db.flush()
        ids["run_checkpoint"] = checkpoint.id

        csv = dataset_csv(label)
        source = Source(
            workspace_id=workspace_id,
            created_by=user_id,
            filename=f"{label.lower()}-figures.csv",
            media_type="text/csv",
            object_key="",
            byte_size=len(csv),
            status="ready",
            chunk_count=1,
        )
        db.add(source)
        db.flush()
        path = object_path(workspace_id, source.id, source.filename)
        path.write_bytes(csv)
        source.object_key = str(path)
        ids["source"] = source.id

        chunk = Chunk(
            workspace_id=workspace_id,
            source_id=source.id,
            ordinal=0,
            content=f"{label} secret passage",
            char_start=0,
            char_end=len(csv),
            token_count=8,
        )
        db.add(chunk)
        db.flush()
        ids["chunk"] = chunk.id

        space = Space(
            workspace_id=workspace_id,
            name=f"{label} space",
            instructions=f"{label} secret space instructions",
            created_by=user_id,
        )
        db.add(space)
        db.flush()
        ids["space"] = space.id

        # A saved starting point whose id another tenant can name: naming it
        # would let you read its instructions, instantiate it into your own
        # workspace, or delete it.
        space_template = SpaceTemplate(
            workspace_id=workspace_id,
            created_by=user_id,
            name=f"{label} space template",
            description=f"{label} secret space template",
            instructions=f"{label} secret template instructions",
            agent_ids_json=json.dumps([agent_id]),
        )
        db.add(space_template)
        db.flush()
        ids["space_template"] = space_template.id

        tool = Tool(
            workspace_id=workspace_id,
            name=f"{label.lower()}-tool",
            description=f"{label} tool",
            base_url="https://api.github.com/zen",
        )
        db.add(tool)
        db.flush()
        ids["tool"] = tool.id

        grant = ToolGrant(
            workspace_id=workspace_id, agent_id=agent_id, tool_id=tool.id
        )
        db.add(grant)
        db.flush()
        ids["tool_grant"] = grant.id

        api_token = ApiToken(
            workspace_id=workspace_id,
            user_id=user_id,
            name=f"{label} token",
            token_hash=f"hash-{workspace_id}",
        )
        db.add(api_token)
        db.flush()
        ids["api_token"] = api_token.id

        tool_call = ToolCall(
            workspace_id=workspace_id,
            run_id=run.id,
            tool_id=tool.id,
            status="proposed",
            request_url="https://api.github.com/zen",
        )
        db.add(tool_call)
        db.flush()
        ids["tool_call"] = tool_call.id

        agent_call = AgentToolCall(
            workspace_id=workspace_id,
            run_id=run.id,
            name="edit_document",
            arguments_json=json.dumps({"title": f"{label} brief", "content": "x"}),
            status="proposed",
            proposal_preview=f"{label} secret diff",
        )
        db.add(agent_call)
        db.flush()
        ids["agent_tool_call"] = agent_call.id

        policy = ToolPolicy(
            workspace_id=workspace_id,
            # ToolPolicyOut carries no id, so the marker has to live in the one
            # field it does return — otherwise a leaked policy list would sail
            # past the id check.
            tool_name=f"{label.lower()} secret policy tool",
            policy="allow",
            created_by=user_id,
        )
        db.add(policy)
        db.flush()
        ids["tool_policy"] = policy.id

        folder = Folder(
            workspace_id=workspace_id,
            name=f"{label} secret folder",
            created_by=user_id,
        )
        db.add(folder)
        db.flush()
        ids["folder"] = folder.id

        document = Document(
            workspace_id=workspace_id,
            title=f"{label} brief",
            kind="markdown",
            content=f"{label} secret document body",
            folder_id=folder.id,
            created_by=user_id,
        )
        db.add(document)
        db.flush()
        ids["document"] = document.id

        version = DocumentVersion(
            workspace_id=workspace_id,
            document_id=document.id,
            content=f"{label} older body",
            summary=f"{label} secret revision",
        )
        db.add(version)
        db.flush()
        ids["document_version"] = version.id

        # A remark on the document, and the mention it produced. The comment's
        # body carries the marker so a foreign list leaks content, not just an
        # id; the notification targets this tenant's owner so a foreign resolve
        # or a leaked feed has a personal row to be wrong about.
        comment = Comment(
            workspace_id=workspace_id,
            subject_kind="document",
            subject_id=document.id,
            body=f"{label} secret comment",
            mentions_json=json.dumps([user_id]),
            created_by=user_id,
        )
        db.add(comment)
        db.flush()
        ids["comment"] = comment.id

        notification = Notification(
            workspace_id=workspace_id,
            target_user_id=user_id,
            kind="mention",
            status="open",
            title=f"{label} secret mention",
            body=f"{label} secret comment",
            document_id=document.id,
            comment_id=comment.id,
            created_by=user_id,
        )
        db.add(notification)
        db.flush()
        ids["notification"] = notification.id

        board = Board(
            workspace_id=workspace_id, name=f"{label} board", created_by=user_id
        )
        db.add(board)
        db.flush()
        ids["board"] = board.id

        column = BoardColumn(
            workspace_id=workspace_id, board_id=board.id, name="Todo", position=0
        )
        second_column = BoardColumn(
            workspace_id=workspace_id, board_id=board.id, name="Done", position=1
        )
        db.add_all([column, second_column])
        db.flush()
        ids["board_column"] = column.id
        ids["board_column_other"] = second_column.id

        card = BoardCard(
            workspace_id=workspace_id,
            board_id=board.id,
            column_id=column.id,
            title=f"{label} secret card",
            position=0,
        )
        db.add(card)
        db.flush()
        ids["board_card"] = card.id

        # A todo list is a *one-column* board (services/artifacts/todos.py), so
        # the board above cannot stand in for one: /api/todos refuses a
        # three-column board on shape alone, and a case aimed at it would pass
        # even with the workspace filter removed.
        todo_list = Board(
            workspace_id=workspace_id, name=f"{label} list", created_by=user_id
        )
        db.add(todo_list)
        db.flush()
        ids["todo_list"] = todo_list.id
        todo_column = BoardColumn(
            workspace_id=workspace_id, board_id=todo_list.id, name="To do", position=0
        )
        db.add(todo_column)
        db.flush()
        todo_item = BoardCard(
            workspace_id=workspace_id,
            board_id=todo_list.id,
            column_id=todo_column.id,
            title=f"{label} secret errand",
            position=0,
        )
        db.add(todo_item)
        db.flush()
        ids["todo_item"] = todo_item.id

        connection = DbConnection(
            workspace_id=workspace_id,
            name=f"{label} warehouse",
            engine="sqlite",
            dsn_encrypted=encrypt_secret(f"sqlite:///{label.lower()}.db", settings),
            read_only=True,
            created_by=user_id,
        )
        db.add(connection)
        db.flush()
        ids["db_connection"] = connection.id

        project = Project(
            workspace_id=workspace_id,
            name=f"{label} project",
            description=f"{label} secret project",
            kind="web",
            entry_path="index.tsx",
            created_by=user_id,
        )
        db.add(project)
        db.flush()
        ids["project"] = project.id

        project_file = ProjectFile(
            workspace_id=workspace_id,
            project_id=project.id,
            path="index.tsx",
            content=f"// {label} secret source",
        )
        db.add(project_file)
        db.flush()
        ids["project_file"] = project_file.id

        server = McpServer(
            workspace_id=workspace_id,
            name=f"{label.lower()}-server",
            transport="stdio",
            command="/bin/true",
            created_by=user_id,
        )
        db.add(server)
        db.flush()
        ids["mcp_server"] = server.id

        mcp_tool = McpTool(
            workspace_id=workspace_id,
            server_id=server.id,
            name="read_secret",
            description=f"{label} secret tool",
        )
        db.add(mcp_tool)
        db.flush()
        ids["mcp_tool"] = mcp_tool.id

        account = IntegrationAccount(
            workspace_id=workspace_id,
            user_id=user_id,
            provider="strava",
            external_account=f"{label.lower()}-athlete",
            scopes="read",
            status="connected",
        )
        db.add(account)
        db.flush()
        ids["integration_account"] = account.id

        job = SyncJob(
            workspace_id=workspace_id,
            account_id=account.id,
            connector="strava",
            status="succeeded",
            stats_json=json.dumps({"secret": label}),
        )
        db.add(job)
        db.flush()
        ids["sync_job"] = job.id

        state = OAuthState(
            workspace_id=workspace_id,
            user_id=user_id,
            provider="strava",
            state=f"state-{new_id()}",
        )
        db.add(state)
        db.flush()
        ids["oauth_state"] = state.id
        ids["oauth_state_value"] = state.state

        audit = AuditEvent(
            workspace_id=workspace_id,
            actor_id=user_id,
            action="secret.recorded",
            resource_type="document",
            resource_id=document.id,
            detail_json=json.dumps({"secret": label}),
        )
        db.add(audit)
        db.flush()
        ids["audit_event"] = audit.id

        memory = MemoryItem(
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            run_id=run.id,
            kind="fact",
            content=f"{label} secret memory",
            normalized_key=f"{label.lower()}-secret-memory",
            status="active",
        )
        db.add(memory)
        db.flush()
        ids["memory"] = memory.id

        projection = GraphProjection(
            workspace_id=workspace_id, status="ready", version="v1", entity_count=2
        )
        db.add(projection)
        db.flush()
        ids["graph_projection"] = projection.id

        _plant_graph(db, label=label, workspace_id=workspace_id, ids=ids)

        dataset = Dataset(
            workspace_id=workspace_id,
            created_by=user_id,
            name=f"{label} figures",
            description=f"{label} secret dataset",
            current_version=1,
        )
        db.add(dataset)
        db.flush()
        dataset_version = create_dataset_version(
            db, dataset=dataset, source=source, version=1
        )
        ids["dataset"] = dataset.id
        ids["dataset_version"] = dataset_version.id

        dashboard = Dashboard(
            workspace_id=workspace_id,
            created_by=user_id,
            dataset_id=dataset.id,
            name=f"{label} dashboard",
            description=f"{label} secret dashboard",
            spec_json=json.dumps(
                {
                    "visualization": "table",
                    "query": {"limit": 10},
                    "x_field": None,
                    "y_fields": [],
                }
            ),
        )
        db.add(dashboard)
        db.flush()
        ids["dashboard"] = dashboard.id

        # A live share link onto that dashboard. The raw token is kept beside
        # the row's id, as the invite's is: the token is the whole credential
        # for `GET /shared/{token}`, so storing it under an id kind makes the
        # leak scan prove no authenticated response — the list route above all —
        # ever echoes it back.
        share_token = f"{label.lower()}-isolation-share-token"
        share_link = ShareLink(
            workspace_id=workspace_id,
            resource_kind="dashboard",
            resource_id=dashboard.id,
            token_hash=share_link_hash(share_token),
            created_by=user_id,
        )
        db.add(share_link)
        db.flush()
        ids["share_link"] = share_link.id
        ids["share_link_token"] = share_token

        # A scheduled mail of that dashboard to this tenant's owner. The table
        # carries no free text for the leak grep, but the workspace_id column
        # enrols it in the tamper digest, and the DELETE DENY below proves a
        # foreign tenant cannot silence (or discover) someone's morning mail.
        dashboard_subscription = DashboardSubscription(
            workspace_id=workspace_id,
            dashboard_id=dashboard.id,
            recipient_user_id=user_id,
            schedule_cron="0 9 * * *",
            schedule_timezone="UTC",
            enabled=True,
            created_by=user_id,
        )
        db.add(dashboard_subscription)
        db.flush()
        ids["dashboard_subscription"] = dashboard_subscription.id

        # A live API token, its raw secret kept beside the row's id exactly as
        # the invite's and share link's are: the secret is the whole credential
        # for /api/hooks, so storing it under an id kind makes the leak scan
        # prove no response — the token list above all — ever echoes it back.
        api_token_secret = f"{SECRET_PREFIX}{label.lower()}-isolation-api-token"
        api_token = ApiToken(
            workspace_id=workspace_id,
            user_id=user_id,
            name=f"{label} secret token",
            token_hash=api_token_digest(api_token_secret),
        )
        db.add(api_token)
        db.flush()
        ids["api_token"] = api_token.id
        ids["api_token_secret"] = api_token_secret

        # A live inbound email address, its raw routing token kept beside the
        # row's id exactly as the api token's secret is: the token is the whole
        # credential for landing mail in this workspace, so storing it under an
        # id kind makes the leak scan prove no response — the address list
        # above all — ever echoes it back.
        inbound_address_token = f"{label.lower()}-isolation-inbound-token"
        inbound_address = InboundAddress(
            workspace_id=workspace_id,
            token_hash=inbound_address_hash(inbound_address_token),
            label=f"{label} secret inbound address",
            target_space_id="",
            created_by=user_id,
        )
        db.add(inbound_address)
        db.flush()
        ids["inbound_address"] = inbound_address.id
        ids["inbound_address_token"] = inbound_address_token

        # An outbound webhook endpoint and one delivery on its trail. The URL
        # is owner-chosen data, so the marker lives in the *name*; the
        # delivery's payload carries a marker title because payloads are what
        # a leaked read would surface. Both rows join the tamper digest via
        # workspace_id.
        webhook_endpoint = WebhookEndpoint(
            workspace_id=workspace_id,
            name=f"{label} secret webhook",
            url=f"https://hooks.{label.lower()}.example.com/sink",
            secret_encrypted=encrypt_secret(
                f"{label.lower()}-webhook-signing-secret", settings
            ),
            events_json=json.dumps(["run.completed", "monitor.tripped"]),
            enabled=True,
            created_by=user_id,
        )
        db.add(webhook_endpoint)
        db.flush()
        ids["webhook_endpoint"] = webhook_endpoint.id

        webhook_delivery = WebhookDelivery(
            workspace_id=workspace_id,
            endpoint_id=webhook_endpoint.id,
            event="run.completed",
            payload_json=json.dumps({"title": f"{label} secret delivery"}),
            status="pending",
            attempts=0,
        )
        db.add(webhook_delivery)
        db.flush()
        ids["webhook_delivery"] = webhook_delivery.id

        # A template whose declared shape the tenant's own dataset satisfies, so
        # a cross-tenant bind fails for the reason under test (the template is
        # not yours) rather than incidentally, because the shape did not fit.
        template = DashboardTemplate(
            workspace_id=workspace_id,
            created_by=user_id,
            name=f"{label} template",
            description=f"{label} secret template",
            required_columns_json=json.dumps(
                [
                    {"name": "region", "type": "string", "description": ""},
                    {"name": "amount", "type": "number", "description": ""},
                ]
            ),
            spec_json=json.dumps(
                {
                    "visualization": "bar",
                    "query": {
                        "group_by": "region",
                        "metrics": [
                            {"field": "amount", "operation": "sum", "label": "total"}
                        ],
                        "limit": 10,
                    },
                    "x_field": "region",
                    "y_fields": ["total"],
                }
            ),
        )
        db.add(template)
        db.flush()
        ids["dashboard_template"] = template.id

        # This tenant's owner has already arranged their home screen. Pins are
        # per user, so this row is what a foreign unpin or layout save would
        # have to move — and what the tamper digest proves nothing moved.
        pin = DashboardPin(
            workspace_id=workspace_id,
            user_id=user_id,
            dashboard_id=dashboard.id,
            grid_x=0,
            grid_y=0,
            grid_w=6,
            grid_h=4,
        )
        db.add(pin)
        db.flush()
        ids["dashboard_pin"] = pin.id

        slug = f"{label.lower()}-{new_id()[:8]}"
        app_row = GeneratedApp(
            workspace_id=workspace_id,
            created_by=user_id,
            name=f"{label} app",
            slug=slug,
            description=f"{label} secret app",
            visibility="private",
            app_type="code",
        )
        db.add(app_row)
        db.flush()
        release = AppRelease(
            workspace_id=workspace_id,
            app_id=app_row.id,
            created_by=user_id,
            version=1,
            status="draft",
            manifest_json=json.dumps(
                {"kind": "code", "html": f"<p>{label} secret html</p>"}
            ),
            content_hash="0" * 64,
        )
        db.add(release)
        db.flush()
        app_row.current_release_id = release.id
        ids["app"] = app_row.id
        ids["app_slug"] = slug
        ids["app_release"] = release.id

        # A sandbox session is written straight to the table rather than through
        # the API on purpose: creating one over HTTP would need a provider, and
        # the thing under test is whether an id from this row can be used by
        # another workspace — which does not depend on a machine existing.
        sandbox = SandboxSession(
            workspace_id=workspace_id,
            created_by=user_id,
            provider="fake",
            external_id=f"{label.lower()}-{new_id()}",
            label=f"{label} secret sandbox",
            status="running",
            network_policy="open",
            allow_hosts_json="[]",
        )
        db.add(sandbox)
        db.flush()
        ids["sandbox_session"] = sandbox.id

        execution = SandboxExecution(
            workspace_id=workspace_id,
            session_id=sandbox.id,
            kind="code",
            source=f"print('{label} secret code')",
            stdout=f"{label} secret output",
            exit_code=0,
        )
        db.add(execution)
        db.flush()
        ids["sandbox_execution"] = execution.id

        # A workspace-defined sandbox tool. Written directly for the same reason
        # the sessions are: the point is to own a row whose id another tenant can
        # name, not to exercise the create route. The description carries the
        # marker string so the id-less leak scan catches it if a list route ever
        # answered with a foreign workspace's tools.
        sandbox_tool = SandboxTool(
            workspace_id=workspace_id,
            created_by=user_id,
            name=f"{label.lower()}-probe",
            description=f"{label} secret sandbox tool",
            input_schema_json='{"type":"object","properties":{}}',
            argv_json=json.dumps(["echo", "hi"]),
            egress_hosts_json="[]",
            approval="inherit",
            enabled=True,
        )
        db.add(sandbox_tool)
        db.flush()
        ids["sandbox_tool"] = sandbox_tool.id

        # A workspace secret. Keyed by name, not id, so the name itself is the
        # cross-tenant handle the DELETE route takes — planted uppercase (the
        # only shape `validate_name` accepts) and carrying the label so a list
        # route that ever answered with a foreign workspace's secrets is caught
        # by the id-less leak scan. `value_enc` is written directly rather than
        # encrypted: no route returns it, and the fixture must not depend on an
        # encryption key the isolation run does not configure.
        secret_name = f"SECRET_{label.upper()}"
        sandbox_secret = SandboxSecret(
            workspace_id=workspace_id,
            created_by=user_id,
            name=secret_name,
            value_enc=f"ciphertext-for-{label.lower()}",
        )
        db.add(sandbox_secret)
        db.flush()
        ids["sandbox_secret"] = secret_name

        # A stored automation, one execution of it, and one node inside that
        # execution. Written directly for the same reason the sandbox rows are:
        # the point is to own a workflow whose id another tenant can name, not
        # to re-test compilation. The graph is a real one — it parses and names a
        # tool every workspace has — so a route that reads it does not fall over
        # before it gets as far as the workspace filter.
        workflow = Workflow(
            workspace_id=workspace_id,
            created_by=user_id,
            name=f"{label} secret workflow",
            description=f"{label} secret automation",
            source_prompt=f"summarise {label} secret sources every monday",
            graph_json=json.dumps(
                {
                    "name": f"{label} secret workflow",
                    "description": f"{label} secret automation",
                    "trigger": {"kind": "manual", "cron": "", "timezone": "UTC"},
                    "nodes": [
                        {
                            "id": "gather",
                            "kind": "tool",
                            "tool": "search_sources",
                            "arguments": {"query": f"{label} secret"},
                            "description": "Find the passages.",
                        }
                    ],
                    "edges": [],
                }
            ),
            status="draft",
        )
        db.add(workflow)
        db.flush()
        ids["workflow"] = workflow.id

        workflow_run = WorkflowRun(
            workspace_id=workspace_id,
            workflow_id=workflow.id,
            created_by=user_id,
            workflow_version=1,
            graph_json=workflow.graph_json,
            trigger="manual",
            status="succeeded",
        )
        db.add(workflow_run)
        db.flush()
        ids["workflow_run"] = workflow_run.id

        node_run = WorkflowNodeRun(
            workspace_id=workspace_id,
            workflow_run_id=workflow_run.id,
            node_key="gather",
            kind="tool",
            tool_name="search_sources",
            status="succeeded",
            policy="allow",
            output_json=json.dumps(f"{label} secret workflow output"),
        )
        db.add(node_run)
        db.flush()
        ids["workflow_node_run"] = node_run.id

        # The automation's saved shape. The graph is the workflow's own — real
        # and parseable — so a foreign instantiate that somehow got past the
        # workspace filter would fail for the wrong reason, not this one.
        workflow_template = WorkflowTemplate(
            workspace_id=workspace_id,
            created_by=user_id,
            name=f"{label} workflow template",
            description=f"{label} secret workflow template",
            graph_json=workflow.graph_json,
            source_prompt=workflow.source_prompt,
        )
        db.add(workflow_template)
        db.flush()
        ids["workflow_template"] = workflow_template.id

        # A personal cron whose id another tenant can name: naming it would let
        # you read its prompt, fire it unattended, or delete it. `run-now`'s DENY
        # is the sharp one — it proves a foreign cron cannot be fired.
        cron = Cron(
            workspace_id=workspace_id,
            created_by=user_id,
            name=f"{label} secret cron",
            kind="task",
            schedule_cron="0 9 * * *",
            schedule_timezone="UTC",
            prompt=f"{label} secret cron prompt",
            enabled=True,
        )
        db.add(cron)
        db.flush()
        ids["cron"] = cron.id

        # A metric monitor over this tenant's dataset. Naming it would let you
        # read its query (which names dataset columns), fire an evaluation, or
        # delete the watchfulness — `run-now`'s DENY is the sharp one, and the
        # create case plants THIS tenant's dataset id to prove a monitor cannot
        # be built over another tenant's data.
        monitor = Monitor(
            workspace_id=workspace_id,
            created_by=user_id,
            name=f"{label} secret monitor",
            dataset_id=dataset.id,
            query_json=json.dumps(
                {
                    "metrics": [
                        {"field": "amount", "operation": "sum", "label": "total"}
                    ],
                    "limit": 1,
                }
            ),
            comparator="gt",
            threshold=5.0,
            schedule_cron="0 9 * * *",
            schedule_timezone="UTC",
            enabled=True,
        )
        db.add(monitor)
        db.flush()
        ids["monitor"] = monitor.id

        # A standing chat grant, so the scope split has something to be wrong
        # about: `test_workflow_policy_scope.py` proves this row does not reach
        # an unattended run, and the tamper digest proves the sweep never adds a
        # workflow-scoped sibling to it.
        policy = ToolPolicy(
            workspace_id=workspace_id,
            tool_name="search_sources",
            policy="allow",
            scope="chat",
            created_by=user_id,
        )
        db.add(policy)
        db.flush()
        ids["tool_policy"] = policy.id

        # One ledger row per tenant, so GET /api/admin/usage has something to
        # aggregate and the leak check has a foreign run id, user id and model
        # name to look for. The model name carries the label for the same reason
        # the dataset cells do: a breakdown leaks *values*, not only ids.
        model_usage = ModelUsage(
            workspace_id=workspace_id,
            run_id=run.id,
            conversation_id=conversation.id,
            user_id=user_id,
            agent_id=agent_id,
            operation="chat",
            model=f"{label}-secret-model",
            input_tokens=100,
            cached_input_tokens=10,
            output_tokens=50,
            reasoning_tokens=20,
            total_tokens=150,
            cost_usd=0.5,
            input_rate_usd_per_mtok=1.0,
            cached_input_rate_usd_per_mtok=0.1,
            output_rate_usd_per_mtok=8.0,
        )
        db.add(model_usage)
        db.flush()
        ids["model_usage"] = model_usage.id

        # An open spend anomaly on this tenant's agent — the broadcast
        # ('' -targeted) notification kind, so the anomalies feed and the
        # shared resolve route have a row whose *content* would leak, not
        # just an id.
        anomaly = Notification(
            workspace_id=workspace_id,
            target_user_id="",
            kind="spend_anomaly",
            status="open",
            title=f"{label} secret anomaly",
            body=f"{label} secret spend detail",
            agent_id=agent_id,
        )
        db.add(anomaly)
        db.flush()
        ids["anomaly"] = anomaly.id

        # A *private* skill, so the DENY cases bite on the workspace filter: a
        # foreign workspace's skill must 404 whether or not it is shared, and a
        # private one also proves a same-workspace member cannot reach it. The
        # marker body is what a leaked read would surface beyond the id.
        skill = Skill(
            workspace_id=workspace_id,
            name=f"{label.lower()}-skill",
            title=f"{label} skill",
            description=f"{label} secret skill description",
            body=f"{label} secret skill body",
            args_json="[]",
            shared=False,
            version=1,
            content_hash="",
            created_by=user_id,
        )
        db.add(skill)
        db.flush()
        ids["skill"] = skill.id
        skill_version = SkillVersion(
            workspace_id=workspace_id,
            skill_id=skill.id,
            version=1,
            title=skill.title,
            description=skill.description,
            body=skill.body,
            args_json="[]",
            created_by=user_id,
        )
        db.add(skill_version)
        db.flush()
        ids["skill_version"] = skill_version.id

        # The two rows that decide who is *in* this workspace. Neither existed
        # in this fixture before invitations did, because until then a workspace
        # could not gain a second member and there was nothing to point another
        # tenant's request at.
        #
        # The invite's raw token is kept beside its id: the token is the whole
        # credential for `POST /api/auth/invites/accept`, which names no
        # workspace at all, so the only way to aim that route at another tenant
        # is to hand it their token. It is stored under an id kind so the leak
        # scan also proves no response ever echoes it back.
        invite_token = f"{label.lower()}-isolation-invite-token"
        invite = WorkspaceInvite(
            workspace_id=workspace_id,
            email=f"{label.lower()}-invitee@example.com",
            role="member",
            token_hash=invite_hash(invite_token),
            invited_by=user_id,
            expires_at=utcnow() + timedelta(days=7),
        )
        db.add(invite)
        db.flush()
        ids["workspace_invite"] = invite.id
        ids["workspace_invite_token"] = invite_token

        membership = db.scalar(
            select(Membership).where(
                Membership.workspace_id == workspace_id, Membership.user_id == user_id
            )
        )
        assert membership is not None, "create_identity writes an owner membership"
        ids["membership"] = membership.id

        # The organization tier. `create_identity` provisions one per tenant with
        # the tenant as its admin, exactly as signup does, so the two tenants are
        # in two different organizations — which is what makes every case below a
        # real cross-org probe rather than two admins of the same org.
        organization_id = db.scalar(
            select(Workspace.organization_id).where(Workspace.id == workspace_id)
        )
        assert organization_id, "every workspace belongs to an organization"
        ids["organization"] = organization_id
        # The tenant's own user id, as an id kind, so a RouteCase can aim one
        # tenant's user at the other's organization. `PUT /api/org/members` names
        # a user in its body and nowhere else, so this is the only way to point
        # it across the boundary.
        ids["user"] = user_id

        org_policy = OrgToolPolicy(
            organization_id=organization_id,
            tool_name=f"{label.lower()}_org_tool",
            policy="deny",
            scope="chat",
        )
        db.add(org_policy)
        db.flush()
        ids["org_tool_policy"] = org_policy.id

        org_membership = db.scalar(
            select(OrgMembership).where(
                OrgMembership.organization_id == organization_id,
                OrgMembership.user_id == user_id,
            )
        )
        assert org_membership is not None, "create_identity writes an org admin"
        ids["org_membership"] = org_membership.id

        # A workspace-tier marketplace listing. The payload body carries the
        # marker string, so a cross-tenant browse or detail read that leaked it
        # would trip the same scan as everything else — a listing's payload is a
        # prompt, and a leaked prompt is the marketplace's version of the skill
        # leak above.
        listing = Listing(
            organization_id=organization_id,
            workspace_id=workspace_id,
            kind="skill",
            slug=f"{label.lower()}-listing",
            title=f"{label} listing",
            description=f"{label} secret listing description",
            visibility="workspace",
            status="published",
            author_name=f"{label} author",
            created_by=user_id,
            latest_version=1,
        )
        db.add(listing)
        db.flush()
        ids["listing"] = listing.id
        listing_version = ListingVersion(
            workspace_id=workspace_id,
            listing_id=listing.id,
            version=1,
            payload_json=json.dumps(
                {
                    "title": f"{label} listing",
                    "description": f"{label} secret listing description",
                    "body": f"{label} secret listing body",
                    "args_json": "[]",
                }
            ),
            content_hash="0" * 64,
            source_id=skill.id,
            source_version=1,
            created_by=user_id,
        )
        db.add(listing_version)
        db.flush()
        ids["listing_version"] = listing_version.id

        # An ORG-tier listing too, because the org arm of the visibility
        # chokepoint is the one query in the marketplace that deliberately
        # crosses workspaces — so the sweep must prove it still stops at the
        # *organization*. The two tenants sit in two different orgs, so every
        # case aimed at this row is a real cross-org probe.
        listing_org = Listing(
            organization_id=organization_id,
            workspace_id=workspace_id,
            kind="skill",
            slug=f"{label.lower()}-org-listing",
            title=f"{label} org listing",
            description=f"{label} secret org listing description",
            visibility="org",
            status="published",
            created_by=user_id,
            latest_version=1,
        )
        db.add(listing_org)
        db.flush()
        ids["listing_org"] = listing_org.id
        db.add(
            ListingVersion(
                workspace_id=workspace_id,
                listing_id=listing_org.id,
                version=1,
                payload_json=json.dumps(
                    {
                        "title": f"{label} org listing",
                        "description": "",
                        "body": f"{label} secret org listing body",
                        "args_json": "[]",
                    }
                ),
                content_hash="1" * 64,
                source_id=skill.id,
                source_version=1,
                created_by=user_id,
            )
        )
        db.flush()

        db.commit()
    finally:
        db.close()
    roommate, roommate_ids = _plant_roommate(label=label, workspace_id=workspace_id)
    return Tenant(
        label=label,
        identity=identity,
        client=client,
        ids=ids,
        roommate=roommate,
        roommate_ids=roommate_ids,
    )


def _plant_roommate(*, label: str, workspace_id: str) -> tuple[Identity, Dict[str, str]]:
    """A second member of this workspace, holding state only they may see.

    Everything here is personal (`owner_id` is the roommate's user id), which is
    the point: these rows sit in the *caller's own workspace*, so no
    `workspace_id` filter anywhere can exclude them. Only an owner filter can,
    and that is exactly what the sweep is then able to prove is present.

    The contents carry a distinct marker rather than the tenant label, because
    the cross-tenant leak check greps for `"{label} secret"` and would otherwise
    report a cross-person leak as a cross-tenant one.
    """
    marker = f"{label.lower()} roommate private"
    db = SessionLocal()
    try:
        user = User(email=f"{os.urandom(6).hex()}@example.com", name=f"{label} roommate")
        db.add(user)
        db.flush()
        db.add(
            Membership(workspace_id=workspace_id, user_id=user.id, role="member")
        )
        # A personal thread of their own, so the memory below has somewhere to
        # have been learned that the owner genuinely cannot read.
        conversation = Conversation(
            workspace_id=workspace_id,
            created_by=user.id,
            title=f"{marker} thread",
            shared=False,
        )
        db.add(conversation)
        db.flush()
        memory = MemoryItem(
            workspace_id=workspace_id,
            owner_id=user.id,
            conversation_id=conversation.id,
            run_id="",
            kind="fact",
            content=f"{marker} memory",
            normalized_key=f"{marker}-key",
            status="active",
        )
        db.add(memory)
        # A standing grant of their own. `search_sources` is deliberately the
        # same tool the workspace-wide row above names, so a policy lookup that
        # dropped its owner filter would find two rows for one tool and one
        # person — which is precisely what the widened unique key permits and
        # what `evaluate_policy` has to rank rather than trip over.
        policy = ToolPolicy(
            workspace_id=workspace_id,
            owner_id=user.id,
            tool_name="search_sources",
            policy="deny",
            scope="chat",
            created_by=user.id,
        )
        db.add(policy)
        db.flush()
        # An open mention of the roommate. Mentions are the first *personal*
        # notification kind, so the owner's feed showing this row would be the
        # roommate-axis leak the target_user_id filter exists to prevent.
        mention = Notification(
            workspace_id=workspace_id,
            target_user_id=user.id,
            kind="mention",
            status="open",
            title=f"{marker} mention",
            body=f"{marker} mention body",
            conversation_id=conversation.id,
            created_by=user.id,
        )
        db.add(mention)
        db.flush()
        ids = {
            "memory": memory.id,
            "tool_policy": policy.id,
            "conversation": conversation.id,
            "notification": mention.id,
        }
        user_id = user.id
        db.commit()
    finally:
        db.close()
    token, csrf_token = issue_session(user_id)
    identity = Identity(
        user_id=user_id,
        workspace_id=workspace_id,
        token=token,
        csrf_token=csrf_token,
    )
    return identity, ids


def _plant_graph(db, *, label: str, workspace_id: str, ids: Dict[str, str]) -> None:
    """Give the workspace two entities and an edge between them.

    Both tenants name one entity "Atlas", so a name-addressed graph tool that
    forgot the workspace filter would cross over and the neighbour it reports
    would carry the wrong label.
    """
    from sqlalchemy import delete

    db.execute(delete(GraphEdge).where(GraphEdge.workspace_id == workspace_id))
    db.execute(delete(GraphEntity).where(GraphEntity.workspace_id == workspace_id))
    db.flush()
    entity = GraphEntity(
        workspace_id=workspace_id,
        name="Atlas",
        normalized_name="atlas",
        entity_type="project",
        mention_count=5,
    )
    neighbour = GraphEntity(
        workspace_id=workspace_id,
        name=f"{label} Neighbour",
        normalized_name=f"{label.lower()} neighbour",
        entity_type="concept",
        mention_count=3,
    )
    db.add_all([entity, neighbour])
    db.flush()
    ids["graph_entity"] = entity.id
    ids["graph_entity_neighbour"] = neighbour.id
    edge = GraphEdge(
        workspace_id=workspace_id,
        from_entity_id=entity.id,
        to_entity_id=neighbour.id,
        relation="mentions",
        weight=4,
    )
    db.add(edge)
    db.flush()
    ids["graph_edge"] = edge.id


def replant_graph(tenant: Tenant) -> None:
    """Re-plant a tenant's graph rows.

    ``POST /api/graph/rebuild`` deletes and rebuilds a workspace's entities and
    edges, and the route sweep calls it — so the graph tool tests plant their
    own rows immediately before they look at them rather than depending on
    whatever survived.
    """
    db = SessionLocal()
    try:
        _plant_graph(
            db,
            label=tenant.label,
            workspace_id=tenant.workspace_id,
            ids=tenant.ids,
        )
        db.commit()
    finally:
        db.close()


def _app():
    from app.main import app as fastapi_app

    return fastapi_app


def _agent_id(db, workspace_id: str) -> str:
    from sqlalchemy import select

    from app.models import Agent

    agent = db.scalar(select(Agent).where(Agent.workspace_id == workspace_id))
    assert agent is not None, "create_identity should have made an agent"
    return agent.id


# --------------------------------------------------------------------------
# The route table


@dataclass(frozen=True)
class RouteCase:
    """One request, aimed at another tenant's resource where one applies."""

    method: str
    template: str
    verdict: str
    # Path parameters, as {name: tenant id kind}. "app_slug" and
    # "oauth_state_value" are values rather than row ids, which is why the
    # tenant dict stores them.
    path_ids: Dict[str, str] = field(default_factory=dict)
    # Literal path parameters that name no resource, e.g. {"provider": "strava"}.
    path_literals: Dict[str, str] = field(default_factory=dict)
    query: Dict[str, Any] = field(default_factory=dict)
    # Query parameters filled with the *other* tenant's ids.
    query_ids: Dict[str, str] = field(default_factory=dict)
    # DENY only: the exact status the refusal must carry.
    expect: int = 404
    body: Optional[Dict[str, Any]] = None
    # Body keys to overwrite with the other tenant's ids, as {json path: kind}.
    body_ids: Dict[str, str] = field(default_factory=dict)
    files: bool = False
    note: str = ""

    def url(self, victim: Tenant) -> str:
        path = self.template
        for name, kind in self.path_ids.items():
            path = path.replace("{" + name + "}", victim.ids[kind])
        for name, literal in self.path_literals.items():
            path = path.replace("{" + name + "}", literal)
        assert "{" not in path, f"unfilled path parameter in {path}"
        return path

    def params(self, victim: Tenant) -> Dict[str, Any]:
        params = dict(self.query)
        for name, kind in self.query_ids.items():
            params[name] = victim.ids[kind]
        return params

    def json_body(self, victim: Tenant) -> Optional[Dict[str, Any]]:
        if self.body is None:
            return None
        payload = json.loads(json.dumps(self.body))
        for pointer, kind in self.body_ids.items():
            _assign(payload, pointer, victim.ids[kind])
        return payload

    @property
    def key(self) -> str:
        return f"{self.method} {self.template}"


def _assign(payload: Dict[str, Any], pointer: str, value: Any) -> None:
    """Set a dotted path, where a numeric part indexes a list."""
    parts = pointer.split(".")
    cursor: Any = payload
    for part in parts[:-1]:
        cursor = cursor[int(part)] if part.isdigit() else cursor[part]
    last = parts[-1]
    if last.isdigit():
        cursor[int(last)] = value
    else:
        cursor[last] = value


_QUERY = {"limit": 10}
_SPEC = {"visualization": "table", "query": _QUERY, "x_field": None, "y_fields": []}

ROUTE_CASES: List[RouteCase] = [
    # -- system ------------------------------------------------------------
    RouteCase("GET", "/health", PUBLIC),
    RouteCase("GET", "/api/bootstrap", SCOPED),
    # -- auth --------------------------------------------------------------
    RouteCase("GET", "/api/auth/me", SCOPED),
    RouteCase("GET", "/api/auth/workspaces", SCOPED),
    RouteCase(
        "POST",
        "/api/auth/workspaces",
        SCOPED,
        body={"name": "mine"},
        note=(
            "Names no resource, foreign or otherwise: it makes a new workspace "
            "for the caller and enrolls only them. The org it lands in is "
            "derived from the caller's own admin membership and never read off "
            "the body, so there is no id here to point at another tenant — see "
            "test_workspace_creation.test_the_org_is_never_taken_from_the_caller."
        ),
    ),
    RouteCase("POST", "/api/auth/logout", SCOPED),
    RouteCase("POST", "/api/auth/login", PUBLIC),
    RouteCase("POST", "/api/auth/signup", PUBLIC),
    RouteCase("POST", "/api/auth/verify-email", PUBLIC),
    RouteCase("POST", "/api/auth/password/reset/request", PUBLIC),
    RouteCase("POST", "/api/auth/password/reset/confirm", PUBLIC),
    RouteCase("GET", "/api/auth/google/start", PUBLIC),
    RouteCase("GET", "/api/auth/google/callback", PUBLIC),
    RouteCase("GET", "/api/auth/dev-override", PUBLIC),
    RouteCase("POST", "/api/auth/dev-login", PUBLIC),
    RouteCase("POST", "/api/auth/login-link/request", PUBLIC),
    RouteCase("POST", "/api/auth/login-link/consume", PUBLIC),
    RouteCase("GET", "/api/auth/playground", PUBLIC),
    RouteCase("POST", "/api/auth/playground", PUBLIC),
    # Reading an invitation is deliberately unauthenticated — the invitee may
    # have no account yet — so the token is the whole credential and PUBLIC is
    # the honest verdict, exactly as for the other token endpoints above.
    RouteCase("POST", "/api/auth/invites/preview", PUBLIC),
    # Accepting is the one route in the app that can put the caller inside
    # another workspace, and it names that workspace nowhere: the invitation
    # does. So the cross-tenant attack is not a foreign id in the path, it is
    # tenant B's token in the body — the case below is A holding B's link.
    #
    # 403, not 404: the token is real and A is allowed to know it failed for a
    # reason they can act on. What must not happen is a membership, and the
    # tamper digest over `memberships` is what proves none appeared.
    RouteCase(
        "POST",
        "/api/auth/invites/accept",
        DENY,
        expect=403,
        body={"token": "placeholder"},
        body_ids={"token": "workspace_invite_token"},
    ),
    # -- chat --------------------------------------------------------------
    RouteCase("GET", "/api/conversations", SCOPED),
    RouteCase("POST", "/api/conversations", SCOPED, body={"title": "mine"}),
    RouteCase(
        "DELETE",
        "/api/conversations/{conversation_id}",
        DENY,
        path_ids={"conversation_id": "conversation"},
    ),
    RouteCase(
        "GET",
        "/api/conversations/{conversation_id}/messages",
        DENY,
        path_ids={"conversation_id": "conversation"},
    ),
    RouteCase(
        "POST",
        "/api/conversations/{conversation_id}/messages",
        DENY,
        path_ids={"conversation_id": "conversation"},
        body={"content": "leak?", "agent_id": ""},
        body_ids={"agent_id": "agent"},
    ),
    RouteCase(
        "PUT",
        "/api/conversations/{conversation_id}/approval-mode",
        DENY,
        path_ids={"conversation_id": "conversation"},
        body={"mode": "auto_writes"},
        note="Turning off another tenant's approval park is the worst of these.",
    ),
    RouteCase(
        "POST",
        "/api/conversations/{conversation_id}/messages/{message_id}/edit",
        DENY,
        path_ids={"conversation_id": "conversation", "message_id": "message"},
        body={"content": "rewritten"},
        note="An edit is a truncation: another tenant's transcript must 404 "
        "on the workspace filter before anything is deleted.",
    ),
    RouteCase(
        "PATCH",
        "/api/conversations/{conversation_id}/defaults",
        DENY,
        path_ids={"conversation_id": "conversation"},
        body={"default_model": "smuggled-model"},
        note="Preferences, not policy — but still another tenant's row, so the "
        "workspace filter must 404 before anything is written.",
    ),
    RouteCase(
        "PUT",
        "/api/conversations/{conversation_id}/share",
        DENY,
        path_ids={"conversation_id": "conversation"},
        body={"shared": True},
        note="Sharing another workspace's thread must 404 on the workspace "
        "filter, before the creator/owner gate is even reached.",
    ),
    RouteCase(
        "PUT",
        "/api/conversations/{conversation_id}/title",
        DENY,
        path_ids={"conversation_id": "conversation"},
        body={"title": "renamed"},
    ),
    RouteCase(
        "POST",
        "/api/conversations/{conversation_id}/fork",
        DENY,
        path_ids={"conversation_id": "conversation"},
        body={"message_id": "placeholder", "title": "forked"},
        body_ids={"message_id": "message"},
        note="Forking another tenant's thread would copy its whole transcript "
        "into the caller's workspace; the visibility gate must refuse before "
        "the anchor message is even looked at.",
    ),
    RouteCase(
        "POST", "/api/runs/{run_id}/cancel", DENY, path_ids={"run_id": "run"}
    ),
    RouteCase(
        "POST",
        "/api/runs/{run_id}/steer",
        DENY,
        path_ids={"run_id": "run"},
        body={"content": "leaked steer"},
        note="Steering another workspace's run injects a prompt into it: 404.",
    ),
    RouteCase(
        "POST",
        "/api/runs/{run_id}/undo",
        DENY,
        path_ids={"run_id": "run"},
        note="Undoing another tenant's run would rewrite their documents and "
        "boards from checkpoints the caller cannot see; the workspace filter "
        "must 404 before the visibility gate is even reached.",
    ),
    RouteCase(
        "GET", "/api/runs/{run_id}/events", DENY, path_ids={"run_id": "run"}
    ),
    # The Inbox's attention feed: a pure workspace-scoped list with no id in
    # the request, so the sweep's job is proving tenant A's feed never carries
    # a row of tenant B's.
    RouteCase("GET", "/api/inbox", SCOPED),
    # The digest opt-in edits the caller's OWN membership row and names no
    # resource at all, so there is no foreign id to probe: SCOPED, with the
    # tamper digest proving the write never reaches tenant B's memberships.
    # enabled=false so the sweep leaves no standing mail subscription behind.
    RouteCase(
        "PUT", "/api/me/digest", SCOPED, body={"enabled": False, "hour_utc": 9}
    ),
    # Safe mode is the same shape as the digest above — the caller's own
    # membership row, no resource id, nothing for a foreign id to probe.
    # enabled=false so the sweep leaves every member it touches on the product
    # default rather than opted into asking.
    RouteCase("PUT", "/api/me/safe-mode", SCOPED, body={"enabled": False}),
    # Transcript search: a workspace-scoped list whose visibility chokepoint is
    # the same one the agent tool reads; the sweep proves tenant A's query
    # never quotes tenant B's words.
    RouteCase("GET", "/api/conversations/search", SCOPED, query={"q": "secret"}),
    # -- comments & notifications ------------------------------------------
    # The @-picker's member list: id-less, workspace-scoped, so the sweep's
    # job is proving A's picker never offers B's colleagues.
    RouteCase("GET", "/api/members", SCOPED),
    # The foreign id rides in the body: commenting on another tenant's
    # document would let the caller list the comment back — and a mention on
    # it would deep-link a stranger into the subject.
    RouteCase(
        "POST",
        "/api/comments",
        DENY,
        body={"subject_kind": "document", "subject_id": "", "body": "probe"},
        body_ids={"subject_id": "document"},
    ),
    RouteCase(
        "POST",
        "/api/comments",
        DENY,
        body={"subject_kind": "conversation", "subject_id": "", "body": "probe"},
        body_ids={"subject_id": "conversation"},
        note="conversation subjects pass resolve_visible, so a foreign "
        "thread must be indistinguishable from a missing one",
    ),
    # Listing rides the same subject resolution, so a foreign subject id in
    # the query is a 404 before any comment row is read.
    RouteCase(
        "GET",
        "/api/comments",
        DENY,
        query={"subject_kind": "document"},
        query_ids={"subject_id": "document"},
    ),
    RouteCase(
        "DELETE",
        "/api/comments/{comment_id}",
        DENY,
        path_ids={"comment_id": "comment"},
    ),
    RouteCase(
        "POST",
        "/api/notifications/{notification_id}/resolve",
        DENY,
        path_ids={"notification_id": "notification"},
        note="resolving another tenant's mention would silently empty their "
        "inbox; the workspace filter must answer before the target gate",
    ),
    # -- spaces ------------------------------------------------------------
    RouteCase("GET", "/api/spaces", SCOPED),
    RouteCase("POST", "/api/spaces", SCOPED, body={"name": "mine"}),
    RouteCase(
        "GET", "/api/spaces/{space_id}", DENY, path_ids={"space_id": "space"}
    ),
    RouteCase(
        "PATCH",
        "/api/spaces/{space_id}",
        DENY,
        path_ids={"space_id": "space"},
        body={"name": "probe"},
    ),
    RouteCase(
        "DELETE", "/api/spaces/{space_id}", DENY, path_ids={"space_id": "space"}
    ),
    # -- space templates ---------------------------------------------------
    RouteCase("GET", "/api/space-templates", SCOPED),
    # The foreign id rides in the body: snapshotting another tenant's space
    # would read its instructions into a row the caller can then list.
    RouteCase(
        "POST",
        "/api/space-templates",
        DENY,
        body={"name": "stolen", "from_space_id": ""},
        body_ids={"from_space_id": "space"},
        note="templates another tenant's space instructions",
    ),
    RouteCase(
        "DELETE",
        "/api/space-templates/{template_id}",
        DENY,
        path_ids={"template_id": "space_template"},
    ),
    RouteCase(
        "POST",
        "/api/space-templates/{template_id}/instantiate",
        DENY,
        path_ids={"template_id": "space_template"},
        body={"name": "probe"},
        note="builds a space from another tenant's template",
    ),
    # -- sources -----------------------------------------------------------
    RouteCase("GET", "/api/sources", SCOPED),
    RouteCase("POST", "/api/sources", SCOPED, files=True),
    RouteCase(
        "DELETE", "/api/sources/{source_id}", DENY, path_ids={"source_id": "source"}
    ),
    RouteCase(
        "GET",
        "/api/sources/{source_id}/content",
        DENY,
        path_ids={"source_id": "source"},
        note="the original bytes, so a leak here is the file itself and not an id",
    ),
    RouteCase("GET", "/api/chunks/{chunk_id}", DENY, path_ids={"chunk_id": "chunk"}),
    # -- agents ------------------------------------------------------------
    # An agent carries its workspace's instructions verbatim, so a cross-tenant
    # read is a prompt leak and a cross-tenant PATCH rewrites who the other
    # workspace's chat is. Both id routes 404 from the same `_load` filter; the
    # 409 "last enabled agent" guard sits behind it and so cannot be reached
    # across tenants — the refusal must be 404, never 409, or the status alone
    # would confirm the agent exists.
    RouteCase("GET", "/api/agents", SCOPED),
    RouteCase(
        "POST",
        "/api/agents",
        SCOPED,
        body={"name": "probe", "instructions": "probe"},
    ),
    RouteCase(
        "PATCH",
        "/api/agents/{agent_id}",
        DENY,
        path_ids={"agent_id": "agent"},
        body={"name": "leaked"},
    ),
    RouteCase(
        "DELETE",
        "/api/agents/{agent_id}",
        DENY,
        path_ids={"agent_id": "agent"},
    ),
    # -- tools -------------------------------------------------------------
    RouteCase("GET", "/api/tool-calls", SCOPED),
    RouteCase("GET", "/api/agent-tool-calls", SCOPED),
    RouteCase(
        "POST",
        "/api/tool-calls/{tool_call_id}/decision",
        DENY,
        path_ids={"tool_call_id": "tool_call"},
        body={"decision": "approved"},
    ),
    RouteCase(
        "POST",
        "/api/agent-tool-calls/{tool_call_id}/decision",
        DENY,
        path_ids={"tool_call_id": "agent_tool_call"},
        body={"decision": "approved", "remember": False},
    ),
    # Assignment routes an approval, and both ids in the request are probes: a
    # foreign call id must 404 before anything else is looked at, and a foreign
    # user id must never be confirmed (the org/members pattern). The sweep
    # sends both foreign at once; the own-call + foreign-user half is pinned in
    # test_assigned_approvals.py.
    RouteCase(
        "POST",
        "/api/agent-tool-calls/{tool_call_id}/assign",
        DENY,
        path_ids={"tool_call_id": "agent_tool_call"},
        body={"user_id": ""},
        body_ids={"user_id": "user"},
    ),
    RouteCase("GET", "/api/tool-policies", SCOPED),
    RouteCase(
        "PUT",
        "/api/tool-policies",
        SCOPED,
        body={"tool_name": "search_sources", "policy": "allow"},
    ),
    RouteCase(
        "DELETE",
        "/api/tool-policies/{tool_name}",
        DENY,
        path_literals={"tool_name": "search_sources"},
        # A tool name is a global string, not a tenant id, so the foreign thing
        # being aimed at is the *row*: both tenants hold (search_sources, chat)
        # and neither holds (search_sources, workflow). Asking for the workflow
        # one must 404 — a route that dropped either filter would instead find a
        # row and return 204, and the tamper digest would show B's grant gone.
        query={"scope": "workflow"},
    ),
    # -- agents ------------------------------------------------------------
    RouteCase("GET", "/api/agents", SCOPED),
    RouteCase(
        "GET",
        "/api/tools",
        SCOPED,
        note="the registry is built per workspace; the list must be the caller's",
    ),
    RouteCase(
        "POST",
        "/api/agents",
        SCOPED,
        body={"name": "mine", "instructions": "Answer plainly."},
    ),
    RouteCase(
        "PATCH",
        "/api/agents/{agent_id}",
        DENY,
        path_ids={"agent_id": "agent"},
        body={"name": "mine now"},
    ),
    RouteCase(
        "DELETE",
        "/api/agents/{agent_id}",
        DENY,
        path_ids={"agent_id": "agent"},
    ),
    # -- skills ------------------------------------------------------------
    # A skill carries a workspace's instructions verbatim, so a cross-tenant read
    # is a prompt leak and a cross-tenant PATCH rewrites what another workspace's
    # turn is told to do. Visibility is own-or-shared within one workspace; the
    # fixture skill is private, so every id route 404s on the workspace filter
    # regardless of the sharing gate behind it. Create/list take no foreign id.
    RouteCase("GET", "/api/skills", SCOPED),
    RouteCase(
        "POST",
        "/api/skills",
        SCOPED,
        body={"name": "mine-skill", "title": "Mine", "body": "do the thing"},
    ),
    RouteCase(
        "GET", "/api/skills/{skill_id}", DENY, path_ids={"skill_id": "skill"}
    ),
    RouteCase(
        "PATCH",
        "/api/skills/{skill_id}",
        DENY,
        path_ids={"skill_id": "skill"},
        body={"title": "leaked"},
    ),
    RouteCase(
        "DELETE", "/api/skills/{skill_id}", DENY, path_ids={"skill_id": "skill"}
    ),
    RouteCase(
        "GET",
        "/api/skills/{skill_id}/versions",
        DENY,
        path_ids={"skill_id": "skill"},
    ),
    RouteCase(
        "POST",
        "/api/skills/{skill_id}/versions/{version_id}/restore",
        DENY,
        path_ids={"skill_id": "skill", "version_id": "skill_version"},
    ),
    # SKILL.md interop. Import takes no foreign id — the sweep proves the
    # created row lands in the caller's workspace and echoes nothing of B's;
    # export names a skill id and must refuse a foreign one.
    RouteCase(
        "POST",
        "/api/skills/import",
        SCOPED,
        body={"markdown": "---\nname: sweep-import\ndescription: sweep\n---\nBody."},
    ),
    RouteCase(
        "GET",
        "/api/skills/{skill_id}/export",
        DENY,
        path_ids={"skill_id": "skill"},
    ),
    # -- marketplace --------------------------------------------------------
    # A listing's payload is instructions verbatim, so a cross-tenant detail
    # read is a prompt leak, and a cross-tenant install would copy another
    # workspace's prompt into the caller's. The fixture listing is
    # workspace-tier, so every id route 404s on the visibility chokepoint.
    RouteCase("GET", "/api/marketplace/listings", SCOPED),
    # Publishing names its source in the body: A pointing at B's skill must 404
    # on the skill lookup — a 2xx would republish another workspace's prompt
    # under A's name.
    RouteCase(
        "POST",
        "/api/marketplace/listings",
        DENY,
        body={"kind": "skill", "source_id": "", "slug": "isolation-probe"},
        body_ids={"source_id": "skill"},
    ),
    RouteCase(
        "GET",
        "/api/marketplace/listings/{listing_id}",
        DENY,
        path_ids={"listing_id": "listing"},
    ),
    RouteCase(
        "POST",
        "/api/marketplace/listings/{listing_id}/install",
        DENY,
        path_ids={"listing_id": "listing"},
    ),
    RouteCase(
        "PATCH",
        "/api/marketplace/listings/{listing_id}",
        DENY,
        path_ids={"listing_id": "listing"},
        body={"title": "renamed by A"},
        note="Delisting or renaming another workspace's listing must 404.",
    ),
    RouteCase(
        "POST",
        "/api/marketplace/listings/{listing_id}/update",
        DENY,
        path_ids={"listing_id": "listing"},
    ),
    RouteCase(
        "POST",
        "/api/marketplace/listings/{listing_id}/pin",
        DENY,
        path_ids={"listing_id": "listing"},
        body={"pinned": True},
    ),
    # The org-tier row: visible one ring wider than the workspace, and these
    # prove the widening stops at the organization boundary — B's org listing
    # is still a 404 to A, on read, install, and manage alike.
    RouteCase(
        "GET",
        "/api/marketplace/listings/{listing_id}",
        DENY,
        path_ids={"listing_id": "listing_org"},
    ),
    RouteCase(
        "POST",
        "/api/marketplace/listings/{listing_id}/install",
        DENY,
        path_ids={"listing_id": "listing_org"},
    ),
    RouteCase(
        "PATCH",
        "/api/marketplace/listings/{listing_id}",
        DENY,
        path_ids={"listing_id": "listing_org"},
        body={"status": "delisted"},
    ),
    RouteCase(
        "POST",
        "/api/marketplace/listings/{listing_id}/update",
        DENY,
        path_ids={"listing_id": "listing_org"},
    ),
    RouteCase(
        "POST",
        "/api/marketplace/listings/{listing_id}/pin",
        DENY,
        path_ids={"listing_id": "listing_org"},
        body={"pinned": True},
    ),
    # -- audit / graph / memory -------------------------------------------
    RouteCase("GET", "/api/audit-events", SCOPED),
    RouteCase("GET", "/api/graph", SCOPED),
    RouteCase("POST", "/api/graph/rebuild", SCOPED),
    RouteCase("GET", "/api/memory", SCOPED),
    RouteCase(
        "DELETE", "/api/memory/{memory_id}", DENY, path_ids={"memory_id": "memory"}
    ),
    # -- documents ---------------------------------------------------------
    RouteCase("GET", "/api/documents", SCOPED),
    RouteCase("POST", "/api/documents", SCOPED, body={"title": "mine", "content": "x"}),
    RouteCase("GET", "/api/documents-pending", SCOPED),
    RouteCase(
        "GET", "/api/documents/{document_id}", DENY, path_ids={"document_id": "document"}
    ),
    RouteCase(
        "PUT",
        "/api/documents/{document_id}",
        DENY,
        path_ids={"document_id": "document"},
        body={"content": "overwritten by A"},
    ),
    RouteCase(
        "DELETE",
        "/api/documents/{document_id}",
        DENY,
        path_ids={"document_id": "document"},
    ),
    RouteCase(
        "GET",
        "/api/documents/{document_id}/versions",
        DENY,
        path_ids={"document_id": "document"},
    ),
    RouteCase(
        "POST",
        "/api/documents/{document_id}/conversation",
        DENY,
        path_ids={"document_id": "document"},
    ),
    RouteCase(
        "POST",
        "/api/documents/{document_id}/versions/{version_id}/restore",
        DENY,
        path_ids={"document_id": "document", "version_id": "document_version"},
    ),
    # -- folders -----------------------------------------------------------
    RouteCase("GET", "/api/folders", SCOPED),
    # Naming another tenant's folder as the parent must refuse rather than
    # create an orphan under an id the caller cannot see.
    RouteCase(
        "POST",
        "/api/folders",
        SCOPED,
        body={"name": "mine", "parent_id": ""},
        body_ids={"parent_id": "folder"},
        note="a foreign parent id answers 422; the folder is never created",
    ),
    RouteCase(
        "PATCH",
        "/api/folders/{folder_id}",
        DENY,
        path_ids={"folder_id": "folder"},
        body={"name": "renamed by A"},
    ),
    RouteCase(
        "DELETE", "/api/folders/{folder_id}", DENY, path_ids={"folder_id": "folder"}
    ),
    RouteCase(
        "PUT",
        "/api/documents/{document_id}/folder",
        DENY,
        path_ids={"document_id": "document"},
        body={"folder_id": ""},
    ),
    # -- boards ------------------------------------------------------------
    RouteCase("GET", "/api/boards", SCOPED),
    RouteCase("POST", "/api/boards", SCOPED, body={"name": "mine", "columns": []}),
    RouteCase("DELETE", "/api/boards/{board_id}", DENY, path_ids={"board_id": "board"}),
    RouteCase(
        "POST",
        "/api/boards/{board_id}/cards",
        DENY,
        path_ids={"board_id": "board"},
        body={"column": "Todo", "title": "planted by A", "body": "", "labels": []},
    ),
    RouteCase(
        "POST",
        "/api/boards/{board_id}/cards/{card_id}/move",
        DENY,
        path_ids={"board_id": "board", "card_id": "board_card"},
        query={"column": "Done"},
    ),
    RouteCase(
        "DELETE",
        "/api/boards/{board_id}/cards/{card_id}",
        DENY,
        path_ids={"board_id": "board", "card_id": "board_card"},
    ),
    RouteCase(
        "POST",
        "/api/board-ops/{board_id}/columns",
        DENY,
        path_ids={"board_id": "board"},
        body={"name": "planted by A"},
    ),
    RouteCase(
        "PATCH",
        "/api/board-ops/{board_id}/columns/{column_id}",
        DENY,
        path_ids={"board_id": "board", "column_id": "board_column"},
        body={"name": "renamed by A"},
    ),
    RouteCase(
        "DELETE",
        "/api/board-ops/{board_id}/columns/{column_id}",
        DENY,
        path_ids={"board_id": "board", "column_id": "board_column"},
        query={"move_cards_to": "Done"},
    ),
    RouteCase(
        "POST",
        "/api/board-ops/{board_id}/columns/reorder",
        DENY,
        path_ids={"board_id": "board"},
        body={"order": ["", ""]},
        body_ids={"order.0": "board_column_other", "order.1": "board_column"},
    ),
    RouteCase(
        "POST",
        "/api/board-ops/{board_id}/cards/{card_id}/reorder",
        DENY,
        path_ids={"board_id": "board", "card_id": "board_card"},
        body={"index": 0, "column_id": ""},
        body_ids={"column_id": "board_column_other"},
    ),
    # -- todo lists ---------------------------------------------------------
    # The item routes take no board id — checking something off should not
    # require knowing which list it came from — so the workspace filter on the
    # item lookup is the only thing between an id and another tenant's list.
    RouteCase("GET", "/api/todos", SCOPED),
    RouteCase("POST", "/api/todos", SCOPED, body={"name": "mine"}),
    RouteCase(
        "POST",
        "/api/todos/{list_id}/items",
        DENY,
        path_ids={"list_id": "todo_list"},
        body={"title": "planted by A", "body": ""},
    ),
    RouteCase(
        "PATCH",
        "/api/todos/items/{item_id}",
        DENY,
        path_ids={"item_id": "todo_item"},
        body={"done": True},
    ),
    RouteCase(
        "DELETE",
        "/api/todos/items/{item_id}",
        DENY,
        path_ids={"item_id": "todo_item"},
    ),
    # -- live coworking ------------------------------------------------------
    # The claim routes resolve the card with the same workspace filter the todo
    # item routes use; the read surfaces are pure workspace-scoped state, so
    # the sweep's job is proving A's activity never carries a row of B's.
    RouteCase("GET", "/api/coworking/activity", SCOPED),
    # `once` bounds the stream to a single pass — the sweep has to terminate —
    # and one pass already carries the runs and presence snapshots the leak
    # scan wants to look inside.
    RouteCase("GET", "/api/coworking/stream", SCOPED, query={"once": "true"}),
    RouteCase(
        "POST",
        "/api/coworking/presence",
        SCOPED,
        body={"surface": "board:probe", "state": {"typing": True}},
    ),
    RouteCase(
        "DELETE", "/api/coworking/presence", SCOPED, query={"surface": "board:probe"}
    ),
    RouteCase(
        "POST",
        "/api/coworking/items/{item_id}/claim",
        DENY,
        path_ids={"item_id": "todo_item"},
    ),
    RouteCase(
        "POST",
        "/api/coworking/items/{item_id}/release",
        DENY,
        path_ids={"item_id": "todo_item"},
    ),
    # -- projects ----------------------------------------------------------
    RouteCase("GET", "/api/projects", SCOPED),
    RouteCase("POST", "/api/projects", SCOPED, body={"name": "mine", "kind": "web"}),
    RouteCase(
        "GET", "/api/projects/{project_id}", DENY, path_ids={"project_id": "project"}
    ),
    RouteCase(
        "DELETE", "/api/projects/{project_id}", DENY, path_ids={"project_id": "project"}
    ),
    RouteCase(
        "POST",
        "/api/projects/{project_id}/conversation",
        DENY,
        path_ids={"project_id": "project"},
    ),
    RouteCase(
        "PUT",
        "/api/projects/{project_id}/entry",
        DENY,
        path_ids={"project_id": "project"},
        body={"entry_path": "index.tsx"},
    ),
    RouteCase(
        "PUT",
        "/api/projects/{project_id}/files",
        DENY,
        path_ids={"project_id": "project"},
        body={"path": "planted.tsx", "content": "// by A"},
    ),
    RouteCase(
        "DELETE",
        "/api/projects/{project_id}/files",
        DENY,
        path_ids={"project_id": "project"},
        query={"path": "index.tsx"},
    ),
    # -- latex compile -----------------------------------------------------
    RouteCase(
        "POST",
        "/api/latex/compile",
        SCOPED,
        body={
            "engine": "pdftex",
            "entry_path": "main.tex",
            "files": [
                {
                    "path": "main.tex",
                    "content": "\\documentclass{article}\\begin{document}Hello\\end{document}",
                }
            ],
        },
    ),
    # -- bibliography ------------------------------------------------------
    RouteCase(
        "GET",
        "/api/bibliography/{project_id}/entries",
        DENY,
        path_ids={"project_id": "project"},
    ),
    RouteCase(
        "GET",
        "/api/bibliography/{project_id}/validate",
        DENY,
        path_ids={"project_id": "project"},
    ),
    RouteCase(
        "POST",
        "/api/bibliography/{project_id}/entries",
        DENY,
        path_ids={"project_id": "project"},
        body={"entry_type": "misc", "key": "planted", "fields": {"title": "by A"}},
    ),
    # -- analytics ---------------------------------------------------------
    RouteCase("GET", "/api/datasets", SCOPED),
    RouteCase(
        "POST",
        "/api/datasets",
        DENY,
        body={"name": "stolen", "description": "", "source_id": ""},
        body_ids={"source_id": "source"},
        note="creates a dataset from another tenant's source",
    ),
    RouteCase(
        "POST",
        "/api/datasets/{dataset_id}/query",
        DENY,
        path_ids={"dataset_id": "dataset"},
        body=dict(_QUERY),
    ),
    RouteCase(
        "POST",
        "/api/datasets/{dataset_id}/versions",
        DENY,
        path_ids={"dataset_id": "dataset"},
        body={"source_id": ""},
        body_ids={"source_id": "source"},
    ),
    RouteCase("GET", "/api/dashboards", SCOPED),
    RouteCase(
        "POST",
        "/api/dashboards",
        DENY,
        body={"name": "stolen", "description": "", "dataset_id": "", "spec": _SPEC},
        body_ids={"dataset_id": "dataset"},
        note="builds a dashboard over another tenant's dataset",
    ),
    RouteCase(
        "POST",
        "/api/dashboards/{dashboard_id}/run",
        DENY,
        path_ids={"dashboard_id": "dashboard"},
    ),
    RouteCase(
        "POST",
        "/api/dashboards/{dashboard_id}/duplicate",
        DENY,
        path_ids={"dashboard_id": "dashboard"},
        note="copies another tenant's dashboard into the caller's workspace",
    ),
    RouteCase(
        "DELETE",
        "/api/dashboards/{dashboard_id}",
        DENY,
        path_ids={"dashboard_id": "dashboard"},
    ),
    RouteCase(
        "POST",
        "/api/dashboards/{dashboard_id}/conversation",
        DENY,
        path_ids={"dashboard_id": "dashboard"},
    ),
    RouteCase("GET", "/api/dashboard-templates", SCOPED),
    RouteCase(
        "POST",
        "/api/dashboard-templates",
        SCOPED,
        body={
            "name": "mine",
            "description": "",
            "required_columns": [{"name": "region", "type": "string"}],
            "spec": {
                "visualization": "table",
                "query": {"group_by": "region", "limit": 10},
                "x_field": "region",
                "y_fields": ["count"],
            },
        },
        note="a definition names no dataset, so there is no foreign id to plant",
    ),
    RouteCase(
        "DELETE",
        "/api/dashboard-templates/{template_id}",
        DENY,
        path_ids={"template_id": "dashboard_template"},
    ),
    # The template is the foreign resource here and the dataset is the caller's
    # own, so a 404 can only mean "that template is not yours" — which is what
    # makes this case bite. Reversing it would be answered by the dataset lookup
    # and prove nothing about templates.
    RouteCase(
        "POST",
        "/api/dashboard-templates/{template_id}/bind",
        DENY,
        path_ids={"template_id": "dashboard_template"},
        body={"name": "stolen", "description": "", "dataset_id": "", "column_bindings": {}},
        body_ids={"dataset_id": "dataset"},
        note="binds another tenant's definition",
    ),
    # -- dashboard pins (per user, not per workspace) -----------------------
    RouteCase("GET", "/api/dashboard-pins", SCOPED),
    RouteCase(
        "PUT",
        "/api/dashboards/{dashboard_id}/pin",
        DENY,
        path_ids={"dashboard_id": "dashboard"},
        body={"grid_x": 0, "grid_y": 0, "grid_w": 6, "grid_h": 4},
    ),
    RouteCase(
        "DELETE",
        "/api/dashboards/{dashboard_id}/pin",
        DENY,
        path_ids={"dashboard_id": "dashboard"},
    ),
    RouteCase(
        "PUT",
        "/api/dashboard-pins/layout",
        DENY,
        body={
            "tiles": [
                {"dashboard_id": "", "grid_x": 0, "grid_y": 0, "grid_w": 6, "grid_h": 4}
            ]
        },
        body_ids={"tiles.0.dashboard_id": "dashboard"},
        note="moves a tile on another tenant's home screen",
    ),
    # -- favorites (per user, not per workspace) ----------------------------
    RouteCase("GET", "/api/favorites", SCOPED),
    RouteCase(
        "PUT",
        "/api/favorites/{kind}/{target_id}",
        DENY,
        path_literals={"kind": "conversation"},
        path_ids={"target_id": "conversation"},
        note="the subtle one: the PUT proves visibility BEFORE storing, so a "
        "foreign id must 404 with no row written. `conversation` is the kind "
        "under test because it has the extra personal/shared axis — the "
        "harness's id maps to tenant B's own rail thread, unshared, and "
        "resolve_visible's never-removed workspace filter refuses it before "
        "the shared/personal question is even asked",
    ),
    RouteCase(
        "DELETE",
        "/api/favorites/{kind}/{target_id}",
        DENY,
        path_literals={"kind": "conversation"},
        path_ids={"target_id": "conversation"},
        note="'Not a favorite': foreign, missing and never-favorited are the "
        "same 404 from this side of the workspace wall",
    ),
    RouteCase(
        "PUT",
        "/api/favorites/order",
        DENY,
        body={"entries": [{"kind": "conversation", "target_id": "", "ordinal": 0}]},
        body_ids={"entries.0.target_id": "conversation"},
        note="reorders an entry the caller never favorited",
    ),
    # -- share links --------------------------------------------------------
    RouteCase("GET", "/api/share-links", SCOPED),
    RouteCase(
        "POST",
        "/api/share-links",
        DENY,
        body={"resource_kind": "dashboard", "resource_id": ""},
        body_ids={"resource_id": "dashboard"},
        note="mints a public link onto another tenant's dashboard",
    ),
    RouteCase(
        "POST",
        "/api/share-links/{link_id}/revoke",
        DENY,
        path_ids={"link_id": "share_link"},
    ),
    # The token is the whole credential, so this is deliberately open; the
    # revoked/expired/foreign fail-closed 404s are pinned in the targeted
    # tests (test_share_links.py), per the PUBLIC docstring above.
    RouteCase(
        "GET",
        "/shared/{token}",
        PUBLIC,
        path_ids={"token": "share_link_token"},
    ),
    # -- dashboard subscriptions -------------------------------------------
    # The cron posture again: create names a *dashboard* in the body, and
    # planting another tenant's dashboard id must 404 before any validation
    # detail could confirm it — a subscription is a standing mail of that
    # dashboard's data, so subscribing to foreign data is the whole attack.
    RouteCase("GET", "/api/dashboard-subscriptions", SCOPED),
    RouteCase(
        "POST",
        "/api/dashboard-subscriptions",
        DENY,
        body={
            "dashboard_id": "",
            "schedule_cron": "0 9 * * *",
            "schedule_timezone": "UTC",
        },
        body_ids={"dashboard_id": "dashboard"},
        note="stands up recurring mail of another tenant's dashboard",
    ),
    RouteCase(
        "DELETE",
        "/api/dashboard-subscriptions/{subscription_id}",
        DENY,
        path_ids={"subscription_id": "dashboard_subscription"},
        note="silences (and confirms) another tenant's scheduled mail",
    ),
    # -- API tokens ---------------------------------------------------------
    # The list and the mint are id-less (SCOPED); the revoke names a token, and
    # revoking another tenant's credential must confirm nothing. The raw secret
    # is planted as an id kind, so the leak grep also proves the list never
    # echoes a token back.
    RouteCase("GET", "/api/api-tokens", SCOPED),
    RouteCase("POST", "/api/api-tokens", SCOPED, body={"name": "isolation probe"}),
    RouteCase(
        "DELETE",
        "/api/api-tokens/{token_id}",
        DENY,
        path_ids={"token_id": "api_token"},
        note="kills (and confirms) another tenant's machine credential",
    ),
    # -- token-authed hooks -------------------------------------------------
    # These two authenticate with `Authorization: Bearer <api token>`, never a
    # cookie, so the sweep's cookie-authenticated client is refused at the door
    # — 401, before the path ids are even read. That the *right* token reaches
    # only its own workspace is pinned by the targeted tests in
    # test_api_tokens.py (the sweep cannot exercise bearer auth).
    RouteCase(
        "POST",
        "/api/hooks/workflows/{workflow_id}/trigger",
        DENY,
        expect=401,
        path_ids={"workflow_id": "workflow"},
        body={"payload": {}},
        note="cookie-only caller holds no bearer token",
    ),
    RouteCase(
        "POST",
        "/api/hooks/conversations/{conversation_id}/messages",
        DENY,
        expect=401,
        path_ids={"conversation_id": "conversation"},
        body={"content": "external note"},
        note="cookie-only caller holds no bearer token",
    ),
    # -- inbound email ------------------------------------------------------
    # The provider webhook is PUBLIC on the tick's posture: with no
    # `inbound_email_webhook_secret` configured (the suite's default) it is a
    # deliberate 503, and the full open-door behaviour — wrong secret 401,
    # unknown token writes nothing, foreign probes cannot land rows — is
    # pinned in test_inbound_email.py. The management routes are the api-token
    # posture: mint names a *space* in the body, and planting another tenant's
    # space id must 404 before the domain check could say anything else.
    RouteCase(
        "POST",
        "/api/hooks/email/inbound",
        PUBLIC,
        body={
            "recipient": "inbox+not-a-real-token-at-all@mail.grain.test",
            "sender": "probe@example.com",
            "subject": "isolation probe",
            "text": "probe body",
            "message_id": "<isolation-probe@example.com>",
        },
    ),
    RouteCase("GET", "/api/inbound-addresses", SCOPED),
    RouteCase(
        "POST",
        "/api/inbound-addresses",
        DENY,
        body={"label": "isolation probe", "target_space_id": ""},
        body_ids={"target_space_id": "space"},
        note="files another tenant's space as a mail target",
    ),
    RouteCase(
        "POST",
        "/api/inbound-addresses/{address_id}/revoke",
        DENY,
        path_ids={"address_id": "inbound_address"},
        note="silences (and confirms) another tenant's mail-in address",
    ),
    # -- outbound webhooks --------------------------------------------------
    # Create takes no foreign id (the URL is the owner's own choice — a DNS
    # failure in a hermetic run answers 422, never 500); update/delete name an
    # endpoint and must 404 on another tenant's, or one workspace could
    # redirect (or silence) another's event stream.
    RouteCase("GET", "/api/webhooks", SCOPED),
    RouteCase(
        "POST",
        "/api/webhooks",
        SCOPED,
        body={
            "name": "isolation probe",
            "url": "https://api.github.com/probe-sink",
            "events": ["run.completed"],
        },
    ),
    RouteCase("GET", "/api/webhooks/deliveries", SCOPED),
    RouteCase(
        "POST",
        "/api/webhooks/deliveries/{delivery_id}/redeliver",
        DENY,
        path_ids={"delivery_id": "webhook_delivery"},
        note="requeues (and confirms) another tenant's delivery row",
    ),
    RouteCase(
        "PUT",
        "/api/webhooks/{endpoint_id}",
        DENY,
        path_ids={"endpoint_id": "webhook_endpoint"},
        body={"enabled": False},
        note="redirects or silences another tenant's event stream",
    ),
    RouteCase(
        "DELETE",
        "/api/webhooks/{endpoint_id}",
        DENY,
        path_ids={"endpoint_id": "webhook_endpoint"},
    ),
    # -- database connections ---------------------------------------------
    RouteCase("GET", "/api/db/connections", SCOPED),
    RouteCase(
        "POST",
        "/api/db/connections",
        SCOPED,
        body={
            "name": "mine",
            "engine": "sqlite",
            "dsn": "mine.db",
            "read_only": True,
        },
    ),
    RouteCase(
        "GET",
        "/api/db/connections/{connection_id}/schema",
        DENY,
        path_ids={"connection_id": "db_connection"},
    ),
    RouteCase(
        "POST",
        "/api/db/connections/{connection_id}/test",
        DENY,
        path_ids={"connection_id": "db_connection"},
    ),
    RouteCase(
        "DELETE",
        "/api/db/connections/{connection_id}",
        DENY,
        path_ids={"connection_id": "db_connection"},
    ),
    # -- MCP ---------------------------------------------------------------
    RouteCase("GET", "/api/mcp/servers", SCOPED),
    RouteCase(
        "POST",
        "/api/mcp/servers",
        SCOPED,
        body={
            "name": "mine-server",
            "transport": "stdio",
            "command": "/bin/true",
            "args": [],
            "url": "",
            "secrets": {},
        },
    ),
    RouteCase(
        "PATCH",
        "/api/mcp/servers/{server_id}",
        DENY,
        path_ids={"server_id": "mcp_server"},
        query={"enabled": "false"},
    ),
    RouteCase(
        "POST",
        "/api/mcp/servers/{server_id}/refresh",
        DENY,
        path_ids={"server_id": "mcp_server"},
    ),
    RouteCase(
        "DELETE",
        "/api/mcp/servers/{server_id}",
        DENY,
        path_ids={"server_id": "mcp_server"},
    ),
    RouteCase(
        "PATCH",
        "/api/mcp/tools/{tool_id}",
        DENY,
        path_ids={"tool_id": "mcp_tool"},
        query={"enabled": "false"},
    ),
    # OAuth tokens are held per user, not per workspace, so these three are the
    # routes where a leak would hand over a credential for someone's third-party
    # account rather than a row of workspace data. /connect is the only route in
    # the app that dials the network, and the 404 has to land before it does.
    RouteCase(
        "GET",
        "/api/mcp/servers/{server_id}/auth",
        DENY,
        path_ids={"server_id": "mcp_server"},
    ),
    RouteCase(
        "POST",
        "/api/mcp/servers/{server_id}/connect",
        DENY,
        path_ids={"server_id": "mcp_server"},
    ),
    RouteCase(
        "POST",
        "/api/mcp/servers/{server_id}/disconnect",
        DENY,
        path_ids={"server_id": "mcp_server"},
    ),
    # Unauthenticated by design: the single-use state row is the credential, and
    # it carries the user id itself. See api/mcp.py:oauth_callback.
    RouteCase("GET", "/api/mcp/oauth/callback", PUBLIC),
    # -- sandbox -----------------------------------------------------------
    # Sessions are the one resource where "not yours" would be worth money to
    # confirm: a session id names a live machine with someone's files already
    # loaded into it. Every route that takes one is therefore a DENY.
    RouteCase("GET", "/api/sandbox", SCOPED),
    RouteCase("POST", "/api/sandbox", SCOPED, body={"project_id": "", "label": ""}),
    RouteCase(
        "GET",
        "/api/sandbox/{session_id}/executions",
        DENY,
        path_ids={"session_id": "sandbox_session"},
    ),
    RouteCase(
        "POST",
        "/api/sandbox/{session_id}/run",
        DENY,
        path_ids={"session_id": "sandbox_session"},
        body={"source": "print('stolen')", "kind": "code"},
    ),
    RouteCase(
        "POST",
        "/api/sandbox/{session_id}/pause",
        DENY,
        path_ids={"session_id": "sandbox_session"},
    ),
    RouteCase(
        "DELETE",
        "/api/sandbox/{session_id}",
        DENY,
        path_ids={"session_id": "sandbox_session"},
    ),
    # A custom sandbox tool is workspace data: listing another tenant's tools
    # would leak their names, descriptions, and egress, and naming one by id to
    # edit or delete it is a cross-tenant write. List/create are SCOPED; every
    # id-taking route is a DENY.
    RouteCase("GET", "/api/sandbox-tools", SCOPED),
    RouteCase(
        "POST",
        "/api/sandbox-tools",
        SCOPED,
        body={"name": "mine-probe", "argv": ["echo", "hi"]},
    ),
    RouteCase(
        "PATCH",
        "/api/sandbox-tools/{tool_id}",
        DENY,
        path_ids={"tool_id": "sandbox_tool"},
        body={"enabled": False},
    ),
    RouteCase(
        "DELETE",
        "/api/sandbox-tools/{tool_id}",
        DENY,
        path_ids={"tool_id": "sandbox_tool"},
    ),
    # A workspace secret is a shared credential the whole workspace's sandbox
    # code can read. Listing is member-visible but scoped, so a foreign
    # workspace's names never appear; setting is owner-only and lands in the
    # caller's own workspace; deleting names a secret and, scoped to the caller,
    # cannot reach across to another tenant's — a miss there is a plain 404.
    RouteCase("GET", "/api/sandbox/secrets", SCOPED),
    RouteCase(
        "PUT",
        "/api/sandbox/secrets",
        SCOPED,
        body={"name": "ISOLATION_PROBE", "value": "x"},
    ),
    RouteCase(
        "DELETE",
        "/api/sandbox/secrets/{name}",
        DENY,
        path_ids={"name": "sandbox_secret"},
    ),
    # -- workflows ---------------------------------------------------------
    # A workflow id is worth more than most: naming another tenant's automation
    # would let you read the graph (which names their tools and datasets), run it
    # unattended, or delete it. Every id-taking route is a DENY, and the run
    # detail is a DENY of its own because a workflow_run id reaches the node
    # table without passing through the workflow.
    RouteCase(
        "POST",
        "/api/workflows/compile",
        SCOPED,
        body={"source_prompt": "search the workspace and summarise it"},
    ),
    RouteCase("GET", "/api/workflows", SCOPED),
    RouteCase(
        "POST",
        "/api/workflows",
        SCOPED,
        body={"source_prompt": "search the workspace and summarise it"},
    ),
    RouteCase(
        "GET", "/api/workflows/{workflow_id}", DENY, path_ids={"workflow_id": "workflow"}
    ),
    RouteCase(
        "PATCH",
        "/api/workflows/{workflow_id}",
        DENY,
        path_ids={"workflow_id": "workflow"},
        body={"status": "active"},
    ),
    RouteCase(
        "DELETE",
        "/api/workflows/{workflow_id}",
        DENY,
        path_ids={"workflow_id": "workflow"},
    ),
    RouteCase(
        "POST",
        "/api/workflows/{workflow_id}/run",
        DENY,
        path_ids={"workflow_id": "workflow"},
        body={"payload": {}},
    ),
    RouteCase(
        "GET",
        "/api/workflows/{workflow_id}/runs",
        DENY,
        path_ids={"workflow_id": "workflow"},
    ),
    RouteCase(
        "GET",
        "/api/workflows/runs/{workflow_run_id}",
        DENY,
        path_ids={"workflow_run_id": "workflow_run"},
    ),
    # Unauthenticated by design: an external cron has no session. It takes no
    # arguments at all — no workflow id, no timestamp — so there is no foreign
    # resource to point it at, and with WORKFLOW_CRON_SECRET unset (as it is
    # here) it refuses outright. See api/workflows.py:tick.
    RouteCase("POST", "/api/workflows/tick", PUBLIC),
    # -- workflow templates ------------------------------------------------
    RouteCase("GET", "/api/workflow-templates", SCOPED),
    # The foreign id rides in the body: snapshotting another tenant's workflow
    # would copy its graph — tool names, prompts, arguments — into a row the
    # caller can then read at leisure.
    RouteCase(
        "POST",
        "/api/workflow-templates",
        DENY,
        body={"name": "stolen", "from_workflow_id": ""},
        body_ids={"from_workflow_id": "workflow"},
        note="templates another tenant's automation",
    ),
    RouteCase(
        "DELETE",
        "/api/workflow-templates/{template_id}",
        DENY,
        path_ids={"template_id": "workflow_template"},
    ),
    RouteCase(
        "POST",
        "/api/workflow-templates/{template_id}/instantiate",
        DENY,
        path_ids={"template_id": "workflow_template"},
        body={"name": ""},
        note="copies another tenant's automation into a runnable workflow",
    ),
    # -- crons -------------------------------------------------------------
    # A cron id is worth as much as a workflow id: naming another tenant's
    # automation would let you read its prompt, fire it unattended, or delete it.
    # Every id-taking route is a DENY; `run-now`'s DENY is what proves a foreign
    # cron cannot be fired. Dispatch has no route of its own — the shared
    # /api/workflows/tick claims and enqueues due crons.
    RouteCase("GET", "/api/crons", SCOPED),
    # Same classification as POST /api/workflows/compile: it stores nothing and
    # names no resource, but it spends model budget on the caller's ledger, so
    # it still requires an authenticated actor — SCOPED, not PUBLIC.
    RouteCase(
        "POST",
        "/api/crons/compile-schedule",
        SCOPED,
        body={"text": "every weekday at 9am"},
    ),
    RouteCase(
        "POST",
        "/api/crons",
        SCOPED,
        body={
            "name": "mine",
            "kind": "task",
            "schedule_cron": "0 9 * * *",
            "schedule_timezone": "UTC",
            "prompt": "do it",
        },
    ),
    RouteCase("GET", "/api/crons/{cron_id}", DENY, path_ids={"cron_id": "cron"}),
    RouteCase(
        "PATCH",
        "/api/crons/{cron_id}",
        DENY,
        path_ids={"cron_id": "cron"},
        body={"enabled": False},
    ),
    RouteCase("DELETE", "/api/crons/{cron_id}", DENY, path_ids={"cron_id": "cron"}),
    RouteCase(
        "POST", "/api/crons/{cron_id}/run-now", DENY, path_ids={"cron_id": "cron"}
    ),
    # -- monitors ----------------------------------------------------------
    # The cron posture, plus one sharper case: create names a *dataset* in the
    # body, and planting another tenant's dataset id must 404 before any
    # validation detail could confirm the id — a monitor is a standing read of
    # that dataset, so building one over foreign data is the whole attack.
    RouteCase("GET", "/api/monitors", SCOPED),
    RouteCase(
        "POST",
        "/api/monitors",
        DENY,
        body={
            "name": "stolen watch",
            "dataset_id": "",
            "query": {"metrics": [{"operation": "count", "label": "rows"}]},
            "comparator": "gt",
            "threshold": 0,
            "schedule_cron": "0 9 * * *",
            "schedule_timezone": "UTC",
        },
        body_ids={"dataset_id": "dataset"},
        note="builds a standing read over another tenant's dataset",
    ),
    RouteCase(
        "PUT",
        "/api/monitors/{monitor_id}",
        DENY,
        path_ids={"monitor_id": "monitor"},
        body={"enabled": False},
    ),
    RouteCase(
        "DELETE", "/api/monitors/{monitor_id}", DENY, path_ids={"monitor_id": "monitor"}
    ),
    # The sharp one: a foreign monitor cannot be evaluated — an evaluation
    # reads the dataset behind it.
    RouteCase(
        "POST",
        "/api/monitors/{monitor_id}/run-now",
        DENY,
        path_ids={"monitor_id": "monitor"},
    ),
    # -- integrations ------------------------------------------------------
    RouteCase("GET", "/api/integrations", SCOPED),
    RouteCase(
        "POST",
        "/api/integrations/{provider}/connect",
        SCOPED,
        path_literals={"provider": "strava"},
    ),
    RouteCase(
        "POST",
        "/api/integrations/garmin/credentials",
        SCOPED,
        body={"email": "a@example.com", "password": "irrelevant"},
    ),
    RouteCase(
        "GET",
        "/api/integrations/{provider}/callback",
        PUBLIC,
        path_literals={"provider": "strava"},
        note="state-bound; covered by the targeted OAuth-state test",
    ),
    RouteCase(
        "DELETE",
        "/api/integrations/{account_id}",
        DENY,
        path_ids={"account_id": "integration_account"},
    ),
    RouteCase(
        "POST",
        "/api/integrations/{account_id}/sync",
        DENY,
        path_ids={"account_id": "integration_account"},
    ),
    RouteCase(
        "GET",
        "/api/integrations/{account_id}/jobs",
        DENY,
        path_ids={"account_id": "integration_account"},
    ),
    # -- generated apps ----------------------------------------------------
    RouteCase("GET", "/api/apps", SCOPED),
    RouteCase(
        "POST",
        "/api/apps",
        DENY,
        body={
            "name": "stolen",
            "slug": "stolen-app",
            "description": "",
            "visibility": "private",
            "app_type": "dashboard",
            "dashboard_ids": [""],
        },
        body_ids={"dashboard_ids.0": "dashboard"},
        # A dashboard id in the *body* is a validation failure, not a missing
        # route target — the app being created does not exist yet.
        expect=422,
        note="bundles another tenant's dashboard into a new app",
    ),
    RouteCase(
        "POST",
        "/api/apps/{app_id}/generate",
        DENY,
        path_ids={"app_id": "app"},
        body={"prompt": "leak it", "dataset_ids": [""]},
        body_ids={"dataset_ids.0": "dataset"},
    ),
    RouteCase(
        "POST",
        "/api/apps/{app_id}/releases",
        DENY,
        path_ids={"app_id": "app"},
        body={"dashboard_ids": [""]},
        body_ids={"dashboard_ids.0": "dashboard"},
    ),
    RouteCase(
        "GET",
        "/api/apps/{app_id}/releases/{release_id}/frame",
        DENY,
        path_ids={"app_id": "app", "release_id": "app_release"},
    ),
    RouteCase(
        "POST",
        "/api/apps/{app_id}/releases/{release_id}/publish",
        DENY,
        path_ids={"app_id": "app", "release_id": "app_release"},
    ),
    RouteCase(
        "POST",
        "/api/apps/{app_id}/rollback/{release_id}",
        DENY,
        path_ids={"app_id": "app", "release_id": "app_release"},
    ),
    RouteCase(
        "GET",
        "/published/apps/{slug}",
        PUBLIC,
        path_ids={"slug": "app_slug"},
        note="private app must 404 here; asserted in the targeted test",
    ),
    RouteCase(
        "GET",
        "/published/apps/{slug}/frame",
        PUBLIC,
        path_ids={"slug": "app_slug"},
        note="private app must 404 here; asserted in the targeted test",
    ),
    # -- admin -------------------------------------------------------------
    # Owner-only reads over the caller's own workspace. Both tenants here are
    # owners of their own workspace, so these exercise the scope filter rather
    # than the role gate — the 403 for a plain member is pinned in
    # tests/test_admin.py, which is also where the no-secrets assertions live.
    RouteCase("GET", "/api/admin/members", SCOPED),
    # Membership and invitations: the routes that decide who is in a workspace
    # and what they may do, which makes a missing filter here worse than
    # anywhere else in the file — it is not a read of another tenant's data, it
    # is a *write* to their access control. All three name an id, so all three
    # are DENY 404: a member id or an invite id from another workspace must be
    # indistinguishable from one that does not exist. `test_sweep_did_not_touch
    # _the_victim` digests `memberships` and `workspace_invites` along with
    # every other workspace-scoped table, so a route that refused in its
    # response but demoted somebody on the way there is caught as well.
    RouteCase(
        "PATCH",
        "/api/admin/members/{membership_id}",
        DENY,
        path_ids={"membership_id": "membership"},
        body={"role": "member"},
    ),
    RouteCase(
        "DELETE",
        "/api/admin/members/{membership_id}",
        DENY,
        path_ids={"membership_id": "membership"},
    ),
    RouteCase("GET", "/api/admin/invites", SCOPED),
    # Creating one takes no foreign id — the workspace is `actor.workspace_id`
    # and nothing else — so the only way it could cross tenants is by writing
    # the invitation somewhere it was not asked to, which the tamper digest
    # catches. The address is a literal so the case does not depend on B.
    RouteCase(
        "POST",
        "/api/admin/invites",
        SCOPED,
        body={"email": "isolation-probe@example.com", "role": "member"},
    ),
    RouteCase(
        "DELETE",
        "/api/admin/invites/{invite_id}",
        DENY,
        path_ids={"invite_id": "workspace_invite"},
    ),
    RouteCase("GET", "/api/admin/audit-events", SCOPED),
    # The SIEM export walks the same table forward on a keyset cursor; like
    # /usage it takes no foreign id, so the leak scan over the victim's ids and
    # markers is what proves its filter holds.
    RouteCase("GET", "/api/admin/audit-events/export", SCOPED),
    # -- API tokens (owner-gated bearer credentials for the MCP surface) -----
    RouteCase("GET", "/api/api-tokens", SCOPED),
    RouteCase("POST", "/api/api-tokens", SCOPED, body={"name": "sweep token"}),
    RouteCase(
        "DELETE",
        "/api/api-tokens/{token_id}",
        DENY,
        path_ids={"token_id": "api_token"},
    ),
    # The MCP server surface authenticates by BEARER, not the cookie the sweep
    # carries, so a cookie-only caller is refused 401 before any tenant lookup.
    # PUBLIC (bearer-gated, not cookie-scoped): the 500 guard and the fact that
    # a wrong-credential caller sees nothing are the whole contract here; its
    # per-workspace scoping is proved by test_mcp_server.py.
    RouteCase("POST", "/api/mcp", PUBLIC),
    RouteCase("GET", "/api/admin/activity", SCOPED),
    RouteCase("GET", "/api/admin/storage", SCOPED),
    RouteCase("GET", "/api/admin/mcp-servers", SCOPED),
    RouteCase("GET", "/api/admin/sandbox-sessions", SCOPED),
    # Token and cost accounting. SCOPED rather than DENY because it takes no id
    # at all — only a window — so the only way it could cross tenants is by
    # aggregating rows it should not see, which is exactly what the leak scan
    # over every id and marker string catches.
    RouteCase("GET", "/api/admin/usage", SCOPED),
    # The per-agent scorecard. Like /usage it takes no id, only a window; the
    # leak scan over the victim's agent id and marker strings proves its five
    # GROUP BYs aggregate only the caller's rows.
    RouteCase("GET", "/api/admin/agents", SCOPED),
    # Latency/throughput/error/liveness/retention rollups. Like /usage it takes
    # no id, only a window, so the only cross-tenant risk is aggregating rows it
    # should not see — the leak scan over the victim's run/conversation ids and
    # marker strings is what proves it does not.
    RouteCase("GET", "/api/admin/observability", SCOPED),
    # The spend ceiling. Both take no id: the ceiling they read and write is the
    # caller's own workspace's, named by `actor.workspace_id` and by nothing in
    # the request. The PUT is the one that matters here — it lists and *releases*
    # runs parked on budget, so a missing workspace filter would show one tenant
    # another's parked runs and resume them. The leak scan over the victim's run
    # ids is what would catch that.
    RouteCase("GET", "/api/admin/budget", SCOPED),
    RouteCase(
        "PUT",
        "/api/admin/budget",
        SCOPED,
        body={"window_hours": 24, "usd_per_window": 100.0},
    ),
    # The one admin route that takes an id, and it names a live machine — same
    # verdict as DELETE /api/sandbox/{session_id} for the same reason.
    RouteCase(
        "DELETE",
        "/api/admin/sandbox-sessions/{session_id}",
        DENY,
        path_ids={"session_id": "sandbox_session"},
    ),
    # -- org ---------------------------------------------------------------
    # The tier above the workspace, and the reason these cases matter more than
    # their SCOPED verdicts suggest: an org row governs *other people's*
    # workspaces, so a missing filter here is not one tenant reading another's
    # data, it is one tenant setting the security posture the other runs under.
    #
    # No route in this group accepts an organization id. The org is always
    # `actor.organization_id`, derived from the workspace the caller's own
    # membership already proved, so there is no foreign id to plant in a path —
    # which is why these are SCOPED rather than DENY. What the sweep proves is
    # that the derivation never lands on B's org: `assert_no_leak` scans every
    # response for B's ids, `ids["organization"]` and `ids["org_tool_policy"]`
    # are among them, and `test_sweep_did_not_touch_the_victim` digests B's
    # organization rows alongside their workspace ones.
    RouteCase("GET", "/api/org", SCOPED),
    RouteCase(
        "PATCH",
        "/api/org",
        SCOPED,
        body={"allowed_models": ["scripted-double"]},
    ),
    RouteCase("GET", "/api/org/policies", SCOPED),
    RouteCase(
        "PUT",
        "/api/org/policies",
        SCOPED,
        body={"tool_name": "isolation_probe", "policy": "deny", "scope": "chat"},
    ),
    # The tool name is a literal rather than B's, because B's policy is keyed on
    # B's *organization* — naming their tool here would delete nothing even
    # without isolation, so the honest probe is that A's delete lands in A's org.
    RouteCase(
        "DELETE",
        "/api/org/policies/{scope}/{tool_name}",
        SCOPED,
        path_literals={"scope": "chat", "tool_name": "isolation_probe"},
    ),
    RouteCase("GET", "/api/org/members", SCOPED),
    # The one org route that takes an id from the client, and the sharpest case
    # in this group: A, an admin of A's org, sends B's user id. A 2xx here would
    # mean one organization can enroll another organization's people — the
    # inverse of the tier and worse than a read. 404, indistinguishable from a
    # user that does not exist, because the refusal must not confirm the id.
    RouteCase(
        "PUT",
        "/api/org/members",
        DENY,
        body={"user_id": "", "role": "admin"},
        body_ids={"user_id": "user"},
    ),
]

CASES_BY_KEY = {case.key: case for case in ROUTE_CASES}
