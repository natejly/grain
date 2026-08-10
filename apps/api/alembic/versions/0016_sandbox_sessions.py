"""Give the agent a computer: server-side execution sessions and their history.

ADR 0004 gave generated code a renderer with no network and no filesystem, which
is the right boundary for a React preview and useless for "clean this CSV and
plot it". ADR 0005 adds an execution boundary beside it: per-session Firecracker
microVMs at a provider, reached through `app/services/sandbox/`.

`sandbox_sessions` is the security object, not a cache. `external_id` names a
live machine, and the unique constraint on (provider, external_id) means one row
owns one machine — two workspaces cannot end up pointing at the same sandbox even
if a provider recycles an id. Every lookup filters on `workspace_id`, so the row
is what stops one tenant attaching to another's session.

`network_policy` and `allow_hosts_json` are recorded per session and not read
back from settings at execution time. A sandbox created under `allowlist` keeps
that egress policy for its whole life; relaxing the workspace default later
cannot retroactively open a machine that is already holding someone's documents.

`sandbox_executions` stores output already clipped by the caller. It exists for
the activity trail and for quota accounting, and deliberately not as a log sink.

Revision ID: 0016_sandbox_sessions
Revises: 0015_retrieval_terms
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016_sandbox_sessions"
down_revision = "0015_retrieval_terms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "sandbox_sessions" not in tables:
        op.create_table(
            "sandbox_sessions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("project_id", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("provider", sa.String(length=24), nullable=False, server_default="e2b"),
            sa.Column("external_id", sa.String(length=120), nullable=False),
            sa.Column("template", sa.String(length=80), nullable=False, server_default=""),
            sa.Column("label", sa.String(length=120), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
            sa.Column(
                "network_policy", sa.String(length=16), nullable=False, server_default="open"
            ),
            sa.Column("allow_hosts_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column("exec_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("wall_ms_used", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=False),
            sa.Column("killed_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("provider", "external_id"),
        )
        op.create_index(
            "ix_sandbox_sessions_workspace_id", "sandbox_sessions", ["workspace_id"]
        )
        op.create_index(
            "ix_sandbox_sessions_workspace_status",
            "sandbox_sessions",
            ["workspace_id", "status"],
        )

    if "sandbox_executions" not in tables:
        op.create_table(
            "sandbox_executions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "session_id",
                sa.String(length=36),
                sa.ForeignKey("sandbox_sessions.id"),
                nullable=False,
            ),
            sa.Column("run_id", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("tool_call_id", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("kind", sa.String(length=16), nullable=False, server_default="code"),
            sa.Column("source", sa.Text(), nullable=False, server_default=""),
            sa.Column("exit_code", sa.Integer(), nullable=True),
            sa.Column("stdout", sa.Text(), nullable=False, server_default=""),
            sa.Column("stderr", sa.Text(), nullable=False, server_default=""),
            sa.Column("error", sa.Text(), nullable=False, server_default=""),
            sa.Column("artifact_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_sandbox_executions_workspace_id", "sandbox_executions", ["workspace_id"]
        )
        op.create_index(
            "ix_sandbox_executions_session_id", "sandbox_executions", ["session_id"]
        )
        op.create_index(
            "ix_sandbox_executions_session",
            "sandbox_executions",
            ["session_id", "created_at"],
        )


def downgrade() -> None:
    op.drop_table("sandbox_executions")
    op.drop_table("sandbox_sessions")
