"""The grounded-answer API: one question in, an answer with its evidence out.

A genuine machine door, on the `/api/hooks/*` posture exactly — an
`Authorization: Bearer grain_…` workspace token resolved through
`auth.get_token_actor`, never a cookie — because the caller is a script, not a
browser, and a bearer header is itself the CSRF defence.

What makes it different from every other answer this app produces: it returns
the *verdict alongside the answer*. The citations, the per-sentence grounding
report and the embedding contract the retrieval ran on all come back in the
same payload, so a caller can decide what to do with a sentence whose words are
not in the passage it cites without having to re-derive any of it. That verdict
is a LEXICAL support test — the words are there — and never a claim that the
answer is true; `schemas.GroundingCheck` says so in the field docs the OpenAPI
publishes.

Every call leaves a receipt. `model_usage` already meters what the call cost
(`operation="grounded_answer"`), and deliberately holds no content; the receipt
is the other half — what was asked, what was said, and what the grader made of
it — which is why it is workspace-scoped and read by owners only, on the same
API & Webhooks surface the tokens are minted from.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, require_owner
from ..database import get_db
from ..models import GroundedReceipt
from ..schemas import ApiModel, Citation, CitationCheck
from ..services import grounded, usage
from ..services.audit import record_audit

# The one builder for a citation payload. Imported rather than re-implemented:
# a second copy of this shape is how the transcript's citations and the API's
# would come to disagree about a field (the `url` a web source needs, most
# recently), which is the drift the citation validator exists to catch.
from ..services.runs import _citations as citation_dicts
from ..services.usage import usage_scope
from .ratelimit import public_rate_limit, token_rate_limit

router = APIRouter(prefix="/api", tags=["answers"])

#: How much of a question the LIST view shows. The list is a ledger a person
#: scans; the whole question (and the answer) is one row away.
LIST_QUESTION_CHARS = 200


class GroundedAnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    #: The space to answer inside. "" is the documented global scope: the whole
    #: workspace library.
    space_id: str = Field(default="", max_length=36)
    #: An allow-list of sources, pushed INTO the query (see
    #: `services.grounded.answer_grounded`), so the ranking happens inside the
    #: list and naming the one source that answers the question returns its
    #: best passage rather than nothing. An id this workspace does not own is
    #: dropped silently rather than refused — an allow-list must not double as
    #: an existence probe — and a list where NONE of the ids resolves answers
    #: from nothing rather than from everything.
    source_ids: List[str] = Field(default_factory=list, max_length=25)
    limit: int = Field(default=5, ge=1, le=10)
    #: Ask for structured output. Non-strict, so an unusual but valid schema
    #: comes back as `schema_error` rather than a request-time failure.
    json_schema: Optional[Dict[str, Any]] = None


class GroundedAnswerOut(ApiModel):
    receipt_id: str
    question: str
    answer: str
    #: The passages the answer was built from, in the order its `[n]` markers
    #: number them — the same shape the chat transcript uses, `url` included.
    citations: List[Citation] = []
    report: CitationCheck
    #: The embedding contract retrieval ran on, "" when none is active. Two
    #: answers to the same question under two contracts are two retrievals.
    embedding_generation_id: str = ""
    structured: Optional[Any] = None
    schema_error: str = ""
    created_at: datetime


class GroundedReceiptOut(ApiModel):
    """One row of the ledger. No answer body — that is the detail route's job."""

    id: str
    question: str
    evidence_count: int
    grounding_score: float
    #: How many sentences were gradable. Carried because the score alone cannot
    #: be rendered honestly without it: `GroundingCheck` says "`scored == 0`
    #: means nothing was gradable here — which is not 'ungrounded', and must
    #: not be rendered as a 0%", and a row holding only `grounding_score` has
    #: no field left to tell "graded and failed" from "nothing to grade".
    scored: int = 0
    valid: bool
    embedding_generation_id: str
    created_at: datetime


class GroundedReceiptDetailOut(ApiModel):
    id: str
    question: str
    answer: str
    citations: List[Citation] = []
    report: Optional[CitationCheck] = None
    space_id: str
    embedding_generation_id: str
    created_at: datetime


