"""A conversation's subject becomes polymorphic, and a turn remembers its focus.

`conversations.document_id` said one thing: which document this thread is about.
Threads now open beside projects and dashboards too, and every rule built on
that column — the rail filter, the visibility relaxation, the run-activity
predicate, the cascade on delete, the turn's injected context, and now the
turn's tool registry — is the SAME rule for all three kinds. So the column is
replaced by a pair, `subject_kind` + `subject_id`, rather than joined by two
more nullable ids that each of those rules would have to remember separately.

Existing document threads are carried across, not stranded: every row with a
non-empty `document_id` becomes `('document', <that id>)`, which is exactly what
it already meant. The downgrade puts `document_id` back and re-derives it from
the document-kind rows, so a project or dashboard thread degrades to an ordinary
rail thread rather than to a dangling pointer at a document that never existed.

`runs.subject_focus` is the part of the subject that was on screen when the turn
was sent — the file the project editor had open. On the run because it is a
property of one message, and because a turn that parks on an approval and
resumes later must come back to the file it was asked about.

Revision ID: 0038_conversation_subjects
Revises: 0037_conversation_sharing
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0038_conversation_subjects"
down_revision = "0037_conversation_sharing"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    # Idempotent, matching 0024–0037: these databases have been through
    # `create_all` as well as alembic, so the columns can already be present.
    conversations = _columns("conversations")
    if "subject_kind" not in conversations:
        op.add_column(
            "conversations",
            sa.Column(
                "subject_kind", sa.String(length=16), nullable=False, server_default=""
            ),
        )
    if "subject_id" not in conversations:
        op.add_column(
            "conversations",
            sa.Column(
                "subject_id", sa.String(length=36), nullable=False, server_default=""
            ),
        )
        op.create_index(
            "ix_conversations_subject_id", "conversations", ["subject_id"]
        )
    if "document_id" in conversations:
        # Carry every existing document thread across before the column goes.
        op.execute(
            "UPDATE conversations SET subject_kind = 'document', "
            "subject_id = document_id WHERE document_id <> '' AND subject_id = ''"
        )
        # SQLite validates every index on the table when a column is dropped, so
        # the index over the departing column has to go first — otherwise the
        # DROP COLUMN fails with "error in index … : no such column".
        if "ix_conversations_document_id" in _indexes("conversations"):
            op.drop_index("ix_conversations_document_id", table_name="conversations")
        op.drop_column("conversations", "document_id")
    if "subject_focus" not in _columns("runs"):
        op.add_column(
            "runs",
            sa.Column(
                "subject_focus", sa.String(length=400), nullable=False, server_default=""
            ),
        )


def downgrade() -> None:
    op.drop_column("runs", "subject_focus")
    op.add_column(
        "conversations",
        sa.Column("document_id", sa.String(length=36), nullable=False, server_default=""),
    )
    # Only the document threads have anywhere to go back to. A project or
    # dashboard thread becomes an ordinary rail thread, which is a thread that
    # still opens and still reads — the alternative is a document_id pointing at
    # a row in another table.
    op.execute(
        "UPDATE conversations SET document_id = subject_id "
        "WHERE subject_kind = 'document'"
    )
    op.create_index("ix_conversations_document_id", "conversations", ["document_id"])
    op.drop_index("ix_conversations_subject_id", table_name="conversations")
    op.drop_column("conversations", "subject_id")
    op.drop_column("conversations", "subject_kind")
