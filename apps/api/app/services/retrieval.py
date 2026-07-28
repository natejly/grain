from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import List

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Chunk, Source

TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{1,}")
STOP_WORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "can",
    "could",
    "for",
    "from",
    "have",
    "how",
    "into",
    "its",
    "more",
    "that",
    "the",
    "their",
    "this",
    "was",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
    "you",
    "your",
}


@dataclass(frozen=True)
class Evidence:
    chunk_id: str
    source_id: str
    filename: str
    ordinal: int
    excerpt: str
    score: float


def tokenize(value: str) -> List[str]:
    return [
        token.lower()
        for token in TOKEN_RE.findall(value)
        if token.lower() not in STOP_WORDS
    ]


def search_evidence(
    db: Session,
    *,
    workspace_id: str,
    query: str,
    limit: int = 5,
    token_budget: int = 1200,
) -> List[Evidence]:
    terms = Counter(tokenize(query))
    if not terms:
        return []
    rows = db.execute(
        select(Chunk, Source)
        .join(Source, Source.id == Chunk.source_id)
        .where(
            Chunk.workspace_id == workspace_id,
            Source.workspace_id == workspace_id,
            Source.deleted_at.is_(None),
            Source.status == "ready",
        )
    ).all()
    scored = []
    for chunk, source in rows:
        chunk_terms = Counter(tokenize(chunk.content))
        overlap = set(terms) & set(chunk_terms)
        if not overlap:
            continue
        score = sum(
            (1.0 + math.log(1 + chunk_terms[term])) * (1.0 + terms[term] * 0.25)
            for term in overlap
        )
        score /= max(1.0, math.sqrt(len(chunk_terms)))
        scored.append((score, chunk, source))
    scored.sort(key=lambda item: (-item[0], item[1].source_id, item[1].ordinal))
    evidence: List[Evidence] = []
    used_tokens = 0
    for score, chunk, source in scored[: limit * 2]:
        if len(evidence) >= limit:
            break
        remaining = token_budget - used_tokens
        if remaining <= 0:
            break
        words = chunk.content.split()
        excerpt = " ".join(words[:remaining])
        used_tokens += len(excerpt.split())
        evidence.append(
            Evidence(
                chunk_id=chunk.id,
                source_id=source.id,
                filename=source.filename,
                ordinal=chunk.ordinal,
                excerpt=excerpt,
                score=round(score, 4),
            )
        )
    return evidence

