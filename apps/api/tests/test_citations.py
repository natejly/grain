from __future__ import annotations

import random
import time

import pytest

from app.services.citations import (
    CitationReport,
    summarize_citations,
    validate_citations,
)


def passages(count: int) -> list[object]:
    """Stand-ins for retrieval.Evidence. The validator only reads the length, and
    proving that with objects it could not possibly introspect is the point."""
    return [object() for _ in range(count)]


def check(answer: str, count: int) -> CitationReport:
    return validate_citations(answer, passages(count))


# --- the headline case: markers naming passages that were never supplied -------


def test_out_of_range_marker_is_reported():
    report = check("The deploy host is Railway [4].", 3)

    assert report.out_of_range == (4,)
    assert report.cited == ()
    assert report.has_fabricated_citations is True
    assert report.is_valid is False


def test_zero_is_out_of_range_because_passages_are_one_based():
    report = check("Claim [0].", 3)

    assert report.out_of_range == (0,)
    assert report.cited == ()


def test_negative_marker_is_fabricated_not_merely_malformed():
    # It used to land in `malformed`, which left `has_fabricated_citations`
    # False — so a caller gating on fabrication waved [-1] through.
    report = check("Claim [-1].", 3)

    assert report.out_of_range == (-1,)
    assert report.malformed == ()
    assert report.has_fabricated_citations is True
    assert summarize_citations(report).startswith("fabricated [-1] (only 3 supplied)")


def test_negative_inside_a_list_does_not_poison_the_rest():
    report = check("Claim [1, -2].", 3)

    assert report.cited == (1,)
    assert report.out_of_range == (-2,)


def test_citation_with_no_evidence_at_all_is_fabricated():
    report = check("General knowledge, allegedly sourced [1].", 0)

    assert report.evidence_count == 0
    assert report.out_of_range == (1,)
    assert report.uncited == ()
    assert report.is_valid is False


def test_in_range_and_out_of_range_are_separated():
    report = check("A [1] and B [2] and C [9].", 2)

    assert report.cited == (1, 2)
    assert report.out_of_range == (9,)
    assert report.uncited == ()


# --- marker shapes -------------------------------------------------------------


def test_adjacent_markers():
    report = check("Both agree [1][2].", 2)

    assert report.cited == (1, 2)
    assert [marker.text for marker in report.markers] == ["[1]", "[2]"]


def test_comma_list_inside_one_marker():
    report = check("Both agree [1, 2].", 3)

    assert report.cited == (1, 2)
    assert report.uncited == (3,)
    assert len(report.markers) == 1


def test_whitespace_separated_list_inside_one_marker():
    assert check("Both agree [1 2].", 2).cited == (1, 2)


def test_range_expands_inclusively():
    report = check("Everything agrees [1-3].", 4)

    assert report.cited == (1, 2, 3)
    assert report.uncited == (4,)


def test_range_partially_out_of_range():
    report = check("Everything agrees [2-5].", 3)

    assert report.cited == (2, 3)
    assert report.out_of_range == (4, 5)


def test_en_dash_range_is_accepted():
    assert check("Everything agrees [1–3].", 3).cited == (1, 2, 3)


def test_duplicate_markers_count_once_in_cited_and_every_time_in_markers():
    report = check("First [1]. Second [1]. Third [1].", 1)

    assert report.cited == (1,)
    assert len(report.markers) == 3
    assert report.to_dict()["marker_count"] == 3


def test_multi_digit_marker():
    assert check("Deep in the list [12].", 12).cited == (12,)


@pytest.mark.parametrize(
    "answer",
    ["Claim [1,].", "Claim [1-].", "Claim [3-1].", "Claim [1--2].", "Claim [,1]."],
)
def test_broken_markers_are_reported_as_malformed(answer):
    report = check(answer, 3)

    assert report.malformed == (answer[len("Claim ") : -1],)
    assert report.cited == ()
    assert report.is_valid is False
    # Malformed is not fabricated: nothing was claimed about a passage that
    # does not exist, the marker simply does not parse.
    assert report.has_fabricated_citations is False


