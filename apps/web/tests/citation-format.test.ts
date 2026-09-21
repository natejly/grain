import { describe, expect, it } from "vitest";
import type { CitationCheck } from "@workspace/api-client";
import {
  describeCitationCheck,
  describeGrounding,
  describeRepair,
  readCitationCheck,
} from "../components/views/citation-format";

/**
 * The citation verdict is a *warning surface*, which makes both halves of this
 * module load-bearing in a way an ordinary formatter is not: a payload it
 * misreads becomes a confident sentence about the truthfulness of an answer,
 * and a violation it declines to describe is a violation the reader never sees.
 *
 * So the tests are written from the failure directions. Reading is checked
 * against payloads that are wrong in each way a payload can be wrong, and
 * describing is checked against the four report shapes `services/citations.py`
 * can actually produce — including the one that is only interesting because it
 * must stay quiet.
 */

function report(overrides: Partial<CitationCheck> = {}): CitationCheck {
  return {
    evidence_count: 3,
    marker_count: 2,
    cited: [1, 2],
    out_of_range: [],
    uncited: [3],
    malformed: [],
    valid: true,
    summary: "cited 2 of 3 passages; uncited 3",
    // None means "never graded per sentence", which is what a turn that
    // predates the grounding report carries. The drawer renders nothing.
    grounding: null,
    repair: null,
    ...overrides,
  };
}

describe("readCitationCheck", () => {
  it("reads a well-formed run.citations payload", () => {
    const read = readCitationCheck({ ...report() } as unknown as Record<string, unknown>);
    expect(read).toEqual(report());
  });

  it("refuses a payload with no verdict in it", () => {
    // Missing `valid` or `evidence_count` means this is not the event we think
    // it is. Guessing would put a green tick under an unchecked answer.
    expect(readCitationCheck({ evidence_count: 3 })).toBeNull();
    expect(readCitationCheck({ valid: true })).toBeNull();
    expect(readCitationCheck({})).toBeNull();
  });

  it("treats a missing list as empty rather than as undefined", () => {
    const read = readCitationCheck({ evidence_count: 2, valid: true });
    expect(read).toEqual({
      evidence_count: 2,
      marker_count: 0,
      cited: [],
      out_of_range: [],
      uncited: [],
      malformed: [],
      valid: true,
      summary: "",
      grounding: null,
      repair: null,
    });
  });

  it("drops entries of the wrong type instead of carrying them into the copy", () => {
    const read = readCitationCheck({
      evidence_count: 2,
      valid: false,
      out_of_range: [4, "5", null],
      malformed: ["[1,]", 7],
    });
    expect(read?.out_of_range).toEqual([4]);
    expect(read?.malformed).toEqual(["[1,]"]);
  });
});

describe("describeCitationCheck", () => {
  it("says nothing when no passages were supplied", () => {
    // There was no retrieval, so there is no contract to have kept. A badge on
    // every chat turn is a badge nobody reads by the time one matters.
    expect(describeCitationCheck(report({ evidence_count: 0, uncited: [] }))).toBeNull();
  });

  it("names the fabricated markers and what they were checked against", () => {
    const verdict = describeCitationCheck(
      report({ out_of_range: [4, 7], valid: false, cited: [1] }),
    );
    expect(verdict?.tone).toBe("fabricated");
    expect(verdict?.title).toContain("2 citations do not match");
    expect(verdict?.detail).toContain("[4] and [7]");
    expect(verdict?.detail).toContain("3 passages were supplied");
  });

  it("uses singular wording for a single fabricated marker", () => {
    const verdict = describeCitationCheck(
      report({ evidence_count: 1, out_of_range: [4], valid: false, uncited: [1] }),
    );
    expect(verdict?.title).toBe("1 citation does not match a supplied passage");
    expect(verdict?.detail).toContain("[4] names a passage");
    expect(verdict?.detail).toContain("1 passage was supplied");
  });

  it("reports malformed markers, which are a violation too", () => {
    const verdict = describeCitationCheck(
      report({ malformed: ["[1,]"], valid: false, cited: [1, 2] }),
    );
    expect(verdict?.tone).toBe("fabricated");
    expect(verdict?.detail).toContain("“[1,]”");
  });

  it("leads with fabrication when an answer is wrong in both ways", () => {
    const verdict = describeCitationCheck(
      report({ out_of_range: [9], malformed: ["[1,]"], valid: false }),
    );
    expect(verdict?.title).toContain("does not match");
  });

  it("flags an answer that cites nothing it was given", () => {
    const verdict = describeCitationCheck(
      report({ marker_count: 0, cited: [], uncited: [1, 2, 3] }),
    );
    expect(verdict?.tone).toBe("uncited");
    expect(verdict?.title).toBe("This answer cites nothing");
  });

  it("still speaks when the answer is clean, so silence is not ambiguous", () => {
    const verdict = describeCitationCheck(report());
    expect(verdict?.tone).toBe("clean");
    expect(verdict?.title).toBe("Citations check out — 2 of 3 passages cited");
    expect(verdict?.detail).toContain("2 citations checked against 3 passages");
    expect(verdict?.detail).toContain("uncited: [3]");
  });

  it("omits the uncited list when there is none", () => {
    const verdict = describeCitationCheck(
      report({ evidence_count: 2, cited: [1, 2], uncited: [] }),
    );
    expect(verdict?.tone).toBe("clean");
    expect(verdict?.detail).toBe("2 citations checked against 2 passages; every marker resolves.");
  });
});

