from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, Optional

import httpx
from sqlalchemy import func, select

from ..clock import utcnow
from ..config import Settings, get_settings
from ..database import SessionLocal
from ..models import ConversationChunk, Message, Run, Tool, ToolCall, ToolGrant
from . import (
    coverage,
    followups,
    grounded,
    model,
    spaces,
    step_plan,
    usage,
    webhooks,
)
from .agent_loop import (
    PAUSED_FOR_BUDGET,
    resume_after_budget,
    resume_agent_turn,
    run_agent_turn,
)
from .audit import record_audit
from .citations import grade_grounding, validate_citations
from .conversation_index import update_conversation_index
from .errors import user_facing_message
from .events import append_event
from .memory import (
    memory_opted_in,
    recall,
    render_memory_context,
    write_conversation_memory,
)
from .model import stream_words
from .retrieval import Evidence, budget_for, search_evidence
from .tools import ToolSecurityError, execute_read_only_get, parse_tool_prompt
from .web_search import citation_url

logger = logging.getLogger(__name__)

TERMINAL_RUN_STATES = {"completed", "failed", "cancelled"}


def _citations(evidence: list[Evidence]) -> list[dict[str, object]]:
    return [
        {
            "chunk_id": item.chunk_id,
            "source_id": item.source_id,
            "filename": item.filename,
            "ordinal": item.ordinal,
            "excerpt": item.excerpt,
            "score": item.score,
            # None for a passage from the index, whose provenance is its chunk.
            # A web source has no chunk to open, so the URL is the only address
            # a reader can follow back to what was cited.
            "url": citation_url(item),
            # The fingerprint of the CONTENT that was quoted, so a stored
            # citation can be re-verified later against the exact text it
            # cited — and so a chunk rewritten since the answer is detectable
            # rather than silently re-read. "" for a web passage, which has no
            # indexed chunk. `retrieval.evidence_manifest` is the same mapping
            # in the numbered [n] form the grounding validator wants.
            "fingerprint": item.chunk_fingerprint,
        }
        for item in evidence
    ]


#: Per-message budget inside the verbatim window.
MAX_TRANSCRIPT_MESSAGE_CHARS = 600

#: What a clipped message ends with. A marker, not decoration: without it a
#: message cut at 600 characters reads as a complete sentence the speaker never
#: finished, and the model answers the truncation instead of the turn.
ELISION = " […]"

#: How much of the thread summary the digest carries. Enough for a rolling
#: summary of a long thread, small enough that it cannot crowd out the verbatim
#: window it exists to give context to.
MAX_TRANSCRIPT_SUMMARY_CHARS = 1200


def _clip(text: str, limit: int) -> str:
    """Collapse whitespace and cut at a WORD boundary, marking the cut.

    The old `[:600]` cut mid-word — "the deploy host is rail" — which is worse
    than a shorter quote: a fragment is indistinguishable from a complete one,
    so a model reads an invented word as a fact. Cutting at the last space
    before the limit and appending an elision marker makes the truncation
    visible to the reader that matters.

    A single word longer than the limit has no space to cut at, and is cut
    where it must be: a 700-character token is not prose, and losing all of it
    would lose the message.
    """
    body = " ".join(text.split())
    if len(body) <= limit:
        return body
    head = body[:limit]
    boundary = head.rfind(" ")
    if boundary > 0:
        head = head[:boundary]
    return head.rstrip() + ELISION


def _older_context(db, run: Run) -> tuple[str, int]:
    """The thread's rolling summary and how many messages it covers.

    `conversation_chunks` has held one `kind="summary"` row per thread since
    the conversation index shipped, refreshed every ten messages, and nothing
    on the run path ever read it — so a turn twelve messages into a thread was
    answering with no knowledge of the first two, and looked to the user as if
    it had forgotten what it was told.

    Workspace-filtered as well as conversation-filtered: the id comes off a
    `Run`, and a summary read across a tenant boundary would be spliced
    straight into this turn's prompt.

    The count matters as much as the text. The summary covers messages
    1..`message_count` and is refreshed only every `SUMMARY_REFRESH_EVERY`
    messages; the verbatim window covers the last
    `settings.memory_transcript_messages`. Those two numbers are equal only by
    coincidence today, and the caller has to be able to tell when they are not
    — see `_transcript`.

    Returns ("", 0) for every failure — no row, no index, an empty summary —
    which is the same silence as a short thread with nothing older to say.
    """
    if not run.conversation_id:
        return "", 0
    summary = db.scalar(
        select(ConversationChunk).where(
            ConversationChunk.workspace_id == run.workspace_id,
            ConversationChunk.conversation_id == run.conversation_id,
            ConversationChunk.kind == "summary",
        )
    )
    if summary is None:
        return "", 0
    return _clip(summary.content, MAX_TRANSCRIPT_SUMMARY_CHARS), int(
        summary.message_count or 0
    )


