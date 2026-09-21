"""Per-member response styles: the instruction block a member's preference adds.

The seam mirrors `services.spaces`' `for_run`/`space_block` split: a pure
renderer the tests can pin byte-for-byte, and a run-facing resolver whose
every failure degrades to "no injection", never a failed turn.

The load-bearing contract is byte identity for the default. "normal" — and a
missing membership, an automation run (blank `created_by`, a cron task run's
`cron_id`, or a backing `WorkflowRun` — cron and workflow runs DO carry their
creator's id, so the id alone is not the guard), an unknown preset stamped
straight into the database, and "custom" with blank text — all render "", so
`resolve_directives` appends nothing and a member who never touched the
setting runs under instructions byte-identical to today's. That is what keeps
every `== CHAT_INSTRUCTIONS` equality assert in the suite true.

The text is trusted like `Agent.instructions`: member-authored through an
authenticated PUT, never model-writable, so it is not `_screen`ed.
"""
from __future__ import annotations

from typing import Dict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Membership, Run, WorkflowRun

#: The three fixed presets. "normal" and "custom" are deliberately absent:
#: normal renders nothing, and custom renders the member's own text.
STYLE_DIRECTIVES: Dict[str, str] = {
    "concise": (
        "Answer concisely: lead with the answer, keep to the essentials, "
        "and skip preamble."
    ),
    "explanatory": (
        "Answer in an explanatory register: walk through the reasoning, "
        "define terms on first use, and prefer worked examples over bare "
        "conclusions."
    ),
    "formal": (
        "Answer formally: complete sentences, professional tone, precise "
        "terminology, no colloquialisms."
    ),
}


def style_block(preset: str, custom_text: str) -> str:
    """The block a preference renders to — "" whenever there is nothing to say.

    Pure, so the byte-identity pin tests it without a database: "normal", any
    unknown preset, and "custom" with blank text are all "".
    """
    if preset == "custom":
        directive = custom_text.strip()
    else:
        directive = STYLE_DIRECTIVES.get(preset, "")
    if not directive:
        return ""
    return f"Response style for this member:\n\n{directive}"


def for_run(db: Session, run: Run) -> str:
    """The style block for this run's member, or "" — and "" for every failure.

    Automation never inherits its creator's style. A cron task run and a
    workflow's backing run both carry `created_by` (crons.py stamps
    `cron.created_by`, the workflow executor stamps `workflow_run.created_by`),
    so the id alone cannot be the guard: the settings copy promises "your
    future turns", and a nightly report or a workflow node output that a
    downstream node parses is not the member's turn — nor should a run-now
    triggered by member B restyle under creator A's preference. The
    classification mirrors `agent_loop.policy_scope_for_run`: `cron_id` for
    cron tasks, a backing `WorkflowRun` row for workflows.

    Blank `run.created_by` and a missing membership row also mean no block;
    the degraded answer is always the stock behaviour, never a failed turn.
    """
    if not run.created_by or run.cron_id:
        return ""
    backing = db.scalar(select(WorkflowRun.id).where(WorkflowRun.run_id == run.id))
    if backing is not None:
        return ""
    row = db.execute(
        # A scoped select, not db.get: DB_GET_ALLOWLIST stays untouched, and
        # the workspace filter makes a cross-tenant preference unreadable by
        # construction.
        select(Membership.style_preset, Membership.custom_style_text).where(
            Membership.workspace_id == run.workspace_id,
            Membership.user_id == run.created_by,
        )
    ).first()
    if row is None:
        return ""
    preset, custom_text = row
    return style_block(preset or "", custom_text or "")