describe("the grounding block", () => {
  function grounding(overrides: Record<string, unknown> = {}) {
    return {
      score: 0.75,
      scored: 4,
      verified: 3,
      cited_unsupported: 1,
      uncited: 0,
      ignored: 0,
      floor: 0.6,
      truncated: false,
      sentences: [
        {
          start: 0,
          end: 10,
          text: "Maya owns it [1].",
          verdict: "verified",
          citations: [1],
          fabricated: [],
          coverage: 1,
          missing_numerals: [],
        },
      ],
      ...overrides,
    };
  }

  it("degrades a malformed grounding to null without losing the eight fields", () => {
    const read = readCitationCheck({
      ...report(),
      grounding: "not an object",
      repair: 7,
    } as unknown as Record<string, unknown>);
    expect(read?.grounding).toBeNull();
    expect(read?.repair).toBeNull();
    // The plate was already correct and must stay correct.
    expect(read?.evidence_count).toBe(3);
    expect(read?.cited).toEqual([1, 2]);
    expect(read?.summary).toBe("cited 2 of 3 passages; uncited 3");
  });

  it("drops a sentence whose verdict is not one of the four", () => {
    const read = readCitationCheck({
      ...report(),
      grounding: grounding({
        sentences: [
          ...grounding().sentences,
          { start: 1, end: 2, text: "x", verdict: "probably_fine", coverage: 1 },
          { start: 3, end: 4, text: "y", verdict: "verified" },
        ],
      }),
    } as unknown as Record<string, unknown>);
    // One kept: the invented verdict and the one with no numeric coverage are
    // both dropped rather than defaulted into a claim nobody made.
    expect(read?.grounding?.sentences).toHaveLength(1);
  });

  it("says nothing when nothing was scored", () => {
    const verdict = describeGrounding(
      report({ grounding: grounding({ scored: 0, score: 0 }) } as never),
    );
    expect(verdict).toBeNull();
  });

  it("reads an attributed count and a sentence-cap flag, defaulting to the old shape", () => {
    const read = readCitationCheck({
      ...report(),
      grounding: grounding({ attributed: 2, n_sentences: 300, sentences_truncated: true }),
    } as unknown as Record<string, unknown>);
    expect(read?.grounding?.attributed).toBe(2);
    expect(read?.grounding?.n_sentences).toBe(300);
    expect(read?.grounding?.sentences_truncated).toBe(true);

    // A verdict stored before these fields existed reads as the old shape, not
    // as a confident zero about a question nobody asked.
    const old = readCitationCheck({
      ...report(),
      grounding: grounding(),
    } as unknown as Record<string, unknown>);
    expect(old?.grounding?.attributed).toBe(0);
    expect(old?.grounding?.sentences_truncated).toBe(false);
  });

  it("keeps an attributed sentence's own verdict rather than dropping the row", () => {
    const read = readCitationCheck({
      ...report(),
      grounding: grounding({
        sentences: [
          {
            start: 0,
            end: 10,
            text: "The colony reported 4.2 million residents [1].",
            verdict: "attributed",
            citations: [1],
            fabricated: [],
            coverage: 0,
            missing_numerals: [],
          },
        ],
      }),
    } as unknown as Record<string, unknown>);
    expect(read?.grounding?.sentences[0].verdict).toBe("attributed");
  });

  it("says what was attributed instead of folding it into the percentage", () => {
    // THE PLATE THAT CERTIFIED NOTHING. A hosted web search returns no page
    // text, so the "passage" each web-cited sentence was graded against was a
    // slice of that sentence — coverage ~1.0 by construction, and the plate
    // read "Grounding 100% — 2 of 2 sentences verified" over a source nothing
    // local had ever read.
    const verdict = describeGrounding(
      report({
        grounding: grounding({
          cited_unsupported: 0,
          uncited: 0,
          verified: 4,
          score: 1,
          attributed: 3,
        }),
      } as never),
    );
    expect(verdict?.title).toContain("4 of 4 checked sentences verified");
    expect(verdict?.title).toContain("3 attributed to web sources, unchecked");
    expect(verdict?.detail).toContain("nothing here checked it");
    // Not the confident tone: part of this answer was not checked.
    expect(verdict?.tone).toBe("partial");
  });

  it("speaks up for an answer built entirely from web citations", () => {
    // `scored === 0` with attributed sentences is not "nothing to say" — it is
    // the case the old grader reported as 100%.
    const verdict = describeGrounding(
      report({
        grounding: grounding({
          scored: 0,
          score: 0,
          verified: 0,
          cited_unsupported: 0,
          attributed: 2,
        }),
      } as never),
    );
    expect(verdict?.title).toContain("nothing checkable");
    expect(verdict?.title).toContain("2 attributed to web sources, unchecked");
    expect(verdict?.title).not.toContain("100%");
  });

  it("says how much of a long answer was never graded", () => {
    const verdict = describeGrounding(
      report({
        grounding: grounding({
          cited_unsupported: 0,
          uncited: 0,
          verified: 4,
          score: 1,
          n_sentences: 300,
          sentences_truncated: true,
        }),
      } as never),
    );
    // The grader stops at 120 sentences; the plate used to read "100% — 120 of
    // 120 verified" over a 300-sentence deliverable whose tail it never saw.
    expect(verdict?.title).toContain("not graded");
    expect(verdict?.tone).toBe("partial");
    expect(verdict?.detail).toContain("not looked at");
  });

  it("gives the three tones", () => {
    expect(
      describeGrounding(report({ grounding: grounding() } as never))?.tone,
    ).toBe("ungrounded");
    expect(
      describeGrounding(
        report({
          grounding: grounding({ cited_unsupported: 0, uncited: 1, verified: 3 }),
        } as never),
      )?.tone,
    ).toBe("partial");
    expect(
      describeGrounding(
        report({
          grounding: grounding({ cited_unsupported: 0, uncited: 0, score: 1, verified: 4 }),
        } as never),
      )?.tone,
    ).toBe("grounded");
  });

  it("says support rather than truth, in every tone", () => {
    for (const block of [
      grounding(),
      grounding({ cited_unsupported: 0, uncited: 1 }),
      grounding({ cited_unsupported: 0, uncited: 0, score: 1, verified: 4 }),
    ]) {
      const verdict = describeGrounding(report({ grounding: block } as never));
      expect(verdict?.detail).toContain("not of truth");
    }
  });

  it("describes the three repair outcomes worth telling a reader about", () => {
    const base = {
      attempted: true,
      applied: false,
      reason: "",
      score_before: 0.5,
      score_after: 0.5,
      unsupported_before: 2,
      unsupported_after: 2,
    };
    expect(
      describeRepair({ ...base, applied: true, unsupported_after: 0 }),
    ).toContain("2 unsupported sentences were rewritten");
    expect(describeRepair({ ...base, reason: "rejected_not_better" })).toContain(
      "discarded",
    );
    expect(
      describeRepair({ ...base, attempted: false, reason: "no_provider" }),
    ).toContain("not configured");
    expect(describeRepair({ ...base, attempted: false, reason: "nothing_to_repair" })).toBeNull();
    expect(describeRepair(null)).toBeNull();
  });
});
