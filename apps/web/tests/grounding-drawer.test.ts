// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import React from "react";
import { afterEach, describe, expect, it } from "vitest";
import type { CitationCheck, GroundingCheck, SentenceGrounding } from "@workspace/api-client";

import { CitationVerdictNote, GroundingDrawer } from "../components/views/citation-note";
import { SENTENCE_LABELS } from "../components/views/citation-format";
import { blocksFor, blocksMentioning, declaration, ruleBody } from "./css-rules";

/**
 * The drawer's rendering contract, and — the part that matters — its silence.
 *
 * An answer that was never graded per sentence must render EXACTLY today's
 * citation plate and nothing more. The grounding block is new, most stored
 * messages predate it, and a drawer that appeared empty on all of them would
 * read as "checked, nothing found" on answers nothing checked.
 *
 * The copy assertions are not decoration either. "Verified" here means the
 * sentence's words are in the passage it cites, and the drawer has to say so
 * in the open state — a badge with no such sentence beside it is exactly the
 * over-reading this feature is one bad string away from.
 */

afterEach(cleanup);

function sentence(overrides: Partial<SentenceGrounding> = {}): SentenceGrounding {
  return {
    start: 0,
    end: 10,
    text: "Maya Chen owns the Northstar launch [1].",
    verdict: "verified",
    citations: [1],
    fabricated: [],
    coverage: 1,
    missing_numerals: [],
    ...overrides,
  };
}

function grounding(overrides: Partial<GroundingCheck> = {}): GroundingCheck {
  return {
    score: 0.75,
    scored: 4,
    verified: 3,
    cited_unsupported: 1,
    uncited: 0,
    ignored: 1,
    floor: 0.6,
    truncated: false,
    sentences: [
      sentence(),
      sentence({ start: 41, end: 90, verdict: "cited_unsupported", coverage: 0.2 }),
      sentence({ start: 91, end: 120 }),
      sentence({ start: 121, end: 150 }),
      sentence({ start: 151, end: 160, verdict: "ignored", citations: [] }),
    ],
    ...overrides,
  };
}

function report(overrides: Partial<CitationCheck> = {}): CitationCheck {
  return {
    evidence_count: 2,
    marker_count: 4,
    cited: [1],
    out_of_range: [],
    uncited: [2],
    malformed: [],
    valid: true,
    summary: "cited 1 of 2 passages; uncited 2",
    grounding: grounding(),
    repair: null,
    ...overrides,
  };
}