@router.post(
    "/answers/grounded",
    response_model=GroundedAnswerOut,
    # Listed FIRST, and before the token resolver, so invalid-token flooding is
    # throttled per IP before an Actor exists to key on.
    dependencies=[Depends(public_rate_limit("grounded-answer"))],
)
def grounded_answer(
    payload: GroundedAnswerRequest,
    actor: Actor = Depends(token_rate_limit("grounded-answer", tier="heavy")),
    db: Session = Depends(get_db),
) -> GroundedAnswerOut:
    """Answer one question from the token workspace's own indexed sources.

    Synchronous and metered: the `usage_scope` below wraps both the query
    embedding and the model call, so the whole cost of this request lands in
    `model_usage` under one operation and "what did the grounded API cost" is a
    query rather than an estimate.

    No repair pass here, deliberately. The chat path may spend a second model
    call rewriting unsupported sentences because a person is reading the result;
    doing that silently inside a metered synchronous API would double its
    latency and its caller's bill to fix something the response already reports.
    """
    with usage_scope(
        workspace_id=actor.workspace_id,
        user_id=actor.user_id,
        operation=usage.GROUNDED_ANSWER,
    ):
        result = grounded.answer_grounded(
            db,
            workspace_id=actor.workspace_id,
            user_id=actor.user_id,
            question=payload.question,
            space_id=payload.space_id,
            source_ids=payload.source_ids,
            limit=payload.limit,
            json_schema=payload.json_schema,
        )

    citations = citation_dicts(result.evidence)
    grounding = result.report.get("grounding")
    score = float(grounding.get("score") or 0.0) if isinstance(grounding, dict) else 0.0
    receipt = GroundedReceipt(
        workspace_id=actor.workspace_id,
        token_id=actor.token_id,
        user_id=actor.user_id,
        space_id=payload.space_id,
        question=payload.question,
        answer=result.answer,
        citations_json=json.dumps(citations),
        report_json=json.dumps(result.report),
        schema_json=json.dumps(payload.json_schema) if payload.json_schema else "",
        structured_json=(
            json.dumps(result.structured) if result.structured is not None else ""
        ),
        generation_id=result.generation_id,
        evidence_count=len(result.evidence),
        grounding_score=score,
    )
    db.add(receipt)
    db.flush()
    record_audit(
        db,
        workspace_id=actor.workspace_id,
        actor_id=actor.user_id,
        action="answers.grounded",
        resource_type="grounded_receipt",
        resource_id=receipt.id,
        # Identifiers and numbers only. The question and the answer live on the
        # receipt, which is owner-read; the audit trail is read far more widely
        # and has never carried content.
        detail={
            "evidence_count": len(result.evidence),
            "score": score,
            "valid": bool(result.report.get("valid")),
        },
    )
    db.commit()
    return GroundedAnswerOut(
        receipt_id=receipt.id,
        question=payload.question,
        answer=result.answer,
        citations=[Citation.model_validate(item) for item in citations],
        report=CitationCheck.model_validate(result.report),
        embedding_generation_id=result.generation_id,
        structured=result.structured,
        schema_error=result.schema_error,
        created_at=receipt.created_at,
    )


@router.get("/answers/receipts", response_model=List[GroundedReceiptOut])
def list_grounded_receipts(
    limit: int = 50,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> List[GroundedReceiptOut]:
    """This workspace's grounded-answer ledger, newest first.

    Owner-gated like the tokens that produce it: a receipt holds the answer
    text, and the credential that made it is minted by an owner.
    """
    window = max(1, min(limit, 200))
    rows = db.scalars(
        select(GroundedReceipt)
        .where(GroundedReceipt.workspace_id == actor.workspace_id)
        .order_by(GroundedReceipt.created_at.desc(), GroundedReceipt.id)
        .limit(window)
    )
    return [
        GroundedReceiptOut(
            id=row.id,
            question=row.question[:LIST_QUESTION_CHARS],
            evidence_count=row.evidence_count,
            grounding_score=row.grounding_score,
            scored=_scored(row.report_json),
            valid=_valid(row.report_json),
            embedding_generation_id=row.generation_id,
            created_at=row.created_at,
        )
        for row in rows
    ]


@router.get("/answers/receipts/{receipt_id}", response_model=GroundedReceiptDetailOut)
def get_grounded_receipt(
    receipt_id: str,
    actor: Actor = Depends(require_owner),
    db: Session = Depends(get_db),
) -> GroundedReceiptDetailOut:
    """One receipt, with its answer and its verdict.

    Selected on BOTH the id and the workspace rather than fetched by primary
    key: a foreign receipt must be indistinguishable from one that does not
    exist, and the two-clause select is also what keeps the `db.get` tripwire in
    test_tenant_isolation.py silent without an allowlist entry.
    """
    row = db.scalar(
        select(GroundedReceipt).where(
            GroundedReceipt.id == receipt_id,
            GroundedReceipt.workspace_id == actor.workspace_id,
        )
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    return GroundedReceiptDetailOut(
        id=row.id,
        question=row.question,
        answer=row.answer,
        citations=[
            Citation.model_validate(item)
            for item in (_json(row.citations_json) or [])
        ],
        report=_report(row.report_json),
        space_id=row.space_id,
        embedding_generation_id=row.generation_id,
        created_at=row.created_at,
    )


def _valid(raw: str) -> bool:
    """Whether the stored verdict said every marker resolved. False when ungraded."""
    stored = _json(raw)
    return bool(stored.get("valid")) if isinstance(stored, dict) else False


def _scored(raw: str) -> int:
    """How many sentences the stored verdict graded. 0 when ungraded."""
    stored = _json(raw)
    if not isinstance(stored, dict):
        return 0
    grounding = stored.get("grounding")
    if not isinstance(grounding, dict):
        return 0
    try:
        return int(grounding.get("scored") or 0)
    except (TypeError, ValueError):
        return 0


def _json(raw: str) -> Any:
    """A stored JSON column, or None. Never raises: see `_report`."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _report(raw: str) -> Optional[CitationCheck]:
    """The stored verdict, or None when there is not a usable one.

    Never raises, the same rule `api/chat.py._citation_report` holds: a receipt
    whose grader crashed has no verdict, and a 500 because a *diagnostic* could
    not be parsed would be strictly worse than a row with no badge.
    """
    stored = _json(raw)
    if not isinstance(stored, dict):
        return None
    try:
        return CitationCheck.model_validate(stored)
    except ValueError:
        return None
