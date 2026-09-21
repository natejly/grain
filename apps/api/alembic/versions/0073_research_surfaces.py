"""Research surfaces: pages, coverage ledgers, deliverable manifests, watches.

THE cycle's single migration. Every cluster of the Perplexity-harness cycle was
told to design migration-free and to hand its wanted columns here, so the four
parallel diffs merge inside one function rather than racing for a revision
number. The anchor comments in `upgrade()` mark where each cluster's adds go,
and `downgrade()` mirrors them in reverse.

What this migration carries of its own (cluster D, the research surfaces):

* `messages.followups_json` — the suggested next questions for this answer.
  '' means never computed; '[]' means computed and nothing was admitted. Two
  different facts, exactly as `citation_report_json` already distinguishes
  "never checked" from "checked and clean".
* `notifications.page_id` / `notifications.watch_id` — two more deep-link
  columns in the fan `Notification` already documents. A new notifying feature
  adds a `kind` and picks its deep links; it does not add a table.
* `pages` / `page_citations` — a published answer and its FROZEN evidence. A
  page is a snapshot, deliberately unlike every other share-link resource.
* `coverage_ledgers` / `coverage_entries` — what a research run actually looked
  at, as data.
* `deliverable_manifests` / `manifest_files` — what a deliverable run produced,
  and what it spent producing it.
* `watches` / `watch_observations` — Cron's shape once more: the same 5-field
  `schedule_cron` + IANA zone, the same `last_dispatched_at` conditional-UPDATE
  claim advanced by the same tick.

Carried for the other clusters:

* cluster A — `grounded_receipts`, one row per machine-answered question on the
  grounded-answer API surface. Only `workspace_id` is a ForeignKey; the token,
  user, space and generation it names are plain strings so the receipt outlives
  every one of them.
* cluster B — `runs.preset`, `runs.retrieval_budget`, `runs.step_plan` and
  `conversations.default_preset`.
* cluster C — `agent_tool_calls.gate_reason` and `workspaces.taint_gating`.
* cluster E — `runs.prompt_fingerprint`.

NO backfill anywhere: the server defaults ARE the historical truth. Nothing was
ever computed, published, watched or manifested; no run carried a preset or a
fingerprint; no call was raised by the taint gate; every workspace followed the
deployment default.

All adds are guarded with 0071/0072's inspector template (has_table first, then
column presence), so the partial-legacy replay test and databases built by
`create_all` both skip cleanly. The downgrade drops the tables newest-first and
then the columns, guarded the same way — a create_all database arrives at 0001
already holding today's columns, and an unguarded drop would fail on exactly
the databases built cleanly from scratch.

Revision ID: 0073_research_surfaces
Revises: 0072_identity_styles_feedback
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0073_research_surfaces"
down_revision = "0072_identity_styles_feedback"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # --- cluster D (research surfaces) columns ---
    if inspector.has_table("messages") and "followups_json" not in _columns("messages"):
        op.add_column(
            "messages",
            sa.Column(
                "followups_json",
                sa.Text(),
                nullable=False,
                server_default="",
            ),
        )
    if inspector.has_table("notifications"):
        if "page_id" not in _columns("notifications"):
            op.add_column(
                "notifications",
                sa.Column(
                    "page_id",
                    sa.String(length=36),
                    nullable=False,
                    server_default="",
                ),
            )
        if "watch_id" not in _columns("notifications"):
            op.add_column(
                "notifications",
                sa.Column(
                    "watch_id",
                    sa.String(length=36),
                    nullable=False,
                    server_default="",
                ),
            )

    # --- cluster D (research surfaces) tables ---
    if not inspector.has_table("pages"):
        op.create_table(
            "pages",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            # Plain column, not a ForeignKey: a page outlives its thread.
            sa.Column(
                "conversation_id",
                sa.String(length=36),
                nullable=False,
                server_default="",
            ),
            sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
            sa.Column("body_md", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "published_by", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "generation_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "status", sa.String(length=16), nullable=False, server_default="published"
            ),
            sa.Column("drift_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("drift_checked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_pages_workspace_id", "pages", ["workspace_id"])
        op.create_index(
            "ix_pages_workspace_status", "pages", ["workspace_id", "status"]
        )
    if not inspector.has_table("page_citations"):
        op.create_table(
            "page_citations",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "page_id",
                sa.String(length=36),
                sa.ForeignKey("pages.id"),
                nullable=False,
            ),
            sa.Column("marker", sa.Integer(), nullable=False, server_default="0"),
            # Plain columns: the chunk or the source may be deleted under a page
            # whose whole value is that its evidence stays pinned.
            sa.Column(
                "chunk_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "source_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "filename", sa.String(length=255), nullable=False, server_default=""
            ),
            sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("frozen_excerpt", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "content_hash", sa.String(length=64), nullable=False, server_default=""
            ),
            sa.Column(
                "generation_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "status", sa.String(length=16), nullable=False, server_default="frozen"
            ),
            sa.Column("checked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "page_id", "marker", name="uq_page_citations_page_marker"
            ),
        )
        op.create_index(
            "ix_page_citations_workspace_id", "page_citations", ["workspace_id"]
        )
        op.create_index("ix_page_citations_page_id", "page_citations", ["page_id"])
    if not inspector.has_table("coverage_ledgers"):
        op.create_table(
            "coverage_ledgers",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column("run_id", sa.String(length=36), nullable=False, server_default=""),
            sa.Column(
                "workflow_run_id",
                sa.String(length=36),
                nullable=False,
                server_default="",
            ),
            sa.Column("question", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "shape", sa.String(length=16), nullable=False, server_default="plain"
            ),
            sa.Column(
                "in_scope_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "consulted_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "one_sided", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_coverage_ledgers_workspace_id", "coverage_ledgers", ["workspace_id"]
        )
        op.create_index("ix_coverage_ledgers_run_id", "coverage_ledgers", ["run_id"])
        op.create_index(
            "ix_coverage_ledgers_workspace_run",
            "coverage_ledgers",
            ["workspace_id", "run_id"],
        )
    if not inspector.has_table("coverage_entries"):
        op.create_table(
            "coverage_entries",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "ledger_id",
                sa.String(length=36),
                sa.ForeignKey("coverage_ledgers.id"),
                nullable=False,
            ),
            sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("sub_question", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "stance", sa.String(length=16), nullable=False, server_default="neutral"
            ),
            sa.Column(
                "supported", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("query", sa.Text(), nullable=False, server_default=""),
            sa.Column("chunk_ids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("source_ids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "ledger_id", "ordinal", name="uq_coverage_entries_ledger_ordinal"
            ),
        )
        op.create_index(
            "ix_coverage_entries_workspace_id", "coverage_entries", ["workspace_id"]
        )
        op.create_index(
            "ix_coverage_entries_ledger_id", "coverage_entries", ["ledger_id"]
        )
    if not inspector.has_table("deliverable_manifests"):
        op.create_table(
            "deliverable_manifests",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "workflow_run_id",
                sa.String(length=36),
                nullable=False,
                server_default="",
            ),
            # THE SENTINEL: '' is the workspace library, never NULL. Both engines
            # treat NULLs inside a unique index as distinct; '' collides with ''.
            sa.Column(
                "space_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
            sa.Column(
                "status", sa.String(length=16), nullable=False, server_default="complete"
            ),
            sa.Column(
                "budget_seconds", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "budget_tool_calls", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("spent_seconds", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "spent_tool_calls", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "ledger_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "created_by", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_deliverable_manifests_workspace_id",
            "deliverable_manifests",
            ["workspace_id"],
        )
        op.create_index(
            "ix_deliverable_manifests_workflow_run_id",
            "deliverable_manifests",
            ["workflow_run_id"],
        )
        op.create_index(
            "ix_deliverable_manifests_workspace_space",
            "deliverable_manifests",
            ["workspace_id", "space_id"],
        )
    if not inspector.has_table("manifest_files"):
        op.create_table(
            "manifest_files",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "manifest_id",
                sa.String(length=36),
                sa.ForeignKey("deliverable_manifests.id"),
                nullable=False,
            ),
            sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
            # Plain column: the Source `sandbox_download` already created.
            sa.Column(
                "source_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "filename", sa.String(length=255), nullable=False, server_default=""
            ),
            sa.Column("byte_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "sandbox_session_id",
                sa.String(length=36),
                nullable=False,
                server_default="",
            ),
            sa.Column("queries_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("chunk_ids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint(
                "manifest_id", "ordinal", name="uq_manifest_files_manifest_ordinal"
            ),
        )
        op.create_index(
            "ix_manifest_files_workspace_id", "manifest_files", ["workspace_id"]
        )
        op.create_index(
            "ix_manifest_files_manifest_id", "manifest_files", ["manifest_id"]
        )
    if not inspector.has_table("watches"):
        op.create_table(
            "watches",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "created_by", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column("name", sa.String(length=160), nullable=False, server_default=""),
            sa.Column(
                "target_kind", sa.String(length=16), nullable=False, server_default=""
            ),
            sa.Column(
                "target_id", sa.String(length=36), nullable=False, server_default=""
            ),
            # SENTINEL, derived server-side and never read off a request body.
            sa.Column(
                "space_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "shared", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column(
                "schedule_cron", sa.String(length=120), nullable=False, server_default=""
            ),
            sa.Column(
                "schedule_timezone",
                sa.String(length=64),
                nullable=False,
                server_default="UTC",
            ),
            sa.Column(
                "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column(
                "extraction_schema_json", sa.Text(), nullable=False, server_default="[]"
            ),
            sa.Column(
                "brief_document_id",
                sa.String(length=36),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "last_fingerprint",
                sa.String(length=64),
                nullable=False,
                server_default="",
            ),
            sa.Column(
                "last_chunk_ids_json", sa.Text(), nullable=False, server_default="[]"
            ),
            sa.Column("last_checked_at", sa.DateTime(), nullable=True),
            sa.Column("last_change_at", sa.DateTime(), nullable=True),
            # THE CLAIM COLUMN — advanced by a conditional UPDATE, exactly as
            # `crons.last_dispatched_at` is, by the same tick.
            sa.Column("last_dispatched_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_watches_workspace_id", "watches", ["workspace_id"])
        op.create_index(
            "ix_watches_workspace_enabled", "watches", ["workspace_id", "enabled"]
        )
    if not inspector.has_table("watch_observations"):
        op.create_table(
            "watch_observations",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            sa.Column(
                "watch_id",
                sa.String(length=36),
                sa.ForeignKey("watches.id"),
                nullable=False,
            ),
            sa.Column(
                "fingerprint", sa.String(length=64), nullable=False, server_default=""
            ),
            sa.Column("added_chunks", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "removed_chunks", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "changed", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("fields_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("memory_ids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("chunk_ids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_watch_observations_workspace_id", "watch_observations", ["workspace_id"]
        )
        op.create_index(
            "ix_watch_observations_watch_created",
            "watch_observations",
            ["watch_id", "created_at"],
        )

    # --- cluster A (verified grounding) columns land here ---
    if not inspector.has_table("grounded_receipts"):
        op.create_table(
            "grounded_receipts",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column(
                "workspace_id",
                sa.String(length=36),
                sa.ForeignKey("workspaces.id"),
                nullable=False,
            ),
            # Ledger-style plain strings: a receipt outlives the token, the
            # member, the space and the generation it names.
            sa.Column(
                "token_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "user_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "space_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column("question", sa.Text(), nullable=False, server_default=""),
            sa.Column("answer", sa.Text(), nullable=False, server_default=""),
            sa.Column("citations_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("report_json", sa.Text(), nullable=False, server_default=""),
            sa.Column("schema_json", sa.Text(), nullable=False, server_default=""),
            sa.Column("structured_json", sa.Text(), nullable=False, server_default=""),
            sa.Column(
                "generation_id", sa.String(length=36), nullable=False, server_default=""
            ),
            sa.Column(
                "evidence_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "grounding_score",
                sa.Float(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index(
            "ix_grounded_receipts_workspace_id", "grounded_receipts", ["workspace_id"]
        )
        op.create_index(
            "ix_grounded_receipts_workspace_created",
            "grounded_receipts",
            ["workspace_id", "created_at"],
        )

    # --- cluster B (plan-then-execute) columns land here ---
    if inspector.has_table("runs"):
        if "preset" not in _columns("runs"):
            op.add_column(
                "runs",
                sa.Column(
                    "preset", sa.String(length=32), nullable=False, server_default=""
                ),
            )
        if "retrieval_budget" not in _columns("runs"):
            op.add_column(
                "runs",
                sa.Column(
                    "retrieval_budget",
                    sa.String(length=8),
                    nullable=False,
                    server_default="",
                ),
            )
        if "step_plan" not in _columns("runs"):
            op.add_column(
                "runs",
                sa.Column(
                    "step_plan",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                ),
            )
    if (
        inspector.has_table("conversations")
        and "default_preset" not in _columns("conversations")
    ):
        op.add_column(
            "conversations",
            sa.Column(
                "default_preset",
                sa.String(length=32),
                nullable=False,
                server_default="",
            ),
        )

    # --- cluster C (council / presets) columns land here ---
    if (
        inspector.has_table("agent_tool_calls")
        and "gate_reason" not in _columns("agent_tool_calls")
    ):
        op.add_column(
            "agent_tool_calls",
            sa.Column(
                "gate_reason", sa.String(length=64), nullable=False, server_default=""
            ),
        )
    if (
        inspector.has_table("workspaces")
        and "taint_gating" not in _columns("workspaces")
    ):
        op.add_column(
            "workspaces",
            sa.Column(
                "taint_gating", sa.String(length=8), nullable=False, server_default=""
            ),
        )

    # --- cluster E (grounded-answer API receipts) columns land here ---
    if inspector.has_table("runs") and "prompt_fingerprint" not in _columns("runs"):
        op.add_column(
            "runs",
            sa.Column(
                "prompt_fingerprint",
                sa.String(length=80),
                nullable=False,
                server_default="",
            ),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # --- cluster E (grounded-answer API receipts) drops ---
    if inspector.has_table("runs") and "prompt_fingerprint" in _columns("runs"):
        op.drop_column("runs", "prompt_fingerprint")

    # --- cluster C (council / presets) drops ---
    if inspector.has_table("workspaces") and "taint_gating" in _columns("workspaces"):
        op.drop_column("workspaces", "taint_gating")
    if (
        inspector.has_table("agent_tool_calls")
        and "gate_reason" in _columns("agent_tool_calls")
    ):
        op.drop_column("agent_tool_calls", "gate_reason")

    # --- cluster B (plan-then-execute) drops ---
    if (
        inspector.has_table("conversations")
        and "default_preset" in _columns("conversations")
    ):
        op.drop_column("conversations", "default_preset")
    if inspector.has_table("runs"):
        if "step_plan" in _columns("runs"):
            op.drop_column("runs", "step_plan")
        if "retrieval_budget" in _columns("runs"):
            op.drop_column("runs", "retrieval_budget")
        if "preset" in _columns("runs"):
            op.drop_column("runs", "preset")

    # --- cluster A (verified grounding) drops ---
    if inspector.has_table("grounded_receipts"):
        op.drop_table("grounded_receipts")

    # --- cluster D (research surfaces) tables, newest first ---
    for table in (
        "watch_observations",
        "watches",
        "manifest_files",
        "deliverable_manifests",
        "coverage_entries",
        "coverage_ledgers",
        "page_citations",
        "pages",
    ):
        if inspector.has_table(table):
            op.drop_table(table)

    # --- cluster D (research surfaces) columns ---
    if inspector.has_table("notifications"):
        if "watch_id" in _columns("notifications"):
            op.drop_column("notifications", "watch_id")
        if "page_id" in _columns("notifications"):
            op.drop_column("notifications", "page_id")
    if inspector.has_table("messages") and "followups_json" in _columns("messages"):
        op.drop_column("messages", "followups_json")
