import { describe, expect, it } from "vitest";
import type { CitationCheck } from "@workspace/api-client";
import {
  describeCitationCheck,
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