def test_long_citation_list_is_parsed_rather_than_silently_dropped():
    # Past ~64 characters the candidate never matched, so an answer citing
    # twenty passages — including ones that do not exist — was reported clean.
    answer = "Everything agrees [" + ", ".join(str(n) for n in range(1, 21)) + "]."
    report = check(answer, 3)

    assert report.cited == (1, 2, 3)
    assert report.out_of_range == tuple(range(4, 21))
    assert report.is_valid is False


def test_marker_packed_with_ranges_is_malformed_rather_than_expanded():
    # Each range is under the per-range span cap; together they would expand to
    # thousands of numbers from one bracket group.
    answer = "Claim [" + ",".join("1-64" for _ in range(50))[:200] + "]."
    report = check(answer, 3)

    assert report.cited == ()
    assert report.malformed != ()


def test_absurd_range_is_malformed_rather_than_expanded():
    report = check("Claim [1-999999999].", 3)

    assert report.malformed == ("[1-999999999]",)
    assert report.cited == ()


@pytest.mark.parametrize("answer", ["See [below].", "Use [n] markers.", "Empty [].", "Dash [-]."])
def test_bracketed_prose_is_not_a_citation(answer):
    report = check(answer, 2)

    assert report.markers == ()
    assert report.malformed == ()
    assert report.has_citations is False


def test_markdown_link_with_numeric_label_counts():
    # It renders to the reader as a numeral in citation position, so an
    # unsupported one is misleading whether or not it is also a hyperlink.
    report = check("See [4](https://example.com/doc).", 3)

    assert report.out_of_range == (4,)


# --- markers written in another script -----------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "Claim [４].",  # fullwidth four
        "Claim [٤].",  # arabic-indic four
        "Claim [४].",  # devanagari four
        "Claim ［4］.",  # fullwidth brackets, ascii digit
        "Claim ［４］.",  # fullwidth throughout
    ],
)
def test_non_ascii_marker_is_read_as_the_citation_a_reader_sees(answer):
    # These render indistinguishably from [4]; not folding them left a
    # fabricated citation reported as a clean answer.
    report = check(answer, 3)

    assert report.out_of_range == (4,)
    assert report.has_fabricated_citations is True


def test_non_ascii_marker_in_range_counts_as_coverage():
    report = check("Claim [１].", 2)

    assert report.cited == (1,)
    assert report.uncited == (2,)


@pytest.mark.parametrize("answer", ["x[²]", "x[₂]", "x[四]", "x[Ⅳ]"])
def test_superscripts_subscripts_and_numerals_are_not_folded(answer):
    # Only decimal digits fold. A superscript is notation, and a CJK or Roman
    # numeral in brackets is not what the prompt asked the model to emit.
    report = check(answer, 3)

    assert report.markers == ()
    assert report.is_valid is True


def test_folding_preserves_offsets_into_the_original_answer():
    answer = "日本語の文 ［４］ と ［１］ です。"
    report = check(answer, 3)

    assert report.cited == (1,)
    assert report.out_of_range == (4,)
    for marker in report.markers:
        assert answer[marker.start : marker.end] == marker.text


def test_folding_does_not_resurrect_markers_masked_as_code():
    report = check("```\n［９］\n```\nDone [1].", 1)

    assert report.cited == (1,)
    assert report.out_of_range == ()


# --- code must not count -------------------------------------------------------


def test_marker_inside_fenced_block_is_ignored():
    answer = "Here is the code:\n\n```python\nvalues[0] = values[7]\n```\n\nDone [1].\n"
    report = check(answer, 1)

    assert report.cited == (1,)
    assert report.out_of_range == ()
    assert len(report.markers) == 1


def test_tilde_fence_is_masked_too():
    answer = "~~~\nrow[9]\n~~~\nNothing cited here.\n"
    report = check(answer, 2)

    assert report.markers == ()
    assert report.uncited == (1, 2)


def test_unterminated_fence_masks_to_the_end():
    answer = "Intro [1].\n```\nvalues[9]\nstill code [8]\n"
    report = check(answer, 1)

    assert report.cited == (1,)
    assert report.out_of_range == ()


