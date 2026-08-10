"""Give the lexical half of retrieval a real inverted index.

`search_evidence` scored raw term frequency with no IDF, which meant a match on a
rare proper noun and a match on "storage" contributed the same weight — and it
computed that by re-tokenizing every chunk in the workspace in Python on every
query, which is O(corpus) regex work per turn.

`chunk_terms` is the portable fix: one row per (chunk, term) with its frequency,
so BM25's tf, df and document length all come out of SQL joins that SQLite and
Postgres plan the same way. Deliberately *not* FTS5/tsvector — those are faster
and they reintroduce the two-backends-that-rank-differently problem that got a
vector extension rejected in the first place (RESEARCH.md #2, #36).

`chunks.lexical_length` is the document length BM25's `b` normalisation needs,
and it doubles as the index's watermark: NULL means "this chunk has never been
indexed", which is how every chunk written before this migration — and any chunk
written by a path that forgets to index — still becomes searchable. Existing rows
are deliberately left NULL rather than backfilled here: tokenization lives in
application code, and a migration that imports the app pins the schema to one
version of it.

Revision ID: 0015_retrieval_terms
Revises: 0014_memory_supersession_and_chunk_vectors
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0015_retrieval_terms"
down_revision = "0014_memory_supersession_and_chunk_vectors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    chunk_columns = {column["name"] for column in inspector.get_columns("chunks")}
    if "lexical_length" not in chunk_columns:
        op.add_column("chunks", sa.Column("lexical_length", sa.Integer(), nullable=True))

    if "chunk_terms" not in set(inspector.get_table_names()):
        op.create_table(
            "chunk_terms",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False
            ),
            sa.Column("chunk_id", sa.String(36), sa.ForeignKey("chunks.id"), nullable=False),
            sa.Column("term", sa.String(64), nullable=False),
            sa.Column("tf", sa.Integer(), nullable=False),
        )
        # The read path's only lookup: "which chunks in this workspace contain
        # this term". Workspace first because every query is workspace-scoped.
        op.create_index("ix_chunk_terms_workspace_term", "chunk_terms", ["workspace_id", "term"])
        # The write path's only lookup: delete a chunk's postings before rewriting
        # them, which happens on every re-ingest.
        op.create_index("ix_chunk_terms_chunk", "chunk_terms", ["chunk_id"])


def downgrade() -> None:
    op.drop_index("ix_chunk_terms_chunk", table_name="chunk_terms")
    op.drop_index("ix_chunk_terms_workspace_term", table_name="chunk_terms")
    op.drop_table("chunk_terms")
    op.drop_column("chunks", "lexical_length")
