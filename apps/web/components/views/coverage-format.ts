import type { CoverageLedger } from "@workspace/api-client";

/**
 * The one line a collapsed coverage ledger shows.
 *
 * It says what was LOOKED AT, never whether the answer is right. Nothing in
 * the ledger is an entailment check — `supported` means retrieval returned
 * something — so a summary that read like a verdict would be the product
 * overstating itself in its own smallest print.
 */
export function summarizeCoverage(ledger: CoverageLedger): string {
  const posed = ledger.entries.filter((entry) => entry.stance === "neutral");
  const unsupported = posed.filter((entry) => !entry.supported).length;
  const parts = [
    `${ledger.consulted_count} of ${ledger.in_scope_count} sources consulted`,
  ];
  if (posed.length > 0) {
    parts.push(
      unsupported === 0
        ? `${posed.length} sub-questions, all supported`
        : `${unsupported} of ${posed.length} sub-questions unsupported`,
    );
  }
  if (ledger.one_sided) {
    parts.push("one-sided: no counter-evidence found");
  } else if (ledger.shape === "debate") {
    parts.push("both sides retrieved");
  }
  return parts.join(" · ");
}

/**
 * What to show when the coverage fetch 404s.
 *
 * A run with no ledger is the ORDINARY case — only plan-mode steps and the
 * deliverable preset write one — so the 404 is information, not a failure, and
 * it must never reach an error toast.
 */
export const NO_COVERAGE = "No coverage recorded for this run.";
