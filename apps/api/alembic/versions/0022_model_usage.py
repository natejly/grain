"""Token and cost accounting: one row per billable model call.

Before this, nothing in the API counted a token. A workflow DAG could fan out
model calls nobody could price, an owner had no usage column to look at, and the
only way to notice a runaway agent loop was the iteration counter — never the
bill. `model_usage` is the ledger that makes those answerable.

Three properties are built into the shape rather than left to the writer.

*Counts, never content.* There is no column here that can hold a prompt or a
completion. The table is kept longer than a conversation and read by an admin
panel, so a "preview" column would be a second copy of everyone's messages with
none of the access rules that guard the first one.

*The rate is stored with the cost.* `cost_usd` is computed once, at write time,
from the rate configured at that moment, and the rate that produced it is
recorded beside it. Prices change; a row that already happened must not silently
reprice when they do, and an operator has to be able to see which rate produced
which figure.

*Null cost is a real answer.* A model with no configured rate records every
token and leaves `cost_usd` null. Zero would say "free" and a guessed rate would
say something worse — a number nothing downstream could tell apart from a
measured one.

`run_id`, `conversation_id` and `user_id` are plain columns, not foreign keys:
an embedding of an uploaded document has neither a run nor a conversation, and
deleting a conversation must not be able to erase what it cost. `workspace_id`
is a foreign key, because a row belonging to no tenant can neither be scoped nor
shown.

Revision ID: 0022_model_usage
Revises: 0021_decision_is_final
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0022_model_usage"
down_revision = "0021_decision_is_final"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "model_usage" in tables:
        return

    op.create_table(
        "model_usage",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "workspace_id",
            sa.String(length=36),
            sa.ForeignKey("workspaces.id"),
            nullable=False,
        ),
        sa.Column("run_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column(
            "conversation_id", sa.String(length=36), nullable=False, server_default=""
        ),
        sa.Column("user_id", sa.String(length=36), nullable=False, server_default=""),
        sa.Column("operation", sa.String(length=32), nullable=False, server_default=""),
        sa.Column(
            "provider", sa.String(length=24), nullable=False, server_default="openai"
        ),
        sa.Column("model", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cached_input_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reasoning_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        # Nullable on purpose: see the module docstring. Null is "no rate was
        # configured for this model", which is not the same fact as zero.
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("input_rate_usd_per_mtok", sa.Float(), nullable=True),
        sa.Column("cached_input_rate_usd_per_mtok", sa.Float(), nullable=True),
        sa.Column("output_rate_usd_per_mtok", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_model_usage_workspace_id", "model_usage", ["workspace_id"])
    op.create_index("ix_model_usage_operation", "model_usage", ["operation"])
    op.create_index("ix_model_usage_created_at", "model_usage", ["created_at"])
    op.create_index(
        "ix_model_usage_workspace_created", "model_usage", ["workspace_id", "created_at"]
    )
    op.create_index(
        "ix_model_usage_workspace_run", "model_usage", ["workspace_id", "run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_model_usage_workspace_run", table_name="model_usage")
    op.drop_index("ix_model_usage_workspace_created", table_name="model_usage")
    op.drop_index("ix_model_usage_created_at", table_name="model_usage")
    op.drop_index("ix_model_usage_operation", table_name="model_usage")
    op.drop_index("ix_model_usage_workspace_id", table_name="model_usage")
    op.drop_table("model_usage")