def test_shorter_fence_run_does_not_close_a_longer_one():
    # CommonMark: ``` inside a ```` block is code, not the end of the block.
    # Closing early exposed the code to the parser as prose.
    report = check("````\nsee ``` then values[9]\n````\nDone [1].", 1)

    assert report.cited == (1,)
    assert report.out_of_range == ()


def test_longer_fence_run_still_closes():
    report = check("```\nvalues[9]\n`````\nDone [1].", 1)

    assert report.cited == (1,)
    assert report.out_of_range == ()


def test_marker_inside_inline_code_is_ignored():
    report = check("Write `items[0]` to index it, per passage [1].", 1)

    assert report.cited == (1,)
    assert report.out_of_range == ()


def test_double_backtick_inline_span_is_masked():
    report = check("Use ``a[3]`` here [1].", 1)

    assert report.cited == (1,)
    assert report.out_of_range == ()


def test_unmatched_backtick_does_not_swallow_the_rest():
    report = check("A stray ` backtick, then [1].", 1)

    assert report.cited == (1,)


def test_indented_code_is_not_masked_and_is_documented_as_such():
    # Four-space indentation is indistinguishable from a wrapped list item
    # without a real markdown parser; prose is the commoner reading, so the
    # marker counts and an out-of-range one is still caught.
    report = check("Example:\n\n    values[9]\n", 1)

    assert report.out_of_range == (9,)


# --- coverage, absence, and adjacency ------------------------------------------


def test_answer_with_no_citations_leaves_every_passage_uncited():
    report = check("I could not find anything relevant in your files.", 3)

    assert report.has_citations is False
    assert report.cited == ()
    assert report.uncited == (1, 2, 3)
    # Uncited passages are not a contract violation.
    assert report.is_valid is True


def test_answer_citing_every_passage_has_nothing_uncited():
    report = check("A [1]. B [2]. C [3].", 3)

    assert report.cited == (1, 2, 3)
    assert report.uncited == ()
    assert report.is_valid is True


def test_empty_evidence_and_no_markers_is_valid():
    report = check("Here is a general answer.", 0)

    assert report.is_valid is True
    assert report.uncited == ()


def test_empty_answer():
    report = check("", 2)

    assert report.markers == ()
    assert report.uncited == (1, 2)


@pytest.mark.parametrize(
    "answer",
    [
        "see[1]",
        "[1].",
        "([1])",
        "Le résumé[1].",
        "中文の文章[1]。",
        "**bold**[1]",
        "line one\n[1]",
    ],
)
def test_markers_are_found_regardless_of_adjacent_text(answer):
    report = check(answer, 1)

    assert report.cited == (1,)


def test_marker_offsets_index_the_original_answer():
    answer = "Le résumé says so [2], and so does the appendix [1]."
    report = check(answer, 2)

    assert report.cited == (1, 2)
    for marker in report.markers:
        assert answer[marker.start : marker.end] == marker.text


# --- purity --------------------------------------------------------------------


def test_interval_notation_in_prose_is_a_known_false_positive():
    # Pinned deliberately, not endorsed: telling "scores in the range [0, 100]"
    # apart from a citation needs to know what the sentence means. The docstring
    # documents it; this test makes a future change to that stance explicit.
    report = check("Scores fall in the range [0, 100].", 3)

    assert report.out_of_range == (0, 100)


# --- pathological input --------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "[" * 500_000 + "]" * 500_000,
        "[" + "1" * 1_000_000,
        ("[" + "1" * 255 + "x") * 5_000,
        "`" + "a" * 1_000_000,
        "` " * 500_000,
        "```\n" + "row[9]\n" * 100_000,
        "［４］" * 200_000,
        "文書の内容がここにあります。" * 60_000,
        " ".join("[1-999999999]" for _ in range(20_000)),
    ],
    ids=[
        "nested_brackets",
        "unclosed_bracket_of_digits",
        "candidate_cap_backtracking",
        "unclosed_backtick",
        "backtick_storm",
        "unterminated_fence",
        "fullwidth_marker_storm",
        "non_ascii_prose",
        "absurd_range_flood",
    ],
)
def test_pathological_answers_finish_promptly(answer):
    start = time.perf_counter()
    validate_citations(answer, passages(3))
    elapsed = time.perf_counter() - start

    # Generous: every one of these runs in well under a second locally. The
    # assertion is guarding against a class change (quadratic backtracking, an
    # expanded absurd range), not measuring the machine.
    assert elapsed < 10.0


