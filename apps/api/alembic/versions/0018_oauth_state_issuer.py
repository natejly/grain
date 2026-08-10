"""Bind an in-flight authorization to the authorization server that began it.

0017 keyed registrations on (server_id, issuer) so a server that moves its
authorization server mints fresh credentials. What it left open is the window
*between* the two halves of one flow. `OAuthState` recorded which MCP server the
user was connecting but not which issuer the authorize URL had been built from,
so the callback resolved the registration by "most recently updated wins".

That is an authorization-server mix-up (the attack RFC 9207 exists to answer).
A user clicks Connect and is sent to issuer A. Before they finish consenting the
MCP server — which is remote, third-party, and the entire threat model here —
changes its advertised `authorization_servers` to one the attacker runs, and any
second Connect mints a newer registration. The first user's callback then posts
their authorization code *and* their PKCE verifier to the attacker's token
endpoint, which is everything needed to redeem that code at issuer A. Retiring
old registrations does not close it: the retirement is what makes the newer row
the only one, and the state row still cannot say which issuer it meant.

One nullable column, matched on the way back. Nullable rather than defaulted
because an empty issuer has to stay legible as "minted before this migration" —
those states are refused rather than guessed at, and they expire in ten minutes.

Revision ID: 0018_oauth_state_issuer
Revises: 0017_mcp_oauth
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0018_oauth_state_issuer"
down_revision = "0017_mcp_oauth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("oauth_states")}
    if "issuer" not in columns:
        op.add_column(
            "oauth_states",
            sa.Column("issuer", sa.String(length=600), nullable=False, server_default=""),
        )


def downgrade() -> None:
    op.drop_column("oauth_states", "issuer")
