"""Workflow automations: a compiled DAG, its runs, and per-node run state.

ADR 0007. A workflow is natural language on one side and a validated DAG on the
other. `workflows.graph_json` holds the compiled graph; `workflows.source_prompt`
holds the sentence it came from, because a recompile that drifts from the ask is
only detectable when the ask survives.

`workflow_runs` deliberately mirrors `runs` — same status vocabulary, same
`waiting_for_approval` park, and a nullable `run_id` pointing at the chat run
that carries the approval record and the RunEvent stream. That is the whole
integration: a workflow that hits a write-capable tool parks through the
machinery that already exists rather than through a second one beside it.

`workflow_node_runs` is what makes a resume cheap. The unique constraint on
(workflow_run_id, node_key) turns "skip the nodes that already finished" into a
database fact: a resumed executor selects the table, and a node that succeeded
cannot be inserted twice or executed twice. `policy` records which authority let
each node run — a standing workspace grant or a specific human decision — so the
audit trail can tell an unattended 3am write apart from an approved one.

Revision ID: 0019_workflows
Revises: 0018_oauth_state_issuer
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0019_workflows"
down_revision = "0018_oauth_state_issuer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "workflows" not in tables:
        op.create_table(
            "workflows",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "created_by", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False
            ),
            sa.Column("name", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("source_prompt", sa.Text(), nullable=False, server_default=""),
            sa.Column("graph_json", sa.Text(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
            sa.Column(
                "trigger_kind", sa.String(length=16), nullable=False, server_default="manual"
            ),
            sa.Column("schedule_cron", sa.String(length=120), nullable=False, server_default=""),
            sa.Column(
                "schedule_timezone", sa.String(length=64), nullable=False, server_default="UTC"
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_workflows_workspace_id", "workflows", ["workspace_id"])
        op.create_index(
            "ix_workflows_workspace_status", "workflows", ["workspace_id", "status"]
        )

    if "workflow_runs" not in tables:
        op.create_table(
            "workflow_runs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "workflow_id",
                sa.String(length=36),
                sa.ForeignKey("workflows.id"),
                nullable=False,
            ),
            sa.Column("created_by", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("workflow_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("graph_json", sa.Text(), nullable=False, server_default=""),
            sa.Column("trigger", sa.String(length=16), nullable=False, server_default="manual"),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="queued"),
            sa.Column(
                "run_id", sa.String(length=36), sa.ForeignKey("runs.id"), nullable=True
            ),
            sa.Column("input_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("0")
            ),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_workflow_runs_workspace_id", "workflow_runs", ["workspace_id"])
        op.create_index("ix_workflow_runs_workflow_id", "workflow_runs", ["workflow_id"])
        op.create_index("ix_workflow_runs_status", "workflow_runs", ["status"])
        op.create_index("ix_workflow_runs_run_id", "workflow_runs", ["run_id"])
        op.create_index(
            "ix_workflow_runs_workspace_status", "workflow_runs", ["workspace_id", "status"]
        )
        op.create_index(
            "ix_workflow_runs_workflow_created",
            "workflow_runs",
            ["workflow_id", "created_at"],
        )

    if "workflow_node_runs" not in tables:
        op.create_table(
            "workflow_node_runs",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "workflow_run_id",
                sa.String(length=36),
                sa.ForeignKey("workflow_runs.id"),
                nullable=False,
            ),
            sa.Column("node_key", sa.String(length=80), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="tool"),
            sa.Column("tool_name", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="pending"),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("arguments_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("output_json", sa.Text(), nullable=False, server_default=""),
            sa.Column("policy", sa.String(length=16), nullable=False, server_default=""),
            sa.Column(
                "agent_tool_call_id",
                sa.String(length=36),
                sa.ForeignKey("agent_tool_calls.id"),
                nullable=True,
            ),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workflow_run_id", "node_key"),
        )
        op.create_index(
            "ix_workflow_node_runs_workspace_id", "workflow_node_runs", ["workspace_id"]
        )
        op.create_index(
            "ix_workflow_node_runs_workflow_run_id", "workflow_node_runs", ["workflow_run_id"]
        )
        op.create_index(
            "ix_workflow_node_runs_run_status",
            "workflow_node_runs",
            ["workflow_run_id", "status"],
        )


def downgrade() -> None:
    op.drop_table("workflow_node_runs")
    op.drop_table("workflow_runs")
    op.drop_table("workflows")