# --- purity (continued) --------------------------------------------------------


def test_validator_does_not_mutate_or_depend_on_evidence_contents():
    answer = "Claim [1]."
    evidence = passages(2)
    snapshot = list(evidence)

    first = validate_citations(answer, evidence)
    second = validate_citations(answer, evidence)

    assert evidence == snapshot
    assert first == second


# --- the audit summary ---------------------------------------------------------


def test_summary_leads_with_the_fabricated_citation():
    summary = summarize_citations(check("Claim [4].", 3))

    assert summary.startswith("fabricated [4] (only 3 supplied)")
    assert "cited 0" not in summary


def test_summary_of_a_clean_answer_mentions_coverage_only():
    summary = summarize_citations(check("A [1]. B [2].", 2))

    assert summary == "cited 2 of 2 passages"


def test_summary_names_uncited_passages():
    summary = summarize_citations(check("A [1].", 3))

    assert summary == "cited 1 of 3 passages; uncited 2, 3"


def test_summary_when_nothing_was_cited():
    assert summarize_citations(check("No sources used.", 2)) == "no passages cited of 2"


def test_summary_when_nothing_was_supplied():
    assert summarize_citations(check("General answer.", 0)) == "no passages supplied"


def test_summary_when_citing_with_no_evidence():
    summary = summarize_citations(check("Claim [1].", 0))

    assert summary == "fabricated [1] (no passages were supplied)"


def test_summary_reports_malformed_markers_and_truncates_a_flood():
    answer = " ".join(f"[{n}-]" for n in range(1, 6))
    summary = summarize_citations(check(answer, 1))

    assert summary.startswith("malformed '[1-]', '[2-]', '[3-]', and 2 more")


def test_summary_is_one_line():
    summary = summarize_citations(check("A [1] and [9] and [2,].", 2))

    assert "\n" not in summary


def test_to_dict_is_json_safe():
    import json

    report = check("A [1] and [9] and [2,].", 2)
    payload = report.to_dict()

    assert json.loads(json.dumps(payload)) == payload
    assert payload["out_of_range"] == [9]
    assert payload["valid"] is False


# --- property-style sweep ------------------------------------------------------


def test_generated_marker_sets_round_trip():
    """Whatever set of passage numbers we render into an answer must come back
    exactly, across every marker syntax, for many random combinations."""
    rng = random.Random(20260810)

    for _ in range(300):
        evidence_count = rng.randint(0, 12)
        numbers = sorted(rng.sample(range(1, 21), rng.randint(0, 8)))

        pieces = []
        for number in numbers:
            style = rng.choice(["plain", "list", "range", "adjacent", "link"])
            if style == "plain":
                pieces.append(f"claim [{number}].")
            elif style == "list":
                pieces.append(f"claim [{number}, {number}].")
            elif style == "range":
                pieces.append(f"claim [{number}-{number}].")
            elif style == "adjacent":
                pieces.append(f"claim[{number}][{number}]")
            else:
                pieces.append(f"claim [{number}](https://example.com/{number})")
        # Noise that must never be read as a citation.
        pieces.append("`buf[99]` and [see notes]")
        rng.shuffle(pieces)

        report = validate_citations(" ".join(pieces), passages(evidence_count))
        expected_cited = tuple(n for n in numbers if n <= evidence_count)
        expected_out = tuple(n for n in numbers if n > evidence_count)

        assert report.cited == expected_cited
        assert report.out_of_range == expected_out
        assert report.malformed == ()
        assert report.uncited == tuple(
            n for n in range(1, evidence_count + 1) if n not in expected_cited
        )
