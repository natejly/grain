"""The grounded-answer path: retrieve, answer, grade — in one place.

Two things live here, and both exist to stop a second copy of themselves being
written somewhere else.

`compose_report` is THE composition of the citation payload. The run path and
the machine API both store "what the validator made of this answer", and if
each built its own dict the two would drift the first time a key was added —
which is exactly what happened to every payload in this repo that had two
builders. It lives here rather than in `citations.py` because it takes
`retrieval.Evidence`, and `citations.py` deliberately knows nothing about
retrieval (see that module's opening docstring).

`answer_grounded` is the synchronous retrieve → answer → grade path behind both
`POST /api/answers/grounded` and the `grounded_answer` tool. It is NOT the chat
path and does not pretend to be: no transcript, no memory, no tools, no
streaming, and no repair pass. The repair in `runs._repair_unsupported` costs a
second model call, and a synchronous metered API that silently doubled its own
latency and its caller's bill would be a worse API than one that reports an
unsupported sentence honestly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Source
from . import embedding_generations, model, web_search
from .citations import grade_grounding, summarize_citations, validate_citations
from .retrieval import Evidence, SourceFilter, search_evidence

logger = logging.getLogger(__name__)


def checkable_flags(evidence: Sequence[object]) -> List[bool]:
    """Which of these passages carry text that can actually check an answer.

    THE ONE PLACE this question is answered, because the answer is about
    provenance and every consumer of `grade_grounding` needs the same one.

    False for a provider-executed web result. `web_search._evidence` builds its
    excerpt as a slice of the ANSWER — the hosted tool returns no page text and
    says so in its own docstring — so grading a sentence against it compares
    the sentence with a substring of itself. Coverage is ~1.0 by construction,
    numerals always match, and the verdict reads VERIFIED for a claim nothing
    local ever checked. Those sentences are ATTRIBUTED instead: the source's
    publisher asserts this, and we did not verify it.

    Asked through `web_search.citation_url`, which already owns the
    `isinstance(item, WebEvidence)` test, so this module does not grow a second
    opinion about what a web passage is.
    """
    return [
        not isinstance(item, Evidence) or web_search.citation_url(item) is None
        for item in evidence
    ]


def compose_report(
    answer: str, evidence: Sequence[object], *, floor: float
) -> Dict[str, Any]:
    """The whole stored verdict on one answer: citations, one line, grounding.

    One function, two call sites (`runs._validated_answer` and the answers
    route), so `messages.citation_report_json` and `grounded_receipts.
    report_json` cannot come to mean different things.

    `floor` is passed rather than read, so the caller's `Settings` decides and
    the value used is recorded inside the payload it produces.

    The grading is told which passages are checkable (see `checkable_flags`);
    `citations.py` still knows nothing about retrieval, it is handed a list of
    booleans.
    """
    report = validate_citations(answer, evidence)
    excerpts = [getattr(item, "excerpt", "") or "" for item in evidence]
    return {
        **report.to_dict(),
        "summary": summarize_citations(report),
        "grounding": grade_grounding(
            answer, excerpts, floor=floor, checkable=checkable_flags(evidence)
        ).to_dict(),
    }


@dataclass(frozen=True)
class GroundedAnswer:
    """One machine-answered question, before anything is persisted."""

    answer: str
    evidence: List[Evidence] = field(default_factory=list)
    report: Dict[str, Any] = field(default_factory=dict)
    #: The embedding contract the retrieval behind this answer ran on, "" when
    #: none is active. Stored so two receipts can be compared honestly: the same
    #: question under two contracts is two different retrievals.
    generation_id: str = ""
    #: The parsed object when the caller supplied a `json_schema` and the answer
    #: parsed; None otherwise.
    structured: Optional[Any] = None
    schema_error: str = ""


def answer_grounded(
    db: Session,
    *,
    workspace_id: str,
    user_id: str,
    question: str,
    space_id: str = "",
    conversation_id: str = "",
    source_ids: Sequence[str] = (),
    limit: int = 5,
    json_schema: Optional[Dict[str, Any]] = None,
    settings: Optional[Settings] = None,
) -> GroundedAnswer:
    """Retrieve, answer over what was retrieved, and grade the result.

    `conversation_id` defaults to "" because the MACHINE door has no thread:
    `POST /api/answers/grounded` is driven by a workspace token, so there are
    no conversation-scoped attachments it could be entitled to. It is a
    parameter rather than a constant because the `grounded_answer` TOOL runs
    inside a chat turn, where the thread's own files are part of the scope the
    other two retrieval tools already carry — a tool that could not see the
    file the user just attached would answer "nothing here says that" about
    the document on their screen.

    `source_ids` is an allow-list pushed INTO the query (`SourceFilter`), not a
    filter over already-ranked passages. That matters: ranking inside the
    allow-list returns the named source's best passage, while ranking globally
    and keeping the survivors returns nothing at all whenever the named source
    loses the ranking race — indistinguishable, in the response, from a corpus
    that has no answer. One property of the old post-filter is kept
    deliberately: an id that resolves to no source in this workspace is dropped
    silently rather than 404'd, because an allow-list must not double as an
    existence probe.

    `user_id` scopes nothing — retrieval here is workspace- and space-scoped,
    exactly as a workspace token's reach is — but it does travel to the
    provider as a hashed `safety_identifier`, so an abuse signal names the
    member whose token drove the call rather than the whole account.
    """
    settings = settings or get_settings()
    allowed = _resolve_sources(db, workspace_id=workspace_id, source_ids=source_ids)
    if allowed is not None and not allowed:
        # Every supplied id was foreign or unknown, so nothing is allowed. This
        # must NOT fall through to an unfiltered search: `SourceFilter` treats
        # an empty tuple as "no narrowing", and answering from the whole
        # library would turn "none of these sources exist here" into a scope
        # widening the caller explicitly excluded.
        evidence: List[Evidence] = []
    else:
        evidence = list(
            search_evidence(
                db,
                workspace_id=workspace_id,
                query=question,
                space_id=space_id,
                conversation_id=conversation_id,
                limit=limit,
                settings=settings,
                filters=(
                    None
                    if allowed is None
                    else SourceFilter(source_ids=tuple(sorted(allowed)))
                ),
            )
        )

    text, schema_error = model.answer_from_passages(
        question, evidence, json_schema, user_id=user_id, settings=settings
    )
    structured: Optional[Any] = None
    if json_schema is not None and not schema_error:
        # The same lenient parse `schema_error` was decided by, so "no error"
        # and "here is the object" can never disagree about one answer.
        structured = model._parsed_json_object(text)

    generation = embedding_generations.active_generation(db)
    return GroundedAnswer(
        answer=text,
        evidence=evidence,
        report=compose_report(text, evidence, floor=settings.grounding_floor),
        generation_id=generation.id if generation is not None else "",
        structured=structured,
        schema_error=schema_error,
    )


def _resolve_sources(
    db: Session, *, workspace_id: str, source_ids: Sequence[str]
) -> Optional[set[str]]:
    """The allow-list, narrowed to ids this workspace really owns. None = no filter.

    Returning an EMPTY set rather than None when every supplied id is unknown is
    the honest reading: the caller asked for an allow-list, none of it exists
    here, so nothing is allowed. Collapsing that to "no filter" would answer a
    question the caller did not ask, from sources they explicitly excluded.
    """
    ids = [str(value) for value in source_ids if str(value)]
    if not ids:
        return None
    rows = db.execute(
        select(Source.id).where(
            Source.workspace_id == workspace_id, Source.id.in_(ids)
        )
    ).scalars()
    return set(rows)


def verdict_line(report: Dict[str, Any]) -> str:
    """One sentence a model can read, summarising the grounding block.

    Deliberately ONE line. `ToolResult.bounded_content()` clips at
    `MAX_RESULT_CHARS` and `mcp_server._call_tool` appends up to eight passages
    AFTER that clip, so anything long here is content that pushes the answer
    out of its own result. The per-sentence list never goes in `content`; it
    rides the REST surface and the browser drawer instead.

    Says "verified" the way the grader means it — words present in the cited
    passage — because a tool result is read by a model that will repeat it.
    """
    grounding = report.get("grounding")
    if not isinstance(grounding, dict):
        return ""
    scored = int(grounding.get("scored") or 0)
    attributed = int(grounding.get("attributed") or 0)
    # Said EVERY time there are any, and said in the same breath as the score,
    # because a model reads this line and will repeat it: an attributed
    # sentence rests on a source's own assertion and nothing here checked it.
    rest = "sentence rests" if attributed == 1 else "sentences rest"
    tail = f"{attributed} {rest} on a web source's own assertion, unchecked here"
    if not scored:
        if attributed:
            return f"Grounding: nothing in this answer could be checked — {tail}."
        return "Grounding: no sentence in this answer made a checkable claim."
    verified = int(grounding.get("verified") or 0)
    unsupported = int(grounding.get("cited_unsupported") or 0)
    share = float(grounding.get("score") or 0.0)
    line = (
        f"Grounding {share:.0%} — {verified} of {scored} checked sentences have "
        "their words present in the passage they cite"
    )
    if unsupported:
        line += f"; {unsupported} cited but unsupported"
    if attributed:
        line += f"; {tail}"
    return line + "."


def flagged_sentences(grounding: Dict[str, Any]) -> Tuple[str, ...]:
    """The texts of the cited-but-unsupported sentences, in answer order.

    Read off the stored grounding block rather than re-graded, so the repair is
    aimed at exactly the sentences the recorded verdict flagged.
    """
    sentences = grounding.get("sentences")
    if not isinstance(sentences, list):
        return ()
    return tuple(
        str(entry.get("text") or "")
        for entry in sentences
        if isinstance(entry, dict) and entry.get("verdict") == "cited_unsupported"
    )
