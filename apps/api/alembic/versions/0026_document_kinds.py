"""Document kinds become 'text' and 'markdown'; 'latex' was never a format.

A document stored as kind "latex" was markdown in every respect that reached a
user: it was rendered by the same ReactMarkdown pipeline, its maths went through
the same KaTeX, and nothing anywhere compiled it. The only difference was the
word, and the word was wrong often enough that the TeX compiler — which lives in
Projects and does produce a PDF — was reported broken twice. Commit 6d3e738
relabelled the format "Markdown + math" and left the stored value alone; this
finishes the job by removing the value too.

So the rewrite below is not a lossy reinterpretation. It renames rows to what
they already were. What the kinds gain in exchange is a real second format:
"text", shown verbatim in a monospace pane with no markdown processing at all,
which is the thing the enum was missing.

Downgrade is deliberately asymmetric. It folds "text" back into "markdown" so
that code which only knows {markdown, latex} can still read every row; it does
not resurrect "latex", because no record of which rows carried that label
survives and inventing one would be worse than the loss. The label named nothing
a reader could tell apart, which is why it is going.

Revision ID: 0026_document_kinds
Revises: 0025_reachable_outputs
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0026_document_kinds"
down_revision = "0025_reachable_outputs"
branch_labels = None
depends_on = None

_DOCUMENTS = sa.table("documents", sa.column("kind", sa.String))


def upgrade() -> None:
    op.execute(
        _DOCUMENTS.update()
        .where(_DOCUMENTS.c.kind == op.inline_literal("latex"))
        .values(kind="markdown")
    )


def downgrade() -> None:
    op.execute(
        _DOCUMENTS.update()
        .where(_DOCUMENTS.c.kind == op.inline_literal("text"))
        .values(kind="markdown")
    )
