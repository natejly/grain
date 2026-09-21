import type { CoverageEntry, CoverageLedger } from "@workspace/api-client";
import { describe, expect, it } from "vitest";
import { NO_COVERAGE, summarizeCoverage } from "../components/views/coverage-format";

/**
 * The summary line is the only part of the ledger most readers will see, so it
 * has to stay inside what the ledger actually knows: what was LOOKED AT.
 * Nothing here is an entailment check — `supported` means retrieval returned
 * something — and a line that read like a verdict would be the product
 * overstating itself in its smallest print.
 */

function entry(overrides: Partial<CoverageEntry> = {}): CoverageEntry {
  return {
    ordinal: 0,
    sub_question: "How long are logs kept?",
    stance: "neutral",
    supported: true,
    query: "logs retention",
    chunk_ids: ["chunk-a"],
    source_ids: ["source-a"],
    ...overrides,
  };
}

function ledger(overrides: Partial<CoverageLedger> = {}): CoverageLedger {
  return {
    id: "ledger-1",
    run_id: "run-1",
    workflow_run_id: "",
    question: "How long are logs kept?",
    shape: "plain",
    in_scope_count: 12,
    consulted_count: 3,
    one_sided: false,
    entries: [entry()],
    report_markdown: "## Coverage\n",
    created_at: "2026-09-21T09:00:00",
    ...overrides,
  };
}

describe("summarizeCoverage", () => {
  it("leads with what was consulted, out of what was in scope", () => {
    expect(summarizeCoverage(ledger())).toBe(
      "3 of 12 sources consulted · 1 sub-questions, all supported",
    );
  });

  it("names the unsupported sub-questions rather than the supported ones", () => {
    const summary = summarizeCoverage(
      ledger({
        entries: [
          entry({ ordinal: 0 }),
          entry({ ordinal: 1, supported: false }),
          entry({ ordinal: 2, supported: false }),
        ],
      }),
    );
    expect(summary).toBe(
      "3 of 12 sources consulted · 2 of 3 sub-questions unsupported",
    );
  });

  it("says a debate question got both sides", () => {
    const summary = summarizeCoverage(
      ledger({
        shape: "debate",
        entries: [
          entry({ ordinal: 0 }),
          entry({ ordinal: 1, stance: "for" }),
          entry({ ordinal: 2, stance: "against" }),
        ],
      }),
    );
    // The stance entries are NOT counted as posed sub-questions: they are the
    // forced counter-evidence pass, which nobody asked for.
    expect(summary).toBe(
      "3 of 12 sources consulted · 1 sub-questions, all supported · both sides retrieved",
    );
  });

  it("says a one-sided corpus out loud", () => {
    const summary = summarizeCoverage(
      ledger({
        shape: "debate",
        one_sided: true,
        entries: [
          entry({ ordinal: 0, stance: "for" }),
          entry({ ordinal: 1, stance: "against", supported: false }),
        ],
      }),
    );
    expect(summary).toContain("one-sided: no counter-evidence found");
  });

  it("omits the sub-question clause when nothing was posed", () => {
    expect(summarizeCoverage(ledger({ entries: [] }))).toBe(
      "3 of 12 sources consulted",
    );
  });
});

describe("the missing-ledger copy", () => {
  it("reads as information, not as a failure", () => {
    // A run with no ledger is the ORDINARY case — only plan-mode steps and the
    // deliverable preset write one — so the 404 must never reach an error toast.
    expect(NO_COVERAGE).toBe("No coverage recorded for this run.");
    expect(NO_COVERAGE.toLowerCase()).not.toContain("error");
    expect(NO_COVERAGE.toLowerCase()).not.toContain("failed");
  });
});
