"""The coverage ledger's read surface: one route, one resolution rule.

The Run is resolved under the caller's workspace FIRST, so a foreign run 404s
before the ledger question is even asked. A legitimate own run with no ledger
404s with the SAME message, deliberately: "this run recorded no coverage" and
"that run is not yours" must be indistinguishable from outside, or the 404 that
protects the second leaks the first.

There is no `GET /api/deliverables/{id}/coverage`. A manifest's detail response
embeds its ledger instead — one fetch, and one place where the embedding shape
is decided.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Actor, get_actor
from ..database import get_db
from ..models import CoverageEntry, CoverageLedger, Run
from ..schemas import ApiModel
from ..services import coverage as service

router = APIRouter(tags=["coverage"])


class CoverageEntryOut(ApiModel):
    ordinal: int
    sub_question: str
    #: "neutral" | "for" | "against". The two stances are the forced
    #: counter-evidence pass; everything a producer posed is neutral.
    stance: str
    #: Retrieval returned something. NOT entailment — nothing here claims a
    #: passage supports a claim, which is a different (and harder) question.
    supported: bool
    query: str
    chunk_ids: List[str] = []
    source_ids: List[str] = []


class CoverageLedgerOut(ApiModel):
    id: str
    run_id: str
    workflow_run_id: str
    question: str
    #: "plain" | "debate".
    shape: str
    in_scope_count: int
    consulted_count: int
    #: True when a debate question found nothing on one side. This is the fact
    #: the rendered section exists to say out loud.
    one_sided: bool
    entries: List[CoverageEntryOut] = []
    #: `coverage.render`'s output, so the API and the report section are the
    #: same bytes from the same renderer.
    report_markdown: str
    created_at: datetime


def _ids(raw: str) -> List[str]:
    try:
        parsed = json.loads(raw or "[]")
    except ValueError:
        return []
    return [item for item in parsed if isinstance(item, str)]


def ledger_out(
    db: Session, ledger: CoverageLedger, entries: List[CoverageEntry]
) -> CoverageLedgerOut:
    return CoverageLedgerOut(
        id=ledger.id,
        run_id=ledger.run_id,
        workflow_run_id=ledger.workflow_run_id,
        question=ledger.question,
        shape=ledger.shape,
        in_scope_count=ledger.in_scope_count,
        consulted_count=ledger.consulted_count,
        one_sided=ledger.one_sided,
        entries=[
            CoverageEntryOut(
                ordinal=entry.ordinal,
                sub_question=entry.sub_question,
                stance=entry.stance,
                supported=entry.supported,
                query=entry.query,
                chunk_ids=_ids(entry.chunk_ids_json),
                source_ids=_ids(entry.source_ids_json),
            )
            for entry in entries
        ],
        report_markdown=service.render(db, ledger=ledger),
        created_at=ledger.created_at,
    )


@router.get("/api/runs/{run_id}/coverage", response_model=CoverageLedgerOut)
def get_run_coverage(
    run_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
) -> CoverageLedgerOut:
    run = db.scalar(
        select(Run).where(Run.id == run_id, Run.workspace_id == actor.workspace_id)
    )
    if run is None:
        raise HTTPException(status_code=404, detail="No coverage recorded for this run")
    ledger = service.ledger_for_run(
        db, workspace_id=actor.workspace_id, run_id=run.id
    )
    if ledger is None:
        raise HTTPException(status_code=404, detail="No coverage recorded for this run")
    return ledger_out(db, ledger, service.entries_for(db, ledger=ledger))
