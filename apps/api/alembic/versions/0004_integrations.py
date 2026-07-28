"""Add agent tool calls, integration accounts, OAuth state, and sync jobs.

Revision ID: 0004_integrations
Revises: 0003_memory
"""
from __future__ import annotations

from alembic import op
from app import models  # noqa: F401
from app.database import Base

revision = "0004_integrations"
down_revision = "0003_memory"
branch_labels = None
depends_on = None

TABLES = [
    "agent_tool_calls",
    "integration_accounts",
    "oauth_states",
    "sync_jobs",
]


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in TABLES:
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name in reversed(TABLES):
        Base.metadata.tables[table_name].drop(bind=bind, checkfirst=True)
