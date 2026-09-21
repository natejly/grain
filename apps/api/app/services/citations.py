"""Deterministic validator for the citation contract the chat prompt promises.

`CHAT_INSTRUCTIONS` tells the model to "attach [n] after each claim supported by
passage n" and "Only use [n] markers that match supplied passages". Nothing used
to check that it complied, so a fabricated `[4]` in an answer built from three
passages reached the user looking exactly like a real citation. This module is
that check: a regex over the answer plus the number of passages supplied, zero
model calls, no stochasticity, and — deliberately — no import from `retrieval`,
so the validator cannot drift along with the thing it is validating.

It returns data and never raises for malformed model output. The caller decides
whether to warn, strip, retry or merely record; a validator that mutated the
answer would make the answer depend on the checker, which is the one property
that would make its verdicts worthless.

What counts as a citation
-------------------------
A citation marker is a bracket group whose contents are *only* digits and the
separators that join them: `[1]`, `[1][2]`, `[1, 2]`, `[1-3]`, `[1 2]`. Ranges
expand inclusively.

* **Code does not count.** Markers inside fenced blocks (``` / ~~~) and inline
  code spans are masked out before parsing, because a `[0]` in a code sample is
  a subscript, not a citation. Four-space-indented blocks are *not* masked —
  they are indistinguishable from a wrapped list item without a full markdown
  parser, and prose is the far more common reading.
* **Markdown links do count.** `[4](https://…)` renders to the reader as the
  numeral 4 in citation position; if there was no passage 4 that is misleading
  whether or not it is also a hyperlink. One rule, no special case.
* **Brackets without digits are prose, not failed citations.** `[see below]`
  and `[n]` are ignored entirely rather than reported as malformed.
* **Non-ASCII digits and brackets are folded first.** `[４]` and `［٤］` render
  to the reader as exactly the citation `[4]` does, so they are read as one. The
  fold is length preserving, which is what keeps `Marker` offsets usable.
* **Duplicates are kept once in the summaries and every time in `markers`**, so
  a caller can count occurrences or highlight offsets without re-parsing.

Zero and negatives are out-of-range, not malformed: passages are numbered from
1, so `[0]` and `[-1]` name passages that do not exist, which is the same
failure as `[4]` out of three.

Known limitation: bracketed numbers in prose that were never meant as citations
— interval notation like "scores in the range [0, 100]", or `array[9]` outside a
code span — are read as citations and, being out of range, reported as
fabricated. Separating those from a real citation needs to know what the
sentence means, which a deterministic checker cannot; the marker offsets are in
the report so a caller can show the reader what was flagged.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# Dashes people (and models) actually type for a range. Escaped before it goes
# into a character class, where a bare "-" would read as a range operator.
_DASHES = re.escape("-–—")

# A candidate marker: a bracket group made only of digits and separators. The
# length cap stops a stray `[` from swallowing an entire digit-and-comma table
# up to some `]` far below; prose can never be swallowed, because letters are
# not in the class. 256 leaves room for a list naming every passage of a large
# retrieval — at 64 an ordinary `[1, 2, ... 20]` matched nothing at all and the
# answer was reported clean, which is the one outcome a validator may not have.
_CANDIDATE_RE = re.compile(rf"\[([0-9,;\s{_DASHES}]{{1,256}})\]")

# One entry inside a marker: a number, or an inclusive range of two.
_ENTRY_RE = re.compile(rf"^([0-9]{{1,9}})(?:[{_DASHES}]([0-9]{{1,9}}))?$")

# A negative entry. Out of range rather than malformed: `[-1]` names a passage
# that does not exist, which is the `[0]` failure, not a parse failure.
_NEGATIVE_RE = re.compile(rf"^[{_DASHES}]([0-9]{{1,9}})$")

_SEPARATOR_RE = re.compile(r"[,;\s]+")

# A citation marker sitting just past a sentence terminator, with only spaces
# or tabs between: "…division. [1][2]". Matched against the MASKED text, which
# is length preserving, so the offsets it yields are offsets into the source.
_TRAILING_MARKER_RE = re.compile(r"[ \t]*" + _CANDIDATE_RE.pattern)

# A single marker spanning more than this many passages is not a citation, it is
# a typo or an adversarial string; refusing to expand it keeps `[1-999999999]`
# from turning a validator into an allocation.
_MAX_RANGE_SPAN = 64

# ...and the same ceiling on one marker's whole expansion, so a bracket group
# packed with ranges cannot multiply its way past the per-range guard.
_MAX_MARKER_NUMBERS = 256

_FENCE_RE = re.compile(r"^(`{3,}|~{3,})")

# Folded before parsing so a marker written in another script is read as the
# citation a reader sees. Every replacement is one character wide, which is what
# lets `Marker.start`/`end` stay offsets into the untouched answer.
_BRACKETS = {"［": "[", "］": "]"}
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f]")

# CommonMark inline code: a run of N backticks closes on a run of N backticks,
# and the span may wrap across lines.
_INLINE_CODE_RE = re.compile(r"(?P<ticks>`+)(?P<body>.*?)(?P=ticks)", re.DOTALL)


@dataclass(frozen=True)
class Marker:
    """One bracket group that was read as a citation, with where it appeared.

    `text` is the exact source slice, so `answer_text[start:end] == text`; the
    offsets survive code masking because masking replaces characters one for one.
    """

    text: str
    start: int
    end: int
    numbers: Tuple[int, ...]
    malformed: bool


@dataclass(frozen=True)
class CitationReport:
    """Everything the validator observed. All tuples are sorted and deduplicated
    except `markers`, which preserves source order and repeats."""

    evidence_count: int
    markers: Tuple[Marker, ...]
    cited: Tuple[int, ...]
    out_of_range: Tuple[int, ...]
    uncited: Tuple[int, ...]
    malformed: Tuple[str, ...]

    @property
    def has_citations(self) -> bool:
        return bool(self.markers)

    @property
    def has_fabricated_citations(self) -> bool:
        """True when the model named a passage that was never supplied."""
        return bool(self.out_of_range)

    @property
    def is_valid(self) -> bool:
        """True when every marker in the answer resolves to a supplied passage.

        Uncited passages are not a contract violation — the model is told to cite
        claims, not to use everything it was handed — so they do not fail this.
        """
        return not self.out_of_range and not self.malformed

    def to_dict(self) -> dict[str, object]:
        """JSON-safe shape for an audit detail or a run event payload."""
        return {
            "evidence_count": self.evidence_count,
            "marker_count": len(self.markers),
            "cited": list(self.cited),
            "out_of_range": list(self.out_of_range),
            "uncited": list(self.uncited),
            "malformed": list(self.malformed),
            "valid": self.is_valid,
        }


def validate_citations(answer_text: str, evidence: Sequence[object]) -> CitationReport:
    """Check an answer's [n] markers against the passages that were supplied.

    Only `len(evidence)` is consulted. Taking the list rather than the count is
    what keeps every call site honest — the count has to come from the same
    object the model was actually given — while ignoring the element type is what
    keeps this module independent of `retrieval.Evidence`.
    """
    evidence_count = len(evidence)
    markers = _parse_markers(answer_text)

    cited: set[int] = set()
    out_of_range: set[int] = set()
    malformed: List[str] = []
    for marker in markers:
        if marker.malformed:
            malformed.append(marker.text)
            continue
        for number in marker.numbers:
            if 1 <= number <= evidence_count:
                cited.add(number)
            else:
                out_of_range.add(number)

    uncited = tuple(n for n in range(1, evidence_count + 1) if n not in cited)
    return CitationReport(
        evidence_count=evidence_count,
        markers=tuple(markers),
        cited=tuple(sorted(cited)),
        out_of_range=tuple(sorted(out_of_range)),
        uncited=uncited,
        malformed=tuple(malformed),
    )


def summarize_citations(report: CitationReport) -> str:
    """One line describing the report, for an audit record or a log.

    Leads with the violation when there is one, because that is the only part
    anyone reads a citation audit entry to find.
    """
    parts: List[str] = []
    if report.out_of_range:
        supplied = (
            "no passages were supplied"
            if report.evidence_count == 0
            else f"only {report.evidence_count} supplied"
        )
        fabricated = ", ".join(f"[{n}]" for n in report.out_of_range)
        parts.append(f"fabricated {fabricated} ({supplied})")
    if report.malformed:
        parts.append("malformed " + ", ".join(_truncate(report.malformed)))

    if report.evidence_count == 0:
        if not report.out_of_range:
            parts.append("no passages supplied")
    elif not report.cited:
        parts.append(f"no passages cited of {report.evidence_count}")
    else:
        parts.append(f"cited {len(report.cited)} of {report.evidence_count} passages")
        if report.uncited:
            uncited = ", ".join(str(n) for n in report.uncited)
            parts.append(f"uncited {uncited}")

    return "; ".join(parts)


def _truncate(values: Sequence[str], limit: int = 3) -> List[str]:
    head = [repr(value) for value in values[:limit]]
    if len(values) > limit:
        head.append(f"and {len(values) - limit} more")
    return head


def _parse_markers(answer_text: str) -> List[Marker]:
    masked = _fold_to_ascii(_mask_code(answer_text))
    markers: List[Marker] = []
    for match in _CANDIDATE_RE.finditer(masked):
        numbers = _parse_entries(match.group(1))
        if numbers is None:
            markers.append(
                Marker(
                    text=answer_text[match.start() : match.end()],
                    start=match.start(),
                    end=match.end(),
                    numbers=(),
                    malformed=True,
                )
            )
            continue
        if not numbers:
            # Digitless separators only, e.g. "[-]": prose, not a citation.
            continue
        markers.append(
            Marker(
                text=answer_text[match.start() : match.end()],
                start=match.start(),
                end=match.end(),
                numbers=tuple(numbers),
                malformed=False,
            )
        )
    return markers


def _parse_entries(content: str) -> List[int] | None:
    """Expand a marker's contents, or None when it is citation-shaped but broken.

    Returns [] for contents that carry no digits at all, which the caller drops
    rather than reports: `[-]` is punctuation, not a failed citation.

    Deliberately unforgiving about the rest. `[1,]` and `[1-]` could each be
    repaired to `[1]`, but a validator that quietly repairs its input stops being
    able to report that the model produced something it was never asked for.
    """
    stripped = content.strip()
    if not any(char.isdigit() for char in stripped):
        return []
    numbers: List[int] = []
    for entry in _SEPARATOR_RE.split(stripped):
        if not entry:
            return None
        match = _ENTRY_RE.match(entry)
        if match is None:
            negative = _NEGATIVE_RE.match(entry)
            if negative is None:
                return None
            numbers.append(-int(negative.group(1)))
            continue
        low = int(match.group(1))
        high = int(match.group(2)) if match.group(2) is not None else low
        if high < low or high - low >= _MAX_RANGE_SPAN:
            return None
        numbers.extend(range(low, high + 1))
        if len(numbers) > _MAX_MARKER_NUMBERS:
            return None
    return numbers


def _fold_to_ascii(text: str) -> str:
    """Rewrite non-ASCII digits and brackets to their ASCII equivalents.

    One character in, one character out, so offsets into the original answer
    survive. Superscripts and subscripts are deliberately left alone: `x[²]` is
    notation, not a citation, and `unicodedata.decimal` refuses them for us.
    """
    if text.isascii():
        return text
    return _NON_ASCII_RE.sub(_fold_char, text)


def _fold_char(match: re.Match[str]) -> str:
    char = match.group(0)
    bracket = _BRACKETS.get(char)
    if bracket is not None:
        return bracket
    try:
        return str(unicodedata.decimal(char))
    except (TypeError, ValueError):
        return char


def _mask_code(text: str) -> str:
    """Blank out code, preserving length so marker offsets stay source offsets."""
    return _mask_inline_code(_mask_fenced_blocks(text))


def _mask_fenced_blocks(text: str) -> str:
    out: List[str] = []
    open_fence: str | None = None
    for line in text.splitlines(keepends=True):
        match = _FENCE_RE.match(line.lstrip(" \t"))
        if open_fence is None:
            if match is None:
                out.append(line)
                continue
            open_fence = match.group(1)
        elif (
            match is not None
            and match.group(1)[0] == open_fence[0]
            and len(match.group(1)) >= len(open_fence)
        ):
            # CommonMark: a shorter run does not close a longer fence, so ``` in
            # the middle of a ```` block is code, not the end of the block.
            open_fence = None
        out.append(_blank(line))
    # An unterminated fence stays masked to the end of the answer, matching how a
    # markdown renderer shows it: everything after ``` is code to the reader.
    return "".join(out)


def _mask_inline_code(text: str) -> str:
    return _INLINE_CODE_RE.sub(lambda match: _blank(match.group(0)), text)


def _blank(chunk: str) -> str:
    return "".join("\n" if char == "\n" else " " for char in chunk)


# ---------------------------------------------------------------------------
# Per-sentence grounding
#
# Everything above answers "does this [n] name a passage that exists". Everything
# below answers the next question down: "do the words of this sentence appear in
# the passage it cites". They are deliberately in one module — same inputs, same
# no-model-no-stochasticity rule, same never-raises contract — and deliberately
# additive: no name above this line changed, because test_citations.py,
# test_stress_retrieval.py and test_web_search.py pin them.
#
# WHAT "VERIFIED" MEANS, AND WHAT IT DOES NOT
# -------------------------------------------
# This is a LEXICAL-OVERLAP SUPPORT TEST, not entailment. `verified` means only
# "the words this sentence uses are present in the passage it cites". It cannot
# catch a negated claim ("the launch is NOT in October" against a passage that
# says it is), a misattributed one ("Ravi owns the launch" against a passage
# naming Ravi and the launch in different sentences), or any claim whose words
# all appear in the passage while its meaning does not. It will also flag a
# correct paraphrase whose vocabulary diverges from its source. A caller that
# presents the score as truth is over-reading it; every string this module
# produces, and every string rendered from it, has to say support, not truth.

VERIFIED = "verified"
CITED_UNSUPPORTED = "cited_unsupported"
UNCITED = "uncited"
IGNORED = "ignored"
#: A sentence whose every citation names a passage this process cannot check.
#:
#: The case that forced it: a provider-executed web search returns no page
#: text, so the only "excerpt" available is the slice of the ANSWER the
#: provider annotated. Grading a sentence against a substring of itself
#: produces coverage ~1.0 by construction — a verdict that certifies nothing
#: while reading exactly like one that certifies a great deal. An ATTRIBUTED
#: sentence is therefore left OUT of `scored` the way IGNORED is, and counted
#: on its own: the source's publisher asserts this, and nothing local checked
#: it.
#:
#: This module still knows nothing about retrieval. It receives a parallel
#: `checkable` flag per passage and takes the caller's word for it; deciding
#: which passages carry real source text is `grounded.compose_report`'s job,
#: where provenance already lives.
ATTRIBUTED = "attributed"

#: How many sentences one report may carry. The report rides a run event and a
#: `messages.citation_report_json` column, so a pathological answer must not be
#: able to grow either without bound. Raising it means measuring the event
#: payload first.
_MAX_SENTENCES = 120

#: Per-sentence budget inside the stored report, for the same reason.
_SENTENCE_TEXT_CHARS = 300

#: A sentence with fewer content tokens than this is not scored. "Here is what I
#: found." carries no claim to support, and scoring it would move the headline
#: number without anything being checked.
_MIN_CONTENT_TOKENS = 4

# A LOCAL tokenizer and a LOCAL stopword list, duplicating `retrieval`'s on
# purpose. This module's opening docstring forbids importing from retrieval —
# the validator must not drift along with the thing it validates — and the
# duplication IS that independence: a tokenizer change made to improve ranking
# must not silently change what "supported" means. Read as copy-paste, this is
# the one place in the repo where copy-paste is the requirement.
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_'-]*")

_STOPWORDS = frozenset(
    """
    a an and are as at be been but by can did do does for from had has have he
    her here him his how i if in into is it its me my no not of on or our out
    she so some such than that the their them then there these they this those
    to too us was we were what when where which while who why will with would
    you your
    """.split()
)

# A token that is a number rather than a word. Anchored, so it classifies a
# token; `_NUMERAL_SCAN_RE` is what finds one in running text, because a numeral
# may carry separators (`1.2`, `40,000`, `2026-03`) that `_TOKEN_RE` splits on.
_NUMERAL_RE = re.compile(r"^\d[\d.,:/-]*$")
_NUMERAL_SCAN_RE = re.compile(r"\d[\d.,:/-]*")

# Ending a sentence after one of these is a mistake a period cannot tell you
# about: the token is an abbreviation, not a terminator.
_ABBREVIATIONS = frozenset(
    {"e.g.", "i.e.", "etc.", "vs.", "no.", "fig.", "approx.", "dr.", "mr.", "ms."}
)


def _content_tokens(text: str) -> set[str]:
    """The words of `text` that carry content, lowercased and deduplicated."""
    return {
        token
        for token in (match.group(0).lower() for match in _TOKEN_RE.finditer(text))
        if token not in _STOPWORDS
    }


def _numerals(text: str) -> set[str]:
    """Numbers as a reader would read them: `40`, `1.2`, `2026-03`.

    Scored apart from the words because a number is the one token a paraphrase
    may not change. "forty percent" and "fourteen percent" share every content
    token but the numeral, so coverage alone reads them as the same claim.
    """
    found: set[str] = set()
    for match in _NUMERAL_SCAN_RE.finditer(text):
        # Trailing separators belong to the sentence, not to the number:
        # "in 2026," is the numeral 2026.
        token = match.group(0).rstrip(".,:/-")
        if token and _NUMERAL_RE.match(token):
            found.add(token)
    return found


@dataclass(frozen=True)
class SentenceVerdict:
    """One sentence of an answer, and what the support test made of it.

    `start`/`end` index the UNTOUCHED answer, exactly as `Marker`'s do, so a
    caller can highlight the span without re-parsing. `text` is clipped for
    storage, so `answer_text[start:end] == text` holds only up to
    `_SENTENCE_TEXT_CHARS` — `split_sentences` is where that property is exact.
    """

    text: str
    start: int
    end: int
    citations: Tuple[int, ...]
    fabricated: Tuple[int, ...]
    verdict: str
    coverage: float
    missing_numerals: Tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "verdict": self.verdict,
            "citations": list(self.citations),
            "fabricated": list(self.fabricated),
            "coverage": self.coverage,
            "missing_numerals": list(self.missing_numerals),
        }


@dataclass(frozen=True)
class GroundingReport:
    """Every sentence's verdict, and the one number over them.

    `score` is verified / scored, where `scored` excludes the sentences that
    were IGNORED for carrying no claim. 0.0 with nothing scored — which is not
    "ungrounded", it is "there was nothing here to ground", and a caller that
    renders the two the same is inventing a verdict.
    """

    sentences: Tuple[SentenceVerdict, ...]
    score: float
    scored: int
    verified: int
    cited_unsupported: int
    uncited: int
    ignored: int
    floor: float
    truncated: bool
    #: Sentences whose citations all name unverifiable passages. Out of
    #: `scored`, like `ignored`, and reported separately so a reader is told
    #: the difference rather than shown a percentage that hides it.
    attributed: int = 0
    #: How many sentences the answer HAS, whether or not they were graded.
    #: `len(sentences)` is capped at `_MAX_SENTENCES`; this is not, which is
    #: what lets a renderer say "120 of 300 graded" instead of "120 of 120".
    n_sentences: int = 0
    #: True when the answer had more sentences than the cap, i.e. a tail of it
    #: was never looked at. Distinct from `truncated`, which is ALSO set when a
    #: single sentence's stored text was clipped — two different facts that
    #: must not share one flag, because only this one means "ungraded".
    sentences_truncated: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "score": self.score,
            "scored": self.scored,
            "verified": self.verified,
            "cited_unsupported": self.cited_unsupported,
            "uncited": self.uncited,
            "ignored": self.ignored,
            "attributed": self.attributed,
            "floor": self.floor,
            "truncated": self.truncated,
            "n_sentences": self.n_sentences,
            "sentences_truncated": self.sentences_truncated,
            "sentences": [sentence.to_dict() for sentence in self.sentences],
        }


def split_sentences(answer_text: str) -> List[Tuple[int, int, str]]:
    """The answer's sentences as `(start, end, text)`, offsets into the original.

    `answer_text[start:end] == text` for every span — the property `Marker`
    already holds, and for the same reason: code masking is length preserving,
    so boundaries found in the masked text are boundaries in the source.

    Code contributes nothing. A fenced block masks to whitespace and therefore
    yields no sentence at all, which is what keeps a `.` inside a Python snippet
    from splitting prose that surrounds it.

    Three rules decide a boundary:

    * a newline always ends a sentence, so a list item, a table row and a
      heading are each their own unit rather than being glued to the paragraph
      above;
    * `.`, `?` or `!` ends one when the next character is whitespace or the end
      of the answer — which is also what leaves `v1.2` and `3.5%` intact,
      since a digit is not whitespace;
    * unless the token it closes is an abbreviation (`e.g.`, `vs.`) or a single
      capital letter (`J. Chen`), where the period is part of the token.

    A citation marker that TRAILS the terminator on the same line belongs to
    the sentence it closes: "Revenue rose [1]." and "Revenue rose. [1]" are the
    same claim, cited the same way, and the prompt pins neither spelling. Before
    this rule the second form put the marker in the NEXT span, so a correctly
    cited answer scored 0% — the claim read UNCITED and, in flowing prose, its
    neighbour was graded against a passage it never named. Only spaces and tabs
    are crossed: a marker on the next LINE stays its own unit, because a
    newline is already a deliberate boundary (list items, table rows).
    """
    masked = _mask_code(answer_text)
    spans: List[Tuple[int, int, str]] = []
    start = 0
    length = len(masked)
    position = 0
    while position < length:
        char = masked[position]
        if char == "\n":
            _append_span(answer_text, masked, start, position, spans)
            start = position + 1
        elif char in ".?!":
            following = masked[position + 1] if position + 1 < length else ""
            if (following == "" or following.isspace()) and not _ends_with_abbreviation(
                masked, start, position + 1
            ):
                end = position + 1
                while True:
                    trailing = _TRAILING_MARKER_RE.match(masked, end)
                    if trailing is None:
                        break
                    end = trailing.end()
                _append_span(answer_text, masked, start, end, spans)
                start = end
                # Re-enter the loop just past the absorbed markers; the `+= 1`
                # below lands exactly on `end`.
                position = end - 1
        position += 1
    _append_span(answer_text, masked, start, length, spans)
    return spans


def _ends_with_abbreviation(masked: str, start: int, end: int) -> bool:
    """Is the token this period closes one that always carries a period?"""
    token = masked[start:end].split()[-1:] or [""]
    candidate = token[0].lower()
    if candidate in _ABBREVIATIONS:
        return True
    # "J. Chen": an initial, not the end of a sentence. Exactly one letter
    # before the dot, because "US." is a sentence ending in an acronym.
    return len(candidate) == 2 and candidate[0].isalpha() and candidate[1] == "."


def _append_span(
    answer_text: str,
    masked: str,
    start: int,
    end: int,
    spans: List[Tuple[int, int, str]],
) -> None:
    """Trim on the MASKED text and record the span, or record nothing.

    Trimming on the masked text is what makes a fenced block disappear: its
    characters are whitespace there, so the span collapses to nothing even
    though the source still holds the code.
    """
    left, right = start, end
    while left < right and masked[left].isspace():
        left += 1
    while right > left and masked[right - 1].isspace():
        right -= 1
    if right > left:
        spans.append((left, right, answer_text[left:right]))


def grade_grounding(
    answer_text: str,
    passages: Sequence[str],
    *,
    floor: float = 0.6,
    checkable: Optional[Sequence[bool]] = None,
) -> GroundingReport:
    """Classify every sentence of an answer against the passages it cites.

    A LEXICAL-OVERLAP SUPPORT TEST — see the section comment above. `verified`
    says the sentence's words are in the passage it named, and says nothing
    about whether the sentence is true.

    Per sentence:

    * markers inside its span are read from the same parse `validate_citations`
      uses; numbers in `1..len(passages)` are `citations`, the rest `fabricated`;
    * fewer than `_MIN_CONTENT_TOKENS` words of content and it is IGNORED and
      left out of the denominator — a greeting is not an unsupported claim;
    * no in-range citation and it is UNCITED;
    * every in-range citation naming a NON-checkable passage and it is
      ATTRIBUTED — out of the denominator, because there is nothing here to
      check the sentence against;
    * otherwise `coverage` is the share of its content tokens that appear
      anywhere in the CHECKABLE passages it cited, and it is VERIFIED when
      coverage reaches `floor` AND every numeral it states appears there too.

    `checkable` is a per-passage flag parallel to `passages`, defaulting to
    "all of them". False means "this passage's text is not independent evidence
    about the answer" — today, a provider-executed web result whose only
    available excerpt is a slice of the answer itself. Grading against that is
    grading a sentence against a substring of itself, which scores ~1.0 by
    construction. The flag is the caller's assertion, not something this module
    infers: `citations.py` imports nothing from retrieval, and that is what
    keeps the validator from drifting along with the thing it validates. A
    short or over-long list is padded with True, so a caller that loses track
    of provenance degrades to the pre-flag behaviour rather than silently
    marking real passages unverifiable.

    `floor` keeps a default so the function stays callable with no `Settings` —
    tests and the eval gate do exactly that. Production passes
    `settings.grounding_floor`, and the value used is reported back in
    `GroundingReport.floor` so a stored verdict says what it was measured at.

    Returns data and never raises, like `validate_citations`: everything here is
    regex, set arithmetic and one guarded division.
    """
    markers = _parse_markers(answer_text)
    masked = _mask_code(answer_text)
    passage_tokens = [_content_tokens(passage) for passage in passages]
    passage_numerals = [_numerals(passage) for passage in passages]
    count = len(passage_tokens)
    flags = list(checkable or ())[:count]
    flags.extend([True] * (count - len(flags)))

    spans = split_sentences(answer_text)
    sentences_truncated = len(spans) > _MAX_SENTENCES
    truncated = sentences_truncated
    verdicts: List[SentenceVerdict] = []
    tally = {
        VERIFIED: 0,
        CITED_UNSUPPORTED: 0,
        UNCITED: 0,
        IGNORED: 0,
        ATTRIBUTED: 0,
    }

    for start, end, text in spans[:_MAX_SENTENCES]:
        cited: List[int] = []
        fabricated: List[int] = []
        for marker in markers:
            if marker.malformed or marker.start < start or marker.end > end:
                continue
            for number in marker.numbers:
                target = cited if 1 <= number <= count else fabricated
                if number not in target:
                    target.append(number)

        # The prose, with code and its own citation markers blanked out. Without
        # the second blanking every cited sentence would carry the numeral of
        # the marker it ends with, and would be reported as stating a number its
        # passage does not.
        body = _blank_markers(masked[start:end], markers, start)
        tokens = _content_tokens(body)

        checked = [number for number in cited if flags[number - 1]]

        if len(tokens) < _MIN_CONTENT_TOKENS:
            verdict = IGNORED
            coverage = 0.0
            missing: Tuple[str, ...] = ()
        elif not cited:
            verdict = UNCITED
            coverage = 0.0
            missing = ()
        elif not checked:
            # Cited, and every passage it cited is one nothing here can check.
            # Scoring it either way would be an invention: "verified" would
            # certify a source this process never read, and "unsupported" would
            # accuse a sentence of a failure nobody measured.
            verdict = ATTRIBUTED
            coverage = 0.0
            missing = ()
        else:
            support: set[str] = set()
            support_numerals: set[str] = set()
            for number in checked:
                support |= passage_tokens[number - 1]
                support_numerals |= passage_numerals[number - 1]
            coverage = len(tokens & support) / len(tokens)
            missing = tuple(sorted(_numerals(body) - support_numerals))
            verdict = (
                VERIFIED if coverage >= floor and not missing else CITED_UNSUPPORTED
            )

        tally[verdict] += 1
        clipped = text[:_SENTENCE_TEXT_CHARS]
        truncated = truncated or len(clipped) < len(text)
        verdicts.append(
            SentenceVerdict(
                text=clipped,
                start=start,
                end=end,
                citations=tuple(sorted(cited)),
                fabricated=tuple(sorted(fabricated)),
                verdict=verdict,
                coverage=round(coverage, 4),
                missing_numerals=missing,
            )
        )

    scored = len(verdicts) - tally[IGNORED] - tally[ATTRIBUTED]
    return GroundingReport(
        sentences=tuple(verdicts),
        score=(tally[VERIFIED] / scored) if scored else 0.0,
        scored=scored,
        verified=tally[VERIFIED],
        cited_unsupported=tally[CITED_UNSUPPORTED],
        uncited=tally[UNCITED],
        ignored=tally[IGNORED],
        attributed=tally[ATTRIBUTED],
        floor=floor,
        truncated=truncated,
        n_sentences=len(spans),
        sentences_truncated=sentences_truncated,
    )


def _blank_markers(chunk: str, markers: Sequence[Marker], offset: int) -> str:
    """`chunk` with every citation marker replaced by spaces, length preserved."""
    out = list(chunk)
    for marker in markers:
        start = marker.start - offset
        end = marker.end - offset
        if start < 0 or end > len(out):
            continue
        for index in range(start, end):
            if out[index] != "\n":
                out[index] = " "
    return "".join(out)
