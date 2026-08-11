import type { CitationCheck } from "@workspace/api-client";

/**
 * Reading, and saying, the citation validator's verdict.
 *
 * The contract the chat prompt sets is narrow and checkable: attach `[n]` after
 * each claim supported by passage n, and use no `[n]` that does not name a
 * supplied passage. `services/citations.py` checks it on every completed run
 * with no model in the loop, and until now reported to an audit row. A contract
 * whose enforcement is invisible is indistinguishable from no contract, which
 * is the whole reason this file exists.
 *
 * Two rules hold it together.
 *
 * **Nothing is invented.** The report has exactly eight fields and this module
 * uses only those. "Which claims were checked" is `marker_count`; "which
 * passed" is `cited`; "which did not" is `out_of_range` and `malformed`. There
 * is no per-claim verdict anywhere in the system, so this must not imply one —
 * the validator knows where the markers are, not what sentences they end.
 *
 * **A verdict is only shown when there was something to check.** Every casual
 * turn retrieves nothing, and a badge on all of them trains people to stop
 * reading badges. `evidence_count === 0` renders nothing at all.
 */

/**
 * The `run.citations` payload, validated.
 *
 * The event arrives as `Record<string, unknown>` and drives a *warning*, so a
 * malformed payload must produce no verdict rather than a confident one built
 * out of `undefined`. Missing `evidence_count` or `valid` is fatal to the read;
 * missing lists degrade to empty, which is what they mean.
 */
export function readCitationCheck(data: Record<string, unknown>): CitationCheck | null {
  const evidenceCount = data.evidence_count;
  if (typeof evidenceCount !== "number" || typeof data.valid !== "boolean") return null;
  return {
    evidence_count: evidenceCount,
    marker_count: typeof data.marker_count === "number" ? data.marker_count : 0,
    cited: numbers(data.cited),
    out_of_range: numbers(data.out_of_range),
    uncited: numbers(data.uncited),
    malformed: strings(data.malformed),
    valid: data.valid,
    summary: typeof data.summary === "string" ? data.summary : "",
  };
}

function numbers(value: unknown): number[] {
  return Array.isArray(value) ? value.filter((item): item is number => typeof item === "number") : [];
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

/** How loudly to say it. `fabricated` is the only one that is a defect. */
export type CitationTone = "fabricated" | "clean" | "uncited";

export type CitationVerdict = {
  tone: CitationTone;
  /** The headline, e.g. "1 citation does not match a supplied passage". */
  title: string;
  /** The part a reader acts on: which markers, and what they were checked against. */
  detail: string;
};

/** `[4]`, or `[4] and [7]`, or `[4], [7] and [9]` — a list a person reads aloud. */
function markerList(numbers: number[]): string {
  const marks = numbers.map((n) => `[${n}]`);
  if (marks.length <= 1) return marks.join("");
  return `${marks.slice(0, -1).join(", ")} and ${marks[marks.length - 1]}`;
}

function quotedList(values: string[]): string {
  const quoted = values.map((value) => `“${value}”`);
  if (quoted.length <= 1) return quoted.join("");
  return `${quoted.slice(0, -1).join(", ")} and ${quoted[quoted.length - 1]}`;
}

function plural(count: number, one: string, many: string): string {
  return count === 1 ? one : many;
}

/**
 * The verdict as a reader should meet it, or null when there is nothing to say.
 *
 * Null only when no passages were supplied: there was no retrieval, so there is
 * no contract to have kept, and a badge on every "hello" would teach people to
 * stop reading badges before the one that matters arrives.
 *
 * A clean answer *does* get a line, quietly. The alternative — showing the
 * verdict only on violations — leaves "checked and clean" and "never checked"
 * looking identical, and a check you cannot see run is a check you cannot
 * believe in when it stays silent.
 */
export function describeCitationCheck(report: CitationCheck): CitationVerdict | null {
  if (report.evidence_count === 0) return null;

  const supplied = `${report.evidence_count} ${plural(
    report.evidence_count,
    "passage was",
    "passages were",
  )} supplied`;

  if (report.out_of_range.length > 0) {
    const count = report.out_of_range.length;
    return {
      tone: "fabricated",
      title: `${count} ${plural(count, "citation does", "citations do")} not match a supplied passage`,
      detail:
        `${markerList(report.out_of_range)} ${plural(count, "names", "name")} ` +
        `a passage that was never supplied — ${supplied}. Check ` +
        `${plural(count, "that claim", "those claims")} before relying on ${plural(count, "it", "them")}.`,
    };
  }

  if (report.malformed.length > 0) {
    const count = report.malformed.length;
    return {
      tone: "fabricated",
      title: `${count} ${plural(count, "citation", "citations")} could not be read`,
      detail:
        `${quotedList(report.malformed)} ${plural(count, "is", "are")} shaped like a ` +
        `citation but names no passage. ${supplied[0].toUpperCase()}${supplied.slice(1)}.`,
    };
  }

  if (report.cited.length === 0) {
    return {
      tone: "uncited",
      title: "This answer cites nothing",
      detail: `${supplied}, and the answer marks no claim against any of them.`,
    };
  }

  const checked = `${report.marker_count} ${plural(
    report.marker_count,
    "citation",
    "citations",
  )} checked against ${report.evidence_count} ${plural(
    report.evidence_count,
    "passage",
    "passages",
  )}`;
  return {
    tone: "clean",
    title: `Citations check out — ${report.cited.length} of ${report.evidence_count} passages cited`,
    detail:
      report.uncited.length > 0
        ? `${checked}; every marker resolves. Passages left uncited: ${markerList(report.uncited)}.`
        : `${checked}; every marker resolves.`,
  };
}
