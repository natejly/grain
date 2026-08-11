"""Hosted web search, folded into the citation contract rather than exempted from it.

`web_search` is provider-executed: the model calls it inside OpenAI's own
infrastructure, so there is no ToolSpec, no approval card and no local execution,
and none of the machinery in `llm_tools`/`agent_loop` that governs a tool call
applies to it. What comes back is an answer carrying `url_citation` annotations —
a url, a title, and the character span of the answer that source supports.

That is exactly why this module exists. The product's central technical claim is
that a cited claim was checked; a class of source arriving through a side door
and skipping `citations.validate_citations` would falsify that claim for every
answer that used one. So the annotations are translated into the only currency
the rest of the pipeline understands: `Evidence`, appended to the same list the
validator counts and `_finish_run` reports, plus an `[n]` marker written into the
answer at the span the provider said the source supports. From there a web source
is validated, reported, persisted and rendered by exactly the path a retrieved
passage takes.

Three decisions worth stating:

* `WebEvidence` extends `Evidence` with a `url` rather than smuggling the URL
  through `filename` or `source_id`. Those fields address a chunk in this
  workspace's index, and a consumer that followed one to a web page would find
  nothing. The subclass is frozen and `url` defaults to "", so every existing
  construction, `asdict` round trip and consumer of `Evidence` is untouched.
* Marker insertion happens here, never in `citations.py`. The validator has to
  stay a pure observer of the answer — a checker that edited its input could not
  be trusted about it. Anchoring is a translation of what the provider asserted,
  applied before the answer is an answer, and it is what lets a web source be
  `cited` rather than permanently `uncited`.
* The provider's annotation is model-adjacent output, so it is untrusted: schemes
  are restricted, lengths bounded, and a span that does not fit the answer is
  dropped rather than clamped onto a sentence it may never have supported.

Nothing here raises. A failed search, or an annotation whose shape is not the
documented one, degrades to an answer with no web sources; the user still gets
their turn.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from ..config import Settings
from .retrieval import Evidence

logger = logging.getLogger(__name__)

# Schemes a reader can actually open. A `javascript:` or `data:` "citation"
# rendered as a link in the transcript is an injection vector, and an annotation
# is untrusted output like any other thing a model hands back.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Bounded here rather than at each of the three places these strings land (a run
# event, a persisted message row, and the citation list the UI renders).
_MAX_URL_CHARS = 2048
_MAX_TITLE_CHARS = 300
_MAX_QUERY_CHARS = 500
# The answer span a source supports becomes that citation's excerpt. Long enough
# to read as a quotation, short enough that one runaway span cannot inflate the
# stored message.
_MAX_EXCERPT_CHARS = 600

# One annotation and the offset in the answer its indices are counted from; None
# when the block it belongs to could not be located. See `_annotations`.
_Annotated = Tuple[Optional[int], Any]

#: Marks a citation id as addressing a page rather than a Chunk row. Exported
#: because the chunk route has to recognise one to refuse it for the right
#: reason: the two ids share a namespace but not a store.
WEB_CHUNK_PREFIX = "web:"


@dataclass(frozen=True)
class WebEvidence(Evidence):
    """A cited web page, shaped as `Evidence` so it validates like a passage.

    `chunk_id` is synthetic (`web:<digest>`) and addresses nothing in the index.
    It is derived from the URL because callers key citation lists by it and two
    sources must not collide, and because the same page cited twice in one answer
    has to be one entry. `score` is 0.0: the provider ranks its own results and
    exposes no comparable number, and inventing one would poison any statistic
    taken over retrieval scores.
    """

    url: str = ""


@dataclass(frozen=True)
class WebSearchOutcome:
    """What one model response did with web search, ready for the loop to apply.

    `anchors` are (offset into that response's text, citation number) pairs, kept
    apart from `evidence` because one source can support several spans and a
    source whose span was unusable still deserves to be reported.
    """

    queries: Tuple[str, ...] = ()
    sources: Tuple[str, ...] = ()
    evidence: Tuple[WebEvidence, ...] = ()
    anchors: Tuple[Tuple[int, int], ...] = ()
    failed: bool = False

    @property
    def happened(self) -> bool:
        """True when the trail has something to show, including a failed search.

        `anchors` counts: a step that cited only a page an earlier step already
        admitted produces a marker and no new evidence, and that is still a
        search this turn made.
        """
        return bool(
            self.queries or self.sources or self.evidence or self.anchors or self.failed
        )

    def event_payload(self) -> Dict[str, Any]:
        """The `web_search.completed` payload: what was searched and what was cited."""
        return {
            "queries": list(self.queries),
            "sources": list(self.sources),
            "cited": [
                {"url": item.url, "title": item.filename} for item in self.evidence
            ],
            "count": len(self.evidence),
            "failed": self.failed,
        }


def web_search_tool(settings: Settings) -> Optional[Dict[str, Any]]:
    """The hosted tool entry for the Responses `tools` array, or None when off.

    None rather than a disabled-looking entry: the flag has to take the tool out
    of the request, not merely discourage its use.

    `web_search_max_results` is deliberately not on the wire. The hosted tool
    takes no result count — it takes a context size, which is what actually moves
    latency — so the setting bounds the number of distinct sources admitted as
    evidence instead. That is the count that reaches the user, the validator and
    the message row, which is the count worth capping.
    """
    if not settings.web_search_enabled:
        return None
    return {
        "type": "web_search",
        "search_context_size": settings.web_search_context_size,
    }


def web_numbers(evidence: Sequence[Evidence]) -> Dict[str, int]:
    """Citation numbers already assigned to web sources this turn, keyed by URL.

    Evidence is numbered by position — `evidence[i]` is `[i+1]` everywhere from
    `_openai_input` to `validate_citations` — so a URL's number is just its index.

    One turn can search several times, either side of a tool call. Without this
    the second step re-admits a page the first already cited: two `Evidence`
    entries carrying the *same* synthetic `chunk_id`, two numbers on one source,
    and a duplicate key in the citation list the UI renders off `chunk_id`.
    """
    return {
        item.url: index
        for index, item in enumerate(evidence, start=1)
        if isinstance(item, WebEvidence) and item.url
    }


def harvest(
    response: Any,
    text: str,
    *,
    settings: Settings,
    evidence_offset: int,
    known: Optional[Mapping[str, int]] = None,
) -> WebSearchOutcome:
    """Read one completed response for web search activity and its citations.

    `text` is the answer text of *this* response, which is what the annotation
    offsets index into; `evidence_offset` is how many pieces of evidence the turn
    already has, so the numbers handed back continue the same sequence the
    retrieved passages were numbered in. `known` is `web_numbers` of that
    evidence, which keeps a page cited by an earlier step one source with one
    number and makes `web_search_max_results` a budget for the whole turn rather
    than for each step of it.

    Total by construction. A provider that changes a field name, or a stub whose
    shape is nothing like the real one, costs the turn its web sources and not the
    turn — the failure is recorded on the outcome so it reaches the activity
    trail instead of disappearing.
    """
    try:
        return _harvest(
            response,
            text,
            settings=settings,
            evidence_offset=evidence_offset,
            known=known or {},
        )
    except Exception:
        logger.warning(
            "web search harvest failed; answering without web sources", exc_info=True
        )
        return WebSearchOutcome(failed=True)


def anchor_citations(text: str, anchors: Sequence[Tuple[int, int]]) -> str:
    """Write `[n]` into the answer where the provider said source n supports it.

    Applied back to front so each insertion leaves the offsets of the ones still
    to come untouched, and sorted within a position so co-located sources read
    `[3][4]` rather than in whatever order the annotations arrived.
    """
    if not anchors:
        return text
    grouped: Dict[int, List[int]] = {}
    for position, number in anchors:
        grouped.setdefault(position, []).append(number)
    result = text
    for position in sorted(grouped, reverse=True):
        marker = "".join(f"[{number}]" for number in sorted(set(grouped[position])))
        result = result[:position] + marker + result[position:]
    return result


def citation_url(item: Evidence) -> Optional[str]:
    """The URL behind a citation, or None for a passage from the index."""
    return item.url or None if isinstance(item, WebEvidence) else None


def revive_evidence(item: Dict[str, Any]) -> Evidence:
    """Rebuild one serialized piece of evidence, keeping a web source a web source.

    `LoopState` round-trips through JSON whenever a turn parks on an approval and
    resumes in another process. Rebuilding a `WebEvidence` as a plain `Evidence`
    would drop the URL there — the citation would survive the pause as an
    unclickable title — and passing the extra key to `Evidence` would raise.
    """
    if "url" in item:
        return WebEvidence(**item)
    return Evidence(**item)


def _harvest(
    response: Any,
    text: str,
    *,
    settings: Settings,
    evidence_offset: int,
    known: Mapping[str, int],
) -> WebSearchOutcome:
    if not settings.web_search_enabled:
        # The flag means one thing in both directions. With the tool off the wire
        # a real response cannot carry annotations, so this only matters when
        # something upstream is wrong — and then "off" has to mean off.
        return WebSearchOutcome()
    queries: List[str] = []
    sources: List[str] = []
    evidence: List[WebEvidence] = []
    anchors: List[Tuple[int, int]] = []
    # Seeded with the pages earlier steps of this turn already admitted, so a page
    # cited on both sides of a tool call keeps one number and one Evidence rather
    # than arriving twice under the same synthetic chunk_id.
    number_by_url: Dict[str, int] = dict(known)
    failed = False
    # A budget for the turn, not for the step: max_results caps how many distinct
    # web sources reach the user, and a turn may search once per iteration.
    limit = max(0, settings.web_search_max_results - len(number_by_url))
    # How far through `text` the blocks seen so far reach. One response can carry
    # more than one block of answer text — a line before the provider ran its
    # search, then the answer — and each annotation indexes its own block while
    # `text` is every block concatenated. See `_annotations`.
    cursor = 0

    for item in _sequence(_field(response, "output")):
        if str(_field(item, "type") or "") == "web_search_call":
            # A provider-side search that failed is the degradation case: the
            # answer stands, minus its web sources, and the trail says so.
            if str(_field(item, "status") or "") in {"failed", "incomplete"}:
                failed = True
            action = _field(item, "action")
            query = _text(_field(action, "query"))[:_MAX_QUERY_CHARS]
            if query and query not in queries:
                queries.append(query)
            for source in _sequence(_field(action, "sources")):
                url = _url(_field(source, "url"))
                if url is not None and url not in sources:
                    sources.append(url)
            continue

        annotated, cursor = _annotations(item, text, cursor)
        for base, annotation in annotated:
            if str(_field(annotation, "type") or "") != "url_citation":
                continue
            url = _url(_field(annotation, "url"))
            if url is None:
                # Malformed or unopenable: rejected, not repaired and not fatal.
                continue
            span = _span(annotation, base, len(text))
            number = number_by_url.get(url)
            if number is None:
                if len(evidence) >= limit:
                    continue
                evidence.append(_evidence(url, annotation, text, span))
                number = evidence_offset + len(evidence)
                number_by_url[url] = number
            if span is not None:
                anchors.append((span[1], number))

    return WebSearchOutcome(
        queries=tuple(queries),
        sources=tuple(sources),
        evidence=tuple(evidence),
        anchors=tuple(anchors),
        failed=failed,
    )


def _evidence(
    url: str, annotation: Any, text: str, span: Optional[Tuple[int, int]]
) -> WebEvidence:
    """One annotation as evidence.

    The excerpt is the slice of the answer the annotation covers. It is not the
    page's own words — the hosted tool returns no page text — but it is the exact
    claim the provider says this source supports, which is what a reader checking
    a citation is trying to locate. When the span is unusable the title stands in,
    so the citation still renders as something.
    """
    title = _text(_field(annotation, "title"))[:_MAX_TITLE_CHARS] or _hostname(url)
    excerpt = text[span[0] : span[1]].strip() if span is not None else ""
    digest = hashlib.sha256(url.encode("utf-8", "replace")).hexdigest()[:16]
    return WebEvidence(
        chunk_id=WEB_CHUNK_PREFIX + digest,
        source_id=WEB_CHUNK_PREFIX + digest,
        filename=title,
        ordinal=0,
        excerpt=(excerpt or title)[:_MAX_EXCERPT_CHARS],
        score=0.0,
        url=url,
    )


def _annotations(item: Any, text: str, cursor: int) -> Tuple[List[_Annotated], int]:
    """Every annotation on a message item, with where its offsets are counted from.

    An annotation's indices are relative to the block of text it is attached to,
    not to the whole turn: the provider numbers characters within one message's
    output_text. `text` is every block of this response concatenated, in the order
    the deltas arrived, because that is what the user is shown — and a reasoning
    model routinely writes a line before it searches and the answer after, so the
    two are not the same string. Locating each block in `text` is what keeps a
    marker on the sentence the provider actually annotated instead of that many
    characters into the preamble.

    A block that cannot be found yields None as its base: the offsets then
    describe a string we do not have, and this module drops a span it cannot place
    rather than placing it approximately. The returned cursor is how far the
    located blocks reach, so the next item resumes there.
    """
    found: List[_Annotated] = [
        (cursor, annotation) for annotation in _sequence(_field(item, "annotations"))
    ]
    for part in _sequence(_field(item, "content")):
        base: Optional[int] = cursor
        block = _field(part, "text")
        if isinstance(block, str) and block:
            at = text.find(block, cursor)
            base = at if at >= 0 else None
            if at >= 0:
                cursor = at + len(block)
        found.extend(
            (base, annotation) for annotation in _sequence(_field(part, "annotations"))
        )
    return found, cursor


def _span(
    annotation: Any, base: Optional[int], length: int
) -> Optional[Tuple[int, int]]:
    """The answer span this annotation covers, or None when it does not fit.

    Rejected rather than clamped. An offset that runs past the answer describes
    some other text, and moving the marker to the nearest legal position would
    attach a source to a sentence it was never claimed to support. `base` is where
    the block these offsets count from begins in the answer; None means that block
    could not be located, which is the same failure.
    """
    if base is None:
        return None
    start = _index(_field(annotation, "start_index"))
    end = _index(_field(annotation, "end_index"))
    if start is None or end is None:
        return None
    if not 0 <= start <= end:
        return None
    if base + end > length:
        return None
    return base + start, base + end


def _url(value: Any) -> Optional[str]:
    # Over-long is rejected, not truncated: a cut URL is a different URL, and
    # linking a citation to some other page is worse than dropping the source.
    raw = _text(value)
    if not raw or len(raw) > _MAX_URL_CHARS:
        return None
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not parsed.netloc:
        return None
    return raw


def _hostname(url: str) -> str:
    try:
        return urlparse(url).netloc[:_MAX_TITLE_CHARS] or url[:_MAX_TITLE_CHARS]
    except ValueError:
        return url[:_MAX_TITLE_CHARS]


def _field(obj: Any, key: str) -> Any:
    """One field of an SDK object or the dict form of the same payload."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _sequence(value: Any) -> List[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _index(value: Any) -> Optional[int]:
    # bool is an int in Python, and `start_index: True` is not offset 1.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
