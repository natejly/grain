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
from typing import List, Sequence, Tuple

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
