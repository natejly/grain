"""Represent superseded memories, and give chunks the vectors they never had.

Two changes that share a migration because both are prerequisites for measured
work already underway.

Memory: `_upsert_item` keys on a hash of the content, so "deploys on Fly.io" and
"moved to Railway" are unrelated rows that both stay active and both get injected.
Measured on evals/memory_corpus.json, the stale-served rate is 100% — every
superseded fact comes back alongside its correction. `superseded_by` is what lets a
newer claim retire an older one through the existing status chokepoint.

Retrieval: chunks have never carried embeddings, so document search is purely
lexical. `embedding` is the dense half of hybrid retrieval and `context_prefix`
holds the Contextual Retrieval blurb, kept out of `content` so provenance still
quotes the author rather than us.

Revision ID: 0014_memory_supersession_and_chunk_vectors
Revises: 0013_auth
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0014_memory_supersession_and_chunk_vectors"
down_revision = "0013_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    memory_columns = {column["name"] for column in inspector.get_columns("memory_items")}
    if "superseded_by" not in memory_columns:
        op.add_column(
            "memory_items", sa.Column("superseded_by", sa.String(36), nullable=True)
        )

    chunk_columns = {column["name"] for column in inspector.get_columns("chunks")}
    if "embedding" not in chunk_columns:
        op.add_column("chunks", sa.Column("embedding", sa.LargeBinary(), nullable=True))
    if "embedding_model" not in chunk_columns:
        op.add_column(
            "chunks",
            sa.Column("embedding_model", sa.String(64), nullable=False, server_default=""),
        )
    if "context_prefix" not in chunk_columns:
        op.add_column(
            "chunks",
            sa.Column("context_prefix", sa.Text(), nullable=False, server_default=""),
        )


def downgrade() -> None:
    op.drop_column("chunks", "context_prefix")
    op.drop_column("chunks", "embedding_model")
    op.drop_column("chunks", "embedding")
    op.drop_column("memory_items", "superseded_by")