describe("GroundingDrawer", () => {
  it("renders nothing at all when the answer was never graded", () => {
    const { container } = render(
      React.createElement(GroundingDrawer, { report: report({ grounding: null }) }),
    );
    expect(container.innerHTML).toBe("");
  });

  it("renders nothing when nothing in the answer was scorable", () => {
    // scored === 0 is "no checkable claim here", not "0% grounded".
    const { container } = render(
      React.createElement(GroundingDrawer, {
        report: report({ grounding: grounding({ scored: 0, score: 0 }) }),
      }),
    );
    expect(container.innerHTML).toBe("");
  });

  it("leaves the citation plate untouched on an ungraded answer", () => {
    render(
      React.createElement(CitationVerdictNote, { report: report({ grounding: null }) }),
    );
    expect(screen.getByText(/Citations check out/)).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("starts closed and opens on click", () => {
    render(React.createElement(GroundingDrawer, { report: report() }));
    const toggle = screen.getByRole("button");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByRole("list")).toBeNull();

    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByRole("list")).toBeTruthy();
  });

  it("shows one row per NON-ignored sentence, labelled as the verdict is named", () => {
    render(React.createElement(GroundingDrawer, { report: report() }));
    fireEvent.click(screen.getByRole("button"));

    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(4);
    expect(screen.getAllByText(SENTENCE_LABELS.verified)).toHaveLength(3);
    expect(screen.getAllByText(SENTENCE_LABELS.cited_unsupported)).toHaveLength(1);
    expect(screen.queryByText(SENTENCE_LABELS.ignored)).toBeNull();
    expect(rows[1].className).toContain("cited_unsupported");
  });

  it("gives an attributed sentence its own badge, not a shade of verified", () => {
    // The sentence cites a web page whose text never reached this app, so
    // there was nothing here to check it against. Rendering it as verified is
    // the failure; hiding it (like `ignored`) is the other failure, because it
    // made a claim.
    render(
      React.createElement(GroundingDrawer, {
        report: report({
          grounding: grounding({
            attributed: 1,
            sentences: [
              sentence(),
              sentence({
                start: 200,
                end: 260,
                verdict: "attributed",
                coverage: 0,
                text: "The colony reported 4.2 million residents [2].",
              }),
            ],
          }),
        }),
      }),
    );
    fireEvent.click(screen.getByRole("button"));
    const rows = screen.getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(screen.getByText(SENTENCE_LABELS.attributed)).toBeTruthy();
    expect(SENTENCE_LABELS.attributed).not.toContain("Verified");
    expect(rows[1].className).toContain("attributed");
    expect(screen.getByText(/nothing here checked it/)).toBeTruthy();
  });

  it("says in the drawer how much of the answer was never graded", () => {
    render(
      React.createElement(GroundingDrawer, {
        report: report({
          grounding: grounding({ n_sentences: 300, sentences_truncated: true }),
        }),
      }),
    );
    fireEvent.click(screen.getByRole("button"));
    // 300 sentences, 5 rows: without this line the list reads as a complete
    // account of an answer the grader only saw the front of.
    expect(screen.getByText(/295 further sentences .* were not checked/)).toBeTruthy();
  });

  it("says what verified means, in the open drawer", () => {
    render(React.createElement(GroundingDrawer, { report: report() }));
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText(/words of the sentence appear in the passage it cites/)).toBeTruthy();
    expect(screen.queryByText(/fact.checked/i)).toBeNull();
  });

  it("carries the score and the counts in the summary", () => {
    render(React.createElement(GroundingDrawer, { report: report() }));
    expect(screen.getByText(/Grounding 75%/)).toBeTruthy();
    // "checked sentences", because an answer can now also carry sentences
    // nothing local could check — they are counted separately, and the
    // denominator has to say which ones it is talking about.
    expect(screen.getByText(/3 of 4 checked sentences verified/)).toBeTruthy();
  });

  it("reports an applied repair in the open drawer", () => {
    render(
      React.createElement(GroundingDrawer, {
        report: report({
          repair: {
            attempted: true,
            applied: true,
            reason: "",
            score_before: 0.5,
            score_after: 0.75,
            unsupported_before: 2,
            unsupported_after: 1,
          },
        }),
      }),
    );
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText(/1 unsupported sentence was rewritten/)).toBeTruthy();
  });

  it("says nothing about a repair that had nothing to do", () => {
    render(
      React.createElement(GroundingDrawer, {
        report: report({
          repair: {
            attempted: false,
            applied: false,
            reason: "nothing_to_repair",
            score_before: 1,
            score_after: 1,
            unsupported_before: 0,
            unsupported_after: 0,
          },
        }),
      }),
    );
    fireEvent.click(screen.getByRole("button"));
    expect(screen.queryByText(/rewritten|discarded|not configured/)).toBeNull();
  });
});

/**
 * Every class the drawer writes has a rule behind it.
 *
 * Read through the shared parser in ./css-rules rather than a regex of this
 * file's own — the reason that module exists. jsdom has no layout engine, so a
 * class with no rule renders as unstyled text and no component test above can
 * tell the difference.
 */
describe("the drawer's styles", () => {
  const CLASSES = [
    ".grounding-plate",
    ".grounding-summary",
    ".grounding-detail",
    ".grounding-meaning",
    ".grounding-repair",
    ".grounding-sentences",
    ".grounding-sentence",
    ".grounding-badge",
    ".grounding-markers",
    ".grounding-text",
    ".grounded-receipt-detail",
    ".grounding-summary.grounded",
    ".grounding-summary.partial",
    ".grounding-summary.ungrounded",
    ".grounding-sentence.verified",
    ".grounding-sentence.cited_unsupported",
    ".grounding-sentence.uncited",
  ];

  it.each(CLASSES)("%s has a rule in globals.css", (selector) => {
    expect(blocksMentioning(selector).length).toBeGreaterThan(0);
  });

  it("sits in the same gutter as the plate it extends", () => {
    // 31px is the citation plate's offset. Two columns of verdict under one
    // answer would read as a notice beside a notice.
    expect(declaration(ruleBody(".grounding-plate"), "margin-left")).toBe("31px");
    expect(declaration(ruleBody(".citation-check"), "margin-left")).toBe("31px");
  });

  it("reads as part of the plate rather than as a second control", () => {
    // `blocksFor`, not `ruleBody`: the concatenation would fold the `:hover`
    // background over the base one and report the wrong winner.
    const body = blocksFor(".grounding-summary").join("");
    expect(declaration(body, "border")).toBe("0");
    expect(declaration(body, "background")).toBe("transparent");
    expect(declaration(body, "width")).toBe("100%");
  });
});
