"""The per-sentence support test: what it catches, and what it deliberately does not.

`grade_grounding` is a LEXICAL overlap check. Every assertion below is written
in those terms on purpose — `verified` means the sentence's words are in the
passage it cites, never that the sentence is true — and one test pins the
limitation directly, because a grader whose blind spot is undocumented reads to
a future maintainer as a bug in the tests rather than a property of the method.

Pure functions, no database, no model.
"""
from __future__ import annotations

from app.services.citations import (
    _MAX_SENTENCES,
    ATTRIBUTED,
    CITED_UNSUPPORTED,
    IGNORED,
    UNCITED,
    VERIFIED,
    grade_grounding,
    split_sentences,
    validate_citations,
)

NORTHSTAR = (
    "The Northstar project launches in October. Maya Chen owns the launch. "
    "Its goal is to reduce customer onboarding time by forty percent."
)


def verdicts(report) -> list[str]:
    return [sentence.verdict for sentence in report.sentences]


def test_a_sentence_whose_words_are_in_its_passage_is_verified():
    report = grade_grounding(
        "Maya Chen owns the Northstar launch [1].", [NORTHSTAR]
    )
    assert verdicts(report) == [VERIFIED]
    assert report.score == 1.0
    assert report.scored == 1
    assert report.sentences[0].citations == (1,)


def test_a_planted_wrong_name_at_high_overlap_is_cited_but_unsupported():
    """The defect this exists to catch: every other word matches, the name does not."""
    report = grade_grounding(
        "Ravi Deshpande owns the Northstar launch project entirely [1].", [NORTHSTAR]
    )
    assert verdicts(report) == [CITED_UNSUPPORTED]
    assert report.cited_unsupported == 1
    assert report.score == 0.0


def test_a_sentence_with_no_marker_is_uncited_and_not_a_defect():
    report = grade_grounding(
        "Maya Chen owns the Northstar launch entirely.", [NORTHSTAR]
    )
    assert verdicts(report) == [UNCITED]
    assert report.uncited == 1
    assert report.cited_unsupported == 0


def test_a_short_sentence_is_ignored_rather_than_scored():
    """"Here you go." carries no claim, so scoring it would move the headline
    number without anything having been checked."""
    report = grade_grounding("Here you go. Maya Chen owns the launch [1].", [NORTHSTAR])
    assert verdicts(report) == [IGNORED, VERIFIED]
    assert report.scored == 1
    assert report.ignored == 1
    assert report.score == 1.0


def test_a_missing_numeral_fails_a_sentence_whose_coverage_clears_the_floor():
    """A paraphrase may change the words. It may not change the figure."""
    report = grade_grounding(
        "The goal is to reduce customer onboarding time by 55 percent [1].",
        [NORTHSTAR],
    )
    assert verdicts(report) == [CITED_UNSUPPORTED]
    assert report.sentences[0].coverage >= report.floor
    assert report.sentences[0].missing_numerals == ("55",)


def test_a_citation_markers_own_digits_are_not_read_as_a_claimed_numeral():
    """Without blanking the marker, every `[1]` would be an unsupported "1"."""
    report = grade_grounding(
        "Maya Chen owns the Northstar launch [1].", [NORTHSTAR]
    )
    assert report.sentences[0].missing_numerals == ()


def test_a_fenced_code_block_contributes_no_sentences():
    answer = "Maya Chen owns the launch [1].\n\n```python\nx = data[0].y\n```\n"
    report = grade_grounding(answer, [NORTHSTAR])
    assert verdicts(report) == [VERIFIED]
    # And the `[0]` inside the fence is not a fabricated citation either — the
    # same masking both halves of this module share.
    assert validate_citations(answer, [NORTHSTAR]).out_of_range == ()


def test_an_abbreviation_and_a_version_number_do_not_split_a_sentence():
    spans = split_sentences("We shipped v1.2, e.g. the violet ring, on Tuesday.")
    assert [text for _start, _end, text in spans] == [
        "We shipped v1.2, e.g. the violet ring, on Tuesday."
    ]


def test_an_initial_does_not_split_a_sentence():
    spans = split_sentences("J. Chen owns it. Ravi does not.")
    assert [text for _start, _end, text in spans] == [
        "J. Chen owns it.",
        "Ravi does not.",
    ]


def test_a_list_item_is_its_own_sentence():
    spans = split_sentences("Two things:\n- the ring is violet\n- the launch is Maya's")
    assert len(spans) == 3


def test_offsets_slice_the_original_answer_back():
    """The property `Marker` already holds, and the reason the drawer can
    highlight a span without re-parsing the answer."""
    answer = "Maya owns it [1]. `code[0]` here.\n\n```\nfenced [2]\n```\nLast one."
    for start, end, text in split_sentences(answer):
        assert answer[start:end] == text


