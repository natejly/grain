"""Let the MCP client authenticate, which is what makes the ecosystem reachable.

`services/mcp/client.py` passed a static header dict and nothing else — no 401
handling, no metadata discovery, no PKCE, no registration, no refresh. That is
the difference between "we have an MCP client" and "we can reach MCP servers":
roughly half of the public registry is remote, most of those authenticate, and a
quarter will not even list their tools to an anonymous caller. Every connector
worth naming — Slack, Notion, Linear, Jira, GitHub, Sentry — is behind that gate.

Two new tables and three columns on an old one:

`mcp_oauth_clients` holds what we registered ourselves as. MCP uses *dynamic*
client registration (RFC 7591) because there is no console where an operator
pastes a client id for a server the user just added, so credentials are minted
per server at connect time and have to be kept. Keyed on (server_id, issuer) so
that a server moving its authorization server mints a fresh registration rather
than reusing credentials the new issuer never granted.

`mcp_oauth_tokens` is per (server, user), not per workspace. An MCP server
authorises a human, so two people sharing a workspace must not share a Linear
account — they get a row each and see their own issues.

`oauth_states` grows `server_id`, `pkce_verifier_enc` and `redirect_uri` rather
than being duplicated, because the CSRF-state machinery is identical to the
connector flow and two implementations of it would be one too many. The verifier
is stored encrypted and server-side on purpose: PKCE's entire guarantee is that
intercepting the authorization code is not enough to redeem it, which evaporates
if the verifier round-trips through the browser.

Revision ID: 0017_mcp_oauth
Revises: 0016_sandbox_sessions
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0017_mcp_oauth"
down_revision = "0016_sandbox_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    state_columns = {column["name"] for column in inspector.get_columns("oauth_states")}
    if "server_id" not in state_columns:
        op.add_column(
            "oauth_states",
            sa.Column("server_id", sa.String(length=36), nullable=False, server_default=""),
        )
    if "pkce_verifier_enc" not in state_columns:
        op.add_column(
            "oauth_states",
            sa.Column("pkce_verifier_enc", sa.Text(), nullable=False, server_default=""),
        )
    if "redirect_uri" not in state_columns:
        op.add_column(
            "oauth_states",
            sa.Column("redirect_uri", sa.String(length=600), nullable=False, server_default=""),
        )

    if "mcp_oauth_clients" not in tables:
        op.create_table(
            "mcp_oauth_clients",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "server_id",
                sa.String(length=36),
                sa.ForeignKey("mcp_servers.id"),
                nullable=False,
            ),
            sa.Column("issuer", sa.String(length=600), nullable=False),
            sa.Column(
                "authorization_endpoint", sa.String(length=600), nullable=False, server_default=""
            ),
            sa.Column("token_endpoint", sa.String(length=600), nullable=False, server_default=""),
            sa.Column(
                "registration_endpoint", sa.String(length=600), nullable=False, server_default=""
            ),
            sa.Column("client_id", sa.String(length=400), nullable=False, server_default=""),
            sa.Column("client_secret_enc", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "registration_access_token_enc", sa.Text(), nullable=False, server_default=""
            ),
            sa.Column(
                "registration_client_uri", sa.String(length=600), nullable=False, server_default=""
            ),
            sa.Column("redirect_uri", sa.String(length=600), nullable=False, server_default=""),
            sa.Column("scopes", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("server_id", "issuer"),
        )
        op.create_index(
            "ix_mcp_oauth_clients_workspace_id", "mcp_oauth_clients", ["workspace_id"]
        )
        op.create_index("ix_mcp_oauth_clients_server_id", "mcp_oauth_clients", ["server_id"])

    if "mcp_oauth_tokens" not in tables:
        op.create_table(
            "mcp_oauth_tokens",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "server_id",
                sa.String(length=36),
                sa.ForeignKey("mcp_servers.id"),
                nullable=False,
            ),
            sa.Column(
                "user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False
            ),
            sa.Column("access_token_enc", sa.Text(), nullable=False, server_default=""),
            sa.Column("refresh_token_enc", sa.Text(), nullable=False, server_default=""),
            sa.Column("token_expires_at", sa.DateTime(), nullable=True),
            sa.Column("scopes", sa.Text(), nullable=False, server_default=""),
            sa.Column("resource", sa.String(length=600), nullable=False, server_default=""),
            sa.Column("status", sa.String(length=24), nullable=False, server_default="connected"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("server_id", "user_id"),
        )
        op.create_index("ix_mcp_oauth_tokens_workspace_id", "mcp_oauth_tokens", ["workspace_id"])
        op.create_index("ix_mcp_oauth_tokens_server_id", "mcp_oauth_tokens", ["server_id"])
        op.create_index("ix_mcp_oauth_tokens_user_id", "mcp_oauth_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_table("mcp_oauth_tokens")
    op.drop_table("mcp_oauth_clients")
    op.drop_column("oauth_states", "redirect_uri")
    op.drop_column("oauth_states", "pkce_verifier_enc")
    op.drop_column("oauth_states", "server_id")
