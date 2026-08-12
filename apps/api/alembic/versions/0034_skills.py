"""Shared skills: a reusable instruction block a person invokes per turn.

Two new tables and two new `runs` columns. `skills` holds the current content
plus a `version` pointer; `skill_versions` retains every prior snapshot so an
edit is a new version and a restore is append-only — the same shape
`datasets`/`dataset_versions` already use. The `runs` columns record which skill
(if any) a turn was invoked with and the args it resolved, so a turn resumed in
a fresh process after an approval or budget park re-injects the same body: they
are empty-string-for-unset like `requested_model`, so a run written before this
feature is byte-for-byte today's behaviour, and they are deliberately not
foreign keys — a run is a historical record that must outlive the skill's
deletion.

Revision ID: 0034_skills
Revises: 0033_run_model_overrides
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0034_skills"
down_revision = "0033_run_model_overrides"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _add(table: str, column: sa.Column) -> None:
    """Idempotent add, matching 0029–0033: these databases have been through
    `create_all` as well as alembic, so a column can already be there and the
    migration still has to run to completion."""
    if column.name not in _columns(table):
        op.add_column(table, column)


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "skills" not in tables:
        op.create_table(
            "skills",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=80), nullable=False),
            sa.Column("title", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("args_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("workspace_id", "name"),
        )
        op.create_index("ix_skills_workspace_id", "skills", ["workspace_id"])

    if "skill_versions" not in tables:
        op.create_table(
            "skill_versions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "skill_id",
                sa.String(length=36),
                sa.ForeignKey("skills.id"),
                nullable=False,
            ),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=160), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("args_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("content_hash", sa.String(length=64), nullable=False, server_default=""),
            sa.Column("created_by", sa.String(length=36), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("skill_id", "version"),
        )
        op.create_index(
            "ix_skill_versions_workspace_id", "skill_versions", ["workspace_id"]
        )
        op.create_index("ix_skill_versions_skill_id", "skill_versions", ["skill_id"])
        op.create_index(
            "ix_skill_versions_workspace_skill",
            "skill_versions",
            ["workspace_id", "skill_id"],
        )

    _add("runs", sa.Column("skill_id", sa.String(length=36), nullable=False, server_default=""))
    _add("runs", sa.Column("skill_args_json", sa.Text(), nullable=False, server_default=""))
    _add("runs", sa.Column("skill_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("runs", "skill_version")
    op.drop_column("runs", "skill_args_json")
    op.drop_column("runs", "skill_id")
    op.drop_table("skill_versions")
    op.drop_table("skills")