def test_empty_passages_leave_every_cited_sentence_unsupported():
    report = grade_grounding("Maya Chen owns the Northstar launch [1].", [""])
    assert verdicts(report) == [CITED_UNSUPPORTED]
    assert report.sentences[0].coverage == 0.0


def test_a_marker_past_the_passage_count_is_fabricated_not_a_citation():
    report = grade_grounding("Maya Chen owns the Northstar launch [4].", [NORTHSTAR])
    assert report.sentences[0].fabricated == (4,)
    assert report.sentences[0].citations == ()
    # No in-range citation, so the sentence is UNCITED rather than unsupported:
    # "cited nothing that exists" is a different fact from "cited badly", and
    # `validate_citations` is what reports the fabrication.
    assert verdicts(report) == [UNCITED]


def test_grading_is_idempotent():
    first = grade_grounding(NORTHSTAR + " [1]", [NORTHSTAR])
    second = grade_grounding(NORTHSTAR + " [1]", [NORTHSTAR])
    assert first == second


def test_score_is_zero_exactly_when_nothing_was_scored():
    empty = grade_grounding("Hi.", [NORTHSTAR])
    assert empty.scored == 0
    assert empty.score == 0.0
    # And 0.0 with something scored means something different, which is why a
    # caller must read `scored` before rendering the number.
    failing = grade_grounding("Ravi Deshpande owns this whole launch [1].", [""])
    assert failing.scored == 1
    assert failing.score == 0.0


def test_the_sentence_list_is_capped_and_says_so():
    answer = " ".join(
        f"Maya Chen owns the Northstar launch number {index} [1]."
        for index in range(_MAX_SENTENCES + 10)
    )
    report = grade_grounding(answer, [NORTHSTAR])
    assert len(report.sentences) == _MAX_SENTENCES
    assert report.truncated is True


def test_a_long_sentence_is_clipped_and_says_so():
    answer = "Maya Chen owns the Northstar launch " + ("x " * 400) + "[1]."
    report = grade_grounding(answer, [NORTHSTAR])
    assert report.truncated is True
    assert len(report.sentences[0].text) == 300


def test_the_floor_travels_with_the_report():
    sentence = "Maya owns the Northstar launch roadmap [1]."
    assert verdicts(grade_grounding(sentence, [NORTHSTAR])) == [VERIFIED]
    strict = grade_grounding(sentence, [NORTHSTAR], floor=0.99)
    assert strict.floor == 0.99
    assert verdicts(strict) == [CITED_UNSUPPORTED]


def test_lexical_support_cannot_see_a_negation():
    """The documented blind spot, pinned so it stays documented.

    Every word of this sentence is in the passage; the sentence says the
    opposite of what the passage says. The grader calls it verified, because
    "verified" means the words are there — and the product copy, the schema
    docs and the eval floors all have to keep saying only that.
    """
    report = grade_grounding(
        "Maya Chen does not own the Northstar launch [1].", [NORTHSTAR]
    )
    assert verdicts(report) == [VERIFIED]


# --- the passages a slice of the answer cannot check -------------------------


def test_a_sentence_cited_only_to_an_uncheckable_passage_is_attributed():
    """The self-grading tautology, pinned.

    A hosted web search returns no page text, so `web_search._evidence` stores
    the slice of the ANSWER the provider annotated as the "excerpt". Grading
    the sentence against that compares it with a substring of itself: coverage
    ~1.0, numerals always present, verdict VERIFIED — a plate reading 100% for
    the one source class nothing local ever read. ATTRIBUTED says what is
    actually known: the publisher asserts it, we did not check it.
    """
    claim = "The Martian colony reported 4.2 million residents in August 2031."
    report = grade_grounding(claim + " [1]", [claim], checkable=[False])

    assert verdicts(report) == [ATTRIBUTED]
    assert report.attributed == 1
    assert report.verified == 0
    # Out of the denominator, like IGNORED: there was nothing to grade, which
    # is not the same as grading it and finding nothing.
    assert report.scored == 0
    assert report.score == 0.0


def test_the_same_sentence_is_verified_when_the_passage_is_checkable():
    """The contrast that makes the test above mean something: identical text,
    identical grader, and the only difference is whether the passage is real
    evidence or a slice of the answer."""
    claim = "The Martian colony reported 4.2 million residents in August 2031."
    report = grade_grounding(claim + " [1]", [claim])

    assert verdicts(report) == [VERIFIED]
    assert report.attributed == 0
    assert report.scored == 1