def _transcript(db, run: Run) -> list[tuple[str, str]]:
    """The thread so far: a rolling summary of what fell out of the window,
    then the last N messages verbatim.

    Two tiers rather than one, because the single tier was lying by omission:
    the window is ten messages, and message eleven simply stopped existing for
    every turn after it. The summary row already existed and already cost an
    LLM call somebody paid for.

    Everything here goes into `input_items`, never into `instructions`. That is
    the system-prompt-outside-transcript invariant and it is load-bearing for
    anything that later compacts a turn: the transcript is the compactable
    region, and instructions that lived inside it could be summarised away —
    a model that forgets its own citation contract mid-turn.
    """
    limit = get_settings().memory_transcript_messages
    scope = (
        Message.workspace_id == run.workspace_id,
        Message.conversation_id == run.conversation_id,
        Message.run_id != run.id,
    )
    rows = list(
        db.scalars(
            select(Message)
            .where(*scope)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
    )
    rows.reverse()
    window = [
        (row.role, _clip(row.content, MAX_TRANSCRIPT_MESSAGE_CHARS)) for row in rows
    ]
    # Only when the window is full, which is the one case where something older
    # may exist. A short thread is already quoted in full, and prefixing a
    # summary of what the reader can see is noise the model has to reconcile.
    if len(rows) < limit:
        return window
    older, covered = _older_context(db, run)
    if not older:
        return window
    # THE COVERAGE CHECK, and the reason it is not an assertion about two
    # constants: the summary covers messages 1..`covered`, the window covers
    # the last `limit`, and they meet only while `covered >= total - limit`.
    # That holds at the shipped defaults (the refresh cadence and the window
    # are both 10) and stops holding the moment an operator sets
    # MEMORY_TRANSCRIPT_MESSAGES lower, or the index write misses twice. A
    # summary spliced over a gap is worse than no summary: it is labelled
    # "earlier" and reads as if it covered everything older, so the messages in
    # the hole are not merely missing, they are denied. Window-only is the
    # honest degradation.
    total = int(
        db.scalar(select(func.count(Message.id)).where(*scope)) or 0
    )
    if covered < total - limit:
        logger.info(
            "thread summary covers %d of %d messages for run %s; sending the "
            "verbatim window alone rather than a summary with a hole in it",
            covered,
            total,
            run.id,
        )
        return window
    return [("earlier", older), *window]


def _complete_with_message(
    run: Run,
    *,
    content: str,
    citations: list[dict[str, object]],
    already_streamed: bool = False,
    citation_report: dict[str, object] | None = None,
    followups: list[dict[str, object]] | None = None,
) -> None:
    """Persist the assistant message and close the run.

    `already_streamed` is set by the agent path, whose deltas were emitted by the
    loop as the model produced them. The `/tool` paths compose their reply here
    with no model behind it, so that text is still chunked for a live feel.

    `citation_report` is the validator's verdict on this exact answer, stored on
    the message so it outlives the event stream. None — every caller but
    `_finish_run` — leaves the column empty, which reads as "not checked" rather
    than "checked and clean".

    `followups` follows exactly the same two-facts convention: None leaves the
    column '' ("never computed", which is correct for a `/tool` reply with no
    retrieval behind it), while [] is the real and common answer "computed, and
    nothing cleared the probe".
    """
    db = SessionLocal()
    try:
        current = db.get(Run, run.id)
        if current is None:
            return
        if current.cancel_requested or current.status == "cancelling":
            current.status = "cancelled"
            current.lease_expires_at = None
            append_event(
                db,
                workspace_id=current.workspace_id,
                run_id=current.id,
                event_type="run.cancelled",
                payload={"status": "cancelled"},
            )
            db.commit()
            return
        for piece in [] if already_streamed else stream_words(content):
            db.refresh(current)
            if current.cancel_requested:
                current.status = "cancelled"
                current.lease_expires_at = None
                append_event(
                    db,
                    workspace_id=current.workspace_id,
                    run_id=current.id,
                    event_type="run.cancelled",
                    payload={"status": "cancelled"},
                )
                db.commit()
                return
            append_event(
                db,
                workspace_id=current.workspace_id,
                run_id=current.id,
                event_type="message.delta",
                payload={"delta": piece},
            )
            db.commit()
        message = Message(
            workspace_id=current.workspace_id,
            conversation_id=current.conversation_id,
            run_id=current.id,
            # Attribute the answer to the member whose turn produced it, so a
            # shared thread can show who said what.
            created_by=current.created_by,
            role="assistant",
            content=content,
            citations_json=json.dumps(citations),
            citation_report_json=(
                json.dumps(citation_report) if citation_report is not None else ""
            ),
            followups_json=(json.dumps(followups) if followups is not None else ""),
        )
        db.add(message)
        db.flush()
        current.status = "completed"
        current.lease_expires_at = None
        append_event(
            db,
            workspace_id=current.workspace_id,
            run_id=current.id,
            event_type="message.completed",
            payload={
                "message_id": message.id,
                "content": content,
                "citations": citations,
                "followups": followups or [],
            },
        )
        append_event(
            db,
            workspace_id=current.workspace_id,
            run_id=current.id,
            event_type="run.completed",
            payload={"status": "completed"},
        )
        # The outbound-webhook chokepoint for chat runs: ids only, no content
        # — the receiver learns a run finished, never what it said. Flushed
        # into this same transaction, committed with the completion.
        webhooks.emit(
            db,
            workspace_id=current.workspace_id,
            event="run.completed",
            payload={
                "run_id": current.id,
                "conversation_id": current.conversation_id,
                "status": "completed",
            },
        )
        db.commit()
    finally:
        db.close()


def _finish_run(
    run: Run,
    *,
    answer: str,
    evidence: list[Evidence],
    already_streamed: bool = True,
) -> None:
    # Takes the evidence list rather than pre-serialized citations so the
    # validator counts from the same object the message is built from. Passing
    # the dicts separately would let the two drift, which is exactly the drift
    # the validator exists to catch.
    # Before _complete_with_message, deliberately: that call emits run.completed,
    # which consumers treat as terminal and stop reading on. An event appended
    # after it is an event nobody sees.
    report, answer = _validated_answer(run, answer, evidence, get_settings())
    # Also before _complete_with_message, and for the same reason: the chips go
    # out on `message.completed`, and anything computed after `run.completed` is
    # computed for nobody.
    followups = _record_followups(run, answer, evidence)
    _record_coverage(run, evidence)
    _complete_with_message(
        run,
        content=answer,
        citations=_citations(evidence),
        already_streamed=already_streamed,
        citation_report=report,
        followups=followups,
    )
    try:
        write_conversation_memory(run.id)
    except Exception:
        # Memory persistence must never fail a completed run — but it must not
        # vanish either. Everything inside write_conversation_memory already
        # logs and rolls back, so reaching here means it could not even open a
        # session; that is worth a line rather than a silent `pass`.
        logger.warning(
            "memory persistence raised for run %s", run.id, exc_info=True
        )
    try:
        # Same contract as the memory write above, and a miss costs even less:
        # search-time reconcile indexes anything this call did not.
        update_conversation_index(run.id)
    except Exception:
        logger.warning(
            "conversation indexing raised for run %s", run.id, exc_info=True
        )


def _record_followups(
    run: Run, answer: str, evidence: list[Evidence]
) -> list[dict[str, object]] | None:
    """The suggested next questions for this answer, or None when we could not.

    Shaped exactly like `_record_citation_report`: its own session, its own
    try/except, and a failure that costs the run nothing. A suggester that can
    fail a completed run is worse than no suggester.

    None and [] are different facts. None becomes '' in the column — "never
    computed", the honest value when the probe itself broke — while [] is
    "computed, and nothing cleared the floor", which is a real and common
    answer for a thin corpus.

    The probe is given BOTH scope axes: the thread's space and the thread
    itself (`retrieval._live_sources`' two axes). A probe missing one would
    offer a chip about a passage this thread cannot retrieve.

    The whole body runs inside a `usage_scope` for the reason the repair path
    states three hundred lines down: runs.py sits OUTSIDE the agent loop's
    scope, which is a ContextVar reset when `run_agent_turn` returns, and
    `_finish_run` runs after that. Without the bind, `polish`'s Responses call
    spends real tokens that `record_model_usage` drops with a "missing
    usage_scope()" warning — invisible to the ledger AND to the spend ceiling,
    which aggregates the same table. The probe's own embedding calls inherit
    the run/conversation/user attribution from here too; `usage_scope` merges
    blank fields rather than clearing them, so retrieval's inner bind keeps its
    own EMBEDDING operation.
    """
    db = SessionLocal()
    try:
        settings = get_settings()
        space_id = spaces.space_id_for_conversation(
            db, workspace_id=run.workspace_id, conversation_id=run.conversation_id
        )
        with usage.usage_scope(
            workspace_id=run.workspace_id,
            run_id=run.id,
            conversation_id=run.conversation_id,
            user_id=run.created_by,
            agent_id=run.agent_id or "",
            operation=usage.FOLLOWUP_POLISH,
        ):
            items = followups.suggest(
                db,
                workspace_id=run.workspace_id,
                answer=answer,
                evidence=evidence,
                space_id=space_id,
                conversation_id=run.conversation_id,
                settings=settings,
            )
            items = followups.polish(
                db,
                items,
                workspace_id=run.workspace_id,
                user_id=run.created_by,
                space_id=space_id,
                conversation_id=run.conversation_id,
                settings=settings,
            )
        return followups.serialize(items)
    except Exception:
        logger.warning(
            "follow-up suggestion raised for run %s", run.id, exc_info=True
        )
        return None
    finally:
        db.close()


def _record_coverage(run: Run, evidence: list[Evidence]) -> None:
    """The coverage ledger for a plan-mode turn: what it set out to cover, and
    what it read.

    THE PRODUCER `services/coverage.py` was written for, and until now did not
    have: nothing in the app called `open_ledger`, so `GET /api/runs/{id}/
    coverage` could only 404 and the drawer under every answer could only say
    "No coverage recorded for this run" — a shipped classifier, counter-
    evidence pass, renderer, route, drawer and two tables, fed by nobody. The
    suite stayed green because every test hand-built the writer production
    lacked.

    HERE rather than inside `step_plan`'s executors, and that placement is the
    whole reason this is honest. A `complete_step` call knows the sub-question
    and the ids the model chose to name; it does NOT know what retrieval
    actually returned during that step, because the searches happened several
    tool calls earlier inside the agent loop and no row attributes a passage to
    a step. `_finish_run` holds the turn's whole evidence list, so the
    ledger's numerator can be what the run really read rather than what the
    model remembered to mention. The per-step entries still carry the model's
    own attribution, which is why the renderer says "no passage was recorded
    for this" and not "nothing in scope supported this".

    Only for a turn that submitted a plan. An ordinary turn has no declared
    sub-questions, and inventing them from the prompt would be the report
    making up its own subject.

    Never fails the run: same contract as the follow-up and memory writes
    above, and a coverage report is worth strictly less than an answer.
    """
    db = SessionLocal()
    try:
        steps = step_plan.completed_steps(db, run.workspace_id, run.id)
        if not steps:
            return
        space_id = spaces.space_id_for_conversation(
            db, workspace_id=run.workspace_id, conversation_id=run.conversation_id
        )
        by_chunk = {item.chunk_id: item for item in evidence if item.chunk_id}
        ledger = coverage.open_ledger(
            db,
            workspace_id=run.workspace_id,
            run_id=run.id,
            question=run.prompt,
            space_id=space_id,
            conversation_id=run.conversation_id,
        )
        for step in steps:
            coverage.record_step(
                db,
                ledger=ledger,
                sub_question=step.question,
                # Matched against the turn's OWN evidence, so an id the model
                # invented attributes nothing, and an id it named twice counts
                # once.
                evidence=[
                    by_chunk[chunk_id]
                    for chunk_id in dict.fromkeys(step.chunk_ids)
                    if chunk_id in by_chunk
                ],
            )
        coverage.counter_evidence_pass(
            db,
            ledger=ledger,
            space_id=space_id,
            conversation_id=run.conversation_id,
        )
        coverage.close_ledger(db, ledger=ledger, consulted_evidence=evidence)
        db.commit()
    except Exception:
        db.rollback()
        logger.warning(
            "coverage ledger raised for run %s", run.id, exc_info=True
        )
    finally:
        db.close()


def _validated_answer(
    run: Run, answer: str, evidence: list[Evidence], settings: Settings
) -> tuple[dict[str, object] | None, str]:
    """Grade the answer, optionally repair it, record ONE verdict. (payload, answer).

    Recorded on every completed run, not only on violations: a
    hallucinated-citation *rate* needs a denominator, and "runs where evidence
    was supplied" only exists if the clean ones are logged too.

    The verdict is returned so the caller can store it on the message it is
    about. An event and an audit row are both places a reader never goes; the
    message is the one surface where a fabricated `[4]` can still be caught.
    `None` means the grader itself failed, which is not a verdict — and the
    ORIGINAL answer comes back with it, because a run must never depend on its
    checker having worked. A validator that can break the thing it watches is
    worse than no validator.

    The one thing that changed when the repair pass landed: this function may
    now return a DIFFERENT answer than it was given. Exactly one event and one
    audit row are still written, and both carry the FINAL payload — the verdict
    on the text that is actually stored, never on a draft that was discarded.
    """
    try:
        payload = grounded.compose_report(
            answer, evidence, floor=settings.grounding_floor
        )
        grounding = payload.get("grounding")
        repaired, repair_payload = _repair_unsupported(
            run,
            answer,
            evidence,
            grounding if isinstance(grounding, dict) else {},
            settings,
        )
        if repaired != answer:
            answer = repaired
            payload = grounded.compose_report(
                answer, evidence, floor=settings.grounding_floor
            )
        # What the repair could not see. The grader stops at
        # `citations._MAX_SENTENCES`, and the repair aims only at sentences the
        # grader flagged — so on a long answer "nothing to repair" means
        # "nothing to repair in the part that was read", and the record has to
        # say which of the two it is.
        repair_payload["tail_ungraded"] = bool(
            isinstance(grounding, dict) and grounding.get("sentences_truncated")
        )
        payload["repair"] = repair_payload
        db = SessionLocal()
        try:
            append_event(
                db,
                workspace_id=run.workspace_id,
                run_id=run.id,
                event_type="run.citations",
                payload=payload,
            )
            record_audit(
                db,
                workspace_id=run.workspace_id,
                actor_id=run.created_by,
                action="run.citations_validated",
                resource_type="run",
                resource_id=run.id,
                detail=_audit_detail(payload),
            )
            db.commit()
        finally:
            db.close()
        return payload, answer
    except Exception:
        logger.warning(
            "citation validation raised for run %s", run.id, exc_info=True
        )
        return None, answer


def _audit_detail(payload: dict[str, object]) -> dict[str, object]:
    """The verdict as the AUDIT TRAIL may hold it: identifiers and numbers.

    The same doctrine `api/answers.py` states for the grounded route — "the
    question and the answer live on the receipt, which is owner-read; the audit
    trail is read far more widely and has never carried content". The composed
    payload now embeds `grounding.sentences`, each with the sentence's own
    text, so passing it straight through would publish up to 120 clipped
    sentences of every answer into a feed any workspace member can read and an
    owner can export off-box. The per-sentence array keeps riding the
    `run.citations` event and `messages.citation_report_json`, both of which
    are scoped to the thread the answer is in.
    """
    grounding = payload.get("grounding")
    grounding = grounding if isinstance(grounding, dict) else {}
    detail: dict[str, object] = {
        key: payload.get(key)
        for key in (
            "valid",
            "evidence_count",
            "marker_count",
            "out_of_range",
            "uncited",
            # Bracket groups only — digits and separators, never prose, by
            # `_CANDIDATE_RE`'s own character class — and what the pre-cycle
            # audit row carried.
            "malformed",
            "summary",
            "repair",
        )
    }
    detail["grounding"] = {
        key: grounding.get(key)
        for key in (
            "score",
            "scored",
            "verified",
            "cited_unsupported",
            "uncited",
            "ignored",
            "attributed",
            "floor",
            "n_sentences",
            "sentences_truncated",
        )
    }
    return detail


def _repair_payload(
    *,
    attempted: bool,
    applied: bool,
    reason: str,
    score_before: float = 0.0,
    score_after: float = 0.0,
    unsupported_before: int = 0,
    unsupported_after: int = 0,
) -> dict[str, object]:
    return {
        "attempted": attempted,
        "applied": applied,
        "reason": reason,
        "score_before": score_before,
        "score_after": score_after,
        "unsupported_before": unsupported_before,
        "unsupported_after": unsupported_after,
    }


#: How much of the answer a rewrite must still be carrying, measured against
#: what is left once the flagged sentences are removed. The repair is allowed
#: to DROP a claim the passages do not support — its own instructions say so —
#: so the yardstick is the unflagged material, not the original length.
MIN_REPAIR_LENGTH_RATIO = 0.8


def _content_preserved(
    answer: str,
    candidate: str,
    *,
    flagged: tuple[str, ...],
    scored_before: int,
    unsupported_before: int,
    regraded_scored: int,
) -> bool:
    """Does this rewrite still contain the answer, or only the front of it?

    The acceptance test this sits beside asks whether the candidate is better.
    Truncation passes that test trivially: cut an answer in half and the
    unsupported sentences in the lost half go with it, so the score rises and
    the unsupported count falls while the user watches two thirds of what they
    read disappear. `validate_citations` does not object either — an answer
    that cites nothing is valid, and stranded passages are explicitly not a
    violation.

    Two independent checks, because they fail in different directions:

    * the graded-sentence count may fall by AT MOST the number of sentences
      that were flagged. A rewrite may delete an unsupported claim; it may not
      delete a supported one. (Short and attributed sentences are outside
      `scored`, which is why the length check below exists as well.)
    * what is left must still be roughly as long as the unflagged material.
      That catches a cut through a tail of sentences too short to be scored.
    """
    if regraded_scored < scored_before - unsupported_before:
        return False
    allowance = len(answer.strip()) - sum(len(sentence) for sentence in flagged)
    return len(candidate.strip()) >= MIN_REPAIR_LENGTH_RATIO * max(allowance, 0)


def _repair_unsupported(
    run: Run,
    answer: str,
    evidence: list[Evidence],
    grounding: dict[str, Any],
    settings: Settings,
) -> tuple[str, dict[str, object]]:
    """One bounded, honest rewrite of the cited-but-unsupported sentences.

    Inline, BEFORE `_complete_with_message`, because there is nowhere later to
    put it: `run.completed` is terminal and nothing after it is read. That is
    safe here for two reasons that have to stay true.

    * The browser rebuilds the whole message from the `message.completed`
      payload and drops the streamed `temporaryId`
      (`apps/web/components/handlers/thread.ts`), so the streamed deltas are a
      preview and the completed content is authoritative. A reader watching a
      sentence get replaced is the known cost, listed in the design risks.
    * It cannot disturb approvals. `_finish_run` is reached only with a
      terminal `AgentResult` — `process_run`, `resume_run` and
      `resume_run_after_budget` all return early on `None` — and the repair
      call passes NO tools, so it can neither propose one nor park.

    It fires at most once per run, only when there is something to fix, and it
    is accepted only when re-grading says it is strictly better: non-empty,
    every marker resolving, still carrying the answer (`_content_preserved`), a
    score that did not fall, and fewer unsupported sentences than before.
    Anything else keeps the original — a "repair" that made the answer worse,
    that invented a citation while fixing one, or that returned the first third
    of the answer and called it corrected, is the single failure mode that
    would make this feature negative value.

    Never raises. Returns `(answer, repair_payload)`; the payload rides the
    existing `run.citations` event, so no new event type and no browser change.
    """
    before = float(grounding.get("score") or 0.0)
    unsupported_before = int(grounding.get("cited_unsupported") or 0)
    if not settings.grounding_repair_enabled:
        return answer, _repair_payload(attempted=False, applied=False, reason="disabled")
    if not evidence or unsupported_before < 1:
        return answer, _repair_payload(
            attempted=False,
            applied=False,
            reason="nothing_to_repair",
            score_before=before,
            score_after=before,
            unsupported_before=unsupported_before,
            unsupported_after=unsupported_before,
        )
    if unsupported_before > settings.grounding_repair_max_sentences:
        # A rewrite asked to fix half an answer is a regeneration, and that is
        # not a thing a validator may do behind the user's back.
        return answer, _repair_payload(
            attempted=False,
            applied=False,
            reason="nothing_to_repair",
            score_before=before,
            score_after=before,
            unsupported_before=unsupported_before,
            unsupported_after=unsupported_before,
        )

    flagged = grounded.flagged_sentences(grounding)
    try:
        # runs.py sits OUTSIDE the agent loop's scope, so without this bind the
        # ledger row is dropped with usage.py's "missing usage_scope()" warning
        # and the repair spends tokens nobody can attribute.
        with usage.usage_scope(
            workspace_id=run.workspace_id,
            run_id=run.id,
            conversation_id=run.conversation_id,
            user_id=run.created_by,
            agent_id=run.agent_id or "",
            operation=usage.GROUNDING_REPAIR,
        ):
            candidate = model.regenerate_unsupported(
                answer,
                evidence,
                flagged,
                user_id=run.created_by,
                settings=settings,
            )
    except Exception:
        logger.warning("grounding repair raised for run %s", run.id, exc_info=True)
        return answer, _repair_payload(
            attempted=True,
            applied=False,
            reason="failed",
            score_before=before,
            score_after=before,
            unsupported_before=unsupported_before,
            unsupported_after=unsupported_before,
        )

    if not candidate.strip():
        # "" is what every provider but openai returns, by design: there is no
        # offline stand-in for a rewrite, so a scripted deployment reports that
        # the stage did not run rather than pretending it did.
        reason = (
            "no_provider" if settings.active_model_provider != "openai" else "failed"
        )
        return answer, _repair_payload(
            attempted=settings.active_model_provider == "openai",
            applied=False,
            reason=reason,
            score_before=before,
            score_after=before,
            unsupported_before=unsupported_before,
            unsupported_after=unsupported_before,
        )

    if not validate_citations(candidate, evidence).is_valid:
        return answer, _repair_payload(
            attempted=True,
            applied=False,
            reason="rejected_fabricated",
            score_before=before,
            score_after=before,
            unsupported_before=unsupported_before,
            unsupported_after=unsupported_before,
        )

    regraded = grade_grounding(
        candidate,
        [item.excerpt for item in evidence],
        floor=settings.grounding_floor,
        # The same flags the original verdict was measured with. Without them
        # the before/after comparison is made on two different scales, and the
        # rewrite is accepted or rejected against a number that means something
        # else.
        checkable=grounded.checkable_flags(evidence),
    )
    kept = _content_preserved(
        answer,
        candidate,
        flagged=flagged,
        scored_before=int(grounding.get("scored") or 0),
        unsupported_before=unsupported_before,
        regraded_scored=regraded.scored,
    )
    if not kept:
        return answer, _repair_payload(
            attempted=True,
            applied=False,
            reason="rejected_truncated",
            score_before=before,
            score_after=regraded.score,
            unsupported_before=unsupported_before,
            unsupported_after=regraded.cited_unsupported,
        )
    if regraded.score < before or regraded.cited_unsupported >= unsupported_before:
        return answer, _repair_payload(
            attempted=True,
            applied=False,
            reason="rejected_not_better",
            score_before=before,
            score_after=regraded.score,
            unsupported_before=unsupported_before,
            unsupported_after=regraded.cited_unsupported,
        )
    return candidate, _repair_payload(
        attempted=True,
        applied=True,
        reason="",
        score_before=before,
        score_after=regraded.score,
        unsupported_before=unsupported_before,
        unsupported_after=regraded.cited_unsupported,
    )


def _fail_run(db, run_id: str, exc: Exception) -> None:
    # The whole detail, kept where operators can find it and nowhere else. This
    # is the only place it survives, so it is logged before anything can go
    # wrong with writing the row below.
    logger.error("run %s failed", run_id, exc_info=exc)
    db.rollback()
    run = db.get(Run, run_id)
    if run is None or run.status in TERMINAL_RUN_STATES:
        return
    run.status = "failed"
    # Never `str(exc)`. This field is published twice — into the `run.failed`
    # event the browser streams, and into the member-facing Inbox — so a driver
    # error put here becomes SQL, bound parameters and row ids on a user's
    # screen. `user_facing_message` passes through only the messages written to
    # be read, and says something honest about everything else.
    run.error = user_facing_message(exc, run_id=run_id)
    run.paused_reason = ""
    run.lease_expires_at = None
    run.agent_state_json = None
    append_event(
        db,
        workspace_id=run.workspace_id,
        run_id=run.id,
        event_type="run.failed",
        payload={"status": "failed", "error": run.error},
    )
    db.commit()


def resume_run(
    run_id: str,
    tool_call_id: str,
    decision: str,
    amendment: Optional[dict[str, Any]] = None,
    inputs: Optional[dict[str, Any]] = None,
) -> None:
    """Continue a run parked on a tool approval, after the user decided.

    `amendment` carries a partial approval — the subset of a document edit's
    hunks the reviewer accepted — through to the executor. `inputs` carries the
    values a person typed at a workflow `manual` node; both are ignored by the
    path they do not belong to.
    """
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if run is None or run.status != "waiting_for_approval":
            return
        run.lease_expires_at = utcnow() + timedelta(
            seconds=get_settings().run_lease_seconds
        )
        append_event(
            db,
            workspace_id=run.workspace_id,
            run_id=run.id,
            event_type="run.started",
            payload={"status": "running"},
        )
        db.commit()
        result = resume_agent_turn(
            db,
            run,
            tool_call_id=tool_call_id,
            decision=decision,
            amendment=amendment,
            inputs=inputs,
        )
        if result is None:
            return
        _finish_run(run, answer=result.answer, evidence=result.evidence)
    except Exception as exc:
        _fail_run(db, run_id, exc)
    finally:
        db.close()


def resume_run_after_budget(run_id: str) -> None:
    """Continue a run parked on the spend ceiling, after an owner raised it.

    The sibling of `resume_run`, and it guards on `paused_reason` as well as
    status so a release can never be applied to a run parked on an *approval* —
    that one is waiting on a decision this path does not have and must not
    invent.

    If the ceiling is still exceeded the loop parks it straight back; the
    enforcement predicate is consulted in exactly one place and this is not it.
    """
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if run is None or run.status != "waiting_for_approval":
            return
        if run.paused_reason != PAUSED_FOR_BUDGET:
            return
        run.lease_expires_at = utcnow() + timedelta(
            seconds=get_settings().run_lease_seconds
        )
        append_event(
            db,
            workspace_id=run.workspace_id,
            run_id=run.id,
            event_type="run.started",
            payload={"status": "running"},
        )
        db.commit()
        result = resume_after_budget(db, run)
        if result is None:
            return
        _finish_run(run, answer=result.answer, evidence=result.evidence)
    except Exception as exc:
        _fail_run(db, run_id, exc)
    finally:
        db.close()


def process_run(run_id: str) -> None:
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if run is None or run.status not in {"queued", "running"}:
            return
        if run.cancel_requested:
            run.status = "cancelled"
            run.lease_expires_at = None
            append_event(
                db,
                workspace_id=run.workspace_id,
                run_id=run.id,
                event_type="run.cancelled",
                payload={"status": "cancelled"},
            )
            db.commit()
            return
        run.status = "running"
        run.lease_expires_at = utcnow() + timedelta(
            seconds=get_settings().run_lease_seconds
        )
        append_event(
            db,
            workspace_id=run.workspace_id,
            run_id=run.id,
            event_type="run.started",
            payload={"status": "running"},
        )
        db.commit()

        tool_name = parse_tool_prompt(run.prompt)
        if tool_name:
            tool = db.scalar(
                select(Tool)
                .join(ToolGrant, ToolGrant.tool_id == Tool.id)
                .where(
                    Tool.workspace_id == run.workspace_id,
                    ToolGrant.workspace_id == run.workspace_id,
                    ToolGrant.agent_id == run.agent_id,
                    Tool.name == tool_name,
                    Tool.enabled.is_(True),
                )
            )
            if tool is None:
                _complete_with_message(
                    run,
                    content=(
                        "That agent is not granted a tool named “"
                        + tool_name
                        + "”. Try `/tool github-zen` in the demo workspace."
                    ),
                    citations=[],
                )
                return
            call = ToolCall(
                workspace_id=run.workspace_id,
                run_id=run.id,
                tool_id=tool.id,
                status="proposed" if tool.requires_approval else "approved",
                request_url=tool.base_url,
            )
            db.add(call)
            db.flush()
            if tool.requires_approval:
                run.status = "waiting_for_approval"
                run.lease_expires_at = None
                append_event(
                    db,
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    event_type="tool.proposed",
                    payload={
                        "tool_call_id": call.id,
                        "tool_name": tool.name,
                        "description": tool.description,
                        "request_url": call.request_url,
                    },
                )
                append_event(
                    db,
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    event_type="run.waiting_for_approval",
                    payload={"status": "waiting_for_approval"},
                )
                record_audit(
                    db,
                    workspace_id=run.workspace_id,
                    actor_id=run.created_by,
                    action="tool.proposed",
                    resource_type="tool_call",
                    resource_id=call.id,
                    detail={"tool": tool.name, "url": call.request_url},
                )
                db.commit()
                return
            db.commit()
            execute_tool_call(call.id)
            return

        # Resolved once for the turn: retrieval and recall must agree on the
        # scope, and a deleted or foreign space degrades to "" in the resolver.
        space_id = spaces.space_id_for_conversation(
            db, workspace_id=run.workspace_id, conversation_id=run.conversation_id
        )
        # The thread's own scope, alongside the space's. Files attached to this
        # conversation are retrievable here and nowhere else; the workspace
        # library is retrievable everywhere. See `retrieval._live_sources`.
        #
        # RELEVANCE-GATED, and the gate lives one level down: `search_evidence`
        # drops everything under `retrieval_fused_floor`, so this preload can
        # legitimately come back empty for a question the corpus does not
        # cover. That is the intended outcome — "what's for lunch" used to
        # arrive with five citations attached — and the `retrieval.completed`
        # event below says count 0 rather than being skipped, because "looked
        # and found nothing" and "never looked" are different facts.
        # The turn's own retrieval budget, read off the row the send endpoint
        # expanded. "" resolves to MEDIUM, whose three numbers are exactly this
        # call's previous defaults — so an unpresetted turn retrieves what it
        # always did.
        turn_budget = budget_for(run.retrieval_budget)
        evidence = search_evidence(
            db,
            workspace_id=run.workspace_id,
            query=run.prompt,
            space_id=space_id,
            conversation_id=run.conversation_id,
            limit=turn_budget.limit,
            token_budget=turn_budget.token_budget,
            per_passage_tokens=turn_budget.per_passage_tokens,
        )
        citations = _citations(evidence)
        append_event(
            db,
            workspace_id=run.workspace_id,
            run_id=run.id,
            event_type="retrieval.completed",
            payload={
                "count": len(citations),
                "citations": citations,
            },
        )
        db.commit()
        settings = get_settings()
        transcript = _transcript(db, run)
        memory_context = ""
        # When the member opted out or the thread is incognito, memory_context
        # stays "" and NO memory.recalled event is emitted: silence says memory
        # did not run, where a count-0 event would say it did.
        if settings.memory_enabled and memory_opted_in(
            db,
            workspace_id=run.workspace_id,
            user_id=run.created_by,
            conversation_id=run.conversation_id,
        ):
            context = recall(
                db,
                workspace_id=run.workspace_id,
                conversation_id=run.conversation_id,
                query=run.prompt,
                # Whose turn this is. The member sees the workspace's memories
                # and their own; another member's personal memories are not
                # candidates for this prompt. ADR 0010.
                viewer_id=run.created_by,
                settings=settings,
            )
            memory_context = render_memory_context(context)
            append_event(
                db,
                workspace_id=run.workspace_id,
                run_id=run.id,
                event_type="memory.recalled",
                payload={
                    "count": len(context.items),
                    "items": [
                        {
                            "id": item.id,
                            "kind": item.kind,
                            "content": item.content,
                        }
                        for item in context.items
                    ],
                    "graph_digest": context.graph_digest,
                },
            )
            db.commit()
        result = run_agent_turn(
            db,
            run,
            evidence=evidence,
            transcript=transcript,
            memory_context=memory_context,
            settings=settings,
        )
        # None means the loop parked for a tool approval or was cancelled;
        # either way the run is not ours to complete right now.
        if result is None:
            return
        _finish_run(run, answer=result.answer, evidence=result.evidence)
    except Exception as exc:
        _fail_run(db, run_id, exc)
    finally:
        db.close()


def execute_tool_call(tool_call_id: str) -> None:
    db = SessionLocal()
    try:
        call = db.get(ToolCall, tool_call_id)
        if call is None or call.status != "approved":
            return
        run = db.get(Run, call.run_id)
        if run is None or run.cancel_requested:
            return
        # Carried on every event this call emits. The agent loop has always named
        # its tool; this path did not, so a chat watching the stream could say
        # only "a tool failed" — and a user cannot act on a failure they cannot
        # name. Filtered by workspace rather than fetched by primary key: the id
        # comes off a row, and a name read across a tenant boundary would be
        # printed straight into another workspace's event stream.
        tool = db.scalar(
            select(Tool).where(
                Tool.id == call.tool_id, Tool.workspace_id == run.workspace_id
            )
        )
        tool_name = tool.name if tool is not None else ""
        call.status = "executing"
        run.status = "running"
        run.lease_expires_at = utcnow() + timedelta(
            seconds=get_settings().run_lease_seconds
        )
        append_event(
            db,
            workspace_id=run.workspace_id,
            run_id=run.id,
            event_type="tool.started",
            payload={"tool_call_id": call.id, "tool_name": tool_name},
        )
        db.commit()
        try:
            status_code, body = execute_read_only_get(call.request_url, get_settings())
            call.status = "succeeded"
            call.response_status = status_code
            call.response_body = body
            append_event(
                db,
                workspace_id=run.workspace_id,
                run_id=run.id,
                event_type="tool.completed",
                payload={
                    "tool_call_id": call.id,
                    "tool_name": tool_name,
                    "status_code": status_code,
                    "preview": body[:500],
                },
            )
            record_audit(
                db,
                workspace_id=run.workspace_id,
                actor_id=run.created_by,
                action="tool.executed",
                resource_type="tool_call",
                resource_id=call.id,
                detail={"status_code": status_code},
            )
            db.commit()
            response = (
                "The approved read-only tool returned HTTP "
                + str(status_code)
                + ":\n\n```\n"
                + body[:1500]
                + "\n```"
            )
            _complete_with_message(run, content=response, citations=[])
        except (ToolSecurityError, httpx.HTTPError, OSError) as exc:
            call.status = "failed"
            call.error = str(exc)[:1000]
            run.status = "failed"
            run.error = call.error
            run.lease_expires_at = None
            append_event(
                db,
                workspace_id=run.workspace_id,
                run_id=run.id,
                event_type="tool.failed",
                payload={
                    "tool_call_id": call.id,
                    "tool_name": tool_name,
                    "error": call.error,
                },
            )
            append_event(
                db,
                workspace_id=run.workspace_id,
                run_id=run.id,
                event_type="run.failed",
                payload={"status": "failed", "error": call.error},
            )
            db.commit()
    finally:
        db.close()


def deny_tool_call(tool_call_id: str) -> None:
    db = SessionLocal()
    try:
        call = db.get(ToolCall, tool_call_id)
        if call is None:
            return
        run = db.get(Run, call.run_id)
        if run is None:
            return
        content = "The proposed tool call was denied. No external request was made."
        _complete_with_message(run, content=content, citations=[])
    finally:
        db.close()