def test_a_mixed_citation_is_graded_against_the_checkable_passage_only():
    """Citing one real passage and one unverifiable page is still a check —
    against the real one. Folding the unverifiable excerpt into the support set
    would let a web result vouch for words the indexed passage does not carry.
    """
    unverifiable = "Ravi Deshpande owns the Northstar launch and its roadmap."
    report = grade_grounding(
        "Ravi Deshpande owns the Northstar launch and its roadmap [1][2].",
        [NORTHSTAR, unverifiable],
        checkable=[True, False],
    )
    assert verdicts(report) == [CITED_UNSUPPORTED]
    assert report.attributed == 0
    assert report.scored == 1


def test_checkable_defaults_to_every_passage_and_tolerates_a_short_list():
    """Every existing caller passes nothing, and a caller that loses track of
    provenance must degrade to "all checkable" rather than to "none"."""
    sentence = "Maya Chen owns the Northstar launch roadmap [1]."
    assert verdicts(grade_grounding(sentence, [NORTHSTAR])) == [VERIFIED]
    assert verdicts(grade_grounding(sentence, [NORTHSTAR], checkable=[])) == [VERIFIED]


def test_an_uncited_sentence_stays_uncited_when_the_passages_are_unverifiable():
    """ATTRIBUTED is about what a sentence CITED. A sentence citing nothing has
    a different defect and keeps its own verdict."""
    report = grade_grounding(
        "Maya Chen owns the Northstar launch roadmap.", [NORTHSTAR], checkable=[False]
    )
    assert verdicts(report) == [UNCITED]
    assert report.scored == 1


# --- a marker that trails the terminator -------------------------------------


def test_every_marker_spacing_scores_the_same():
    """"claim [1].", "claim.[1]" and "claim. [1]" are one claim cited one way.

    The third spelling used to put the marker in the FOLLOWING span, so the
    claim read UNCITED and the answer scored 0% — a wholesale wrong verdict on
    a correctly cited answer, persisted to the receipt and used to rank council
    candidates. Nothing in the prompt pins the spelling, so all three have to
    agree.
    """
    claim = "Maya Chen owns the Northstar launch roadmap"
    for answer in (f"{claim} [1].", f"{claim}.[1]", f"{claim}. [1]"):
        report = grade_grounding(answer, [NORTHSTAR])
        assert verdicts(report) == [VERIFIED], answer
        assert report.score == 1.0, answer
        assert report.sentences[0].citations == (1,), answer


def test_a_trailing_marker_is_not_stolen_by_the_next_sentence():
    """The worse half of the same defect: in flowing prose the orphan marker
    was attributed to the NEXT sentence, which was then graded against a
    passage it never named and reported as cited-but-unsupported."""
    report = grade_grounding(
        "Maya Chen owns the Northstar launch roadmap. [1] "
        "Onboarding time falls by forty percent under it. [2]",
        [NORTHSTAR, NORTHSTAR],
    )
    assert verdicts(report) == [VERIFIED, VERIFIED]
    assert [sentence.citations for sentence in report.sentences] == [(1,), (2,)]


def test_a_marker_on_the_next_line_stays_its_own_unit():
    """Only spaces and tabs are crossed. A newline is already a boundary rule —
    list items and table rows depend on it — so a marker starting a new line
    must not be glued to the line above."""
    spans = split_sentences("Maya Chen owns the launch.\n[1] handbook.md")
    assert [text for _start, _end, text in spans] == [
        "Maya Chen owns the launch.",
        "[1] handbook.md",
    ]


def test_offsets_still_slice_the_original_answer_back_with_trailing_markers():
    answer = "Maya Chen owns the launch. [1] Onboarding falls by forty percent. [2][3]"
    for start, end, text in split_sentences(answer):
        assert answer[start:end] == text


# --- the ungraded tail -------------------------------------------------------


def test_a_capped_report_says_how_many_sentences_the_answer_had():
    """`truncated` alone could not be rendered honestly: it is also set when a
    single sentence's stored text is clipped. `n_sentences` and
    `sentences_truncated` are what let a surface say "120 of 300 graded"
    instead of "120 of 120 verified" about an answer it only read the front of.
    """
    total = _MAX_SENTENCES + 10
    answer = "\n".join(
        f"Maya Chen owns the Northstar launch number {index} [1]."
        for index in range(total)
    )
    report = grade_grounding(answer, [NORTHSTAR])

    assert len(report.sentences) == _MAX_SENTENCES
    assert report.n_sentences == total
    assert report.sentences_truncated is True
    assert report.to_dict()["n_sentences"] == total
    assert report.to_dict()["sentences_truncated"] is True


def test_a_clipped_sentence_is_not_reported_as_an_ungraded_tail():
    answer = "Maya Chen owns the Northstar launch " + ("x " * 400) + "[1]."
    report = grade_grounding(answer, [NORTHSTAR])
    assert report.truncated is True
    assert report.sentences_truncated is False
    assert report.n_sentences == 1
