import type { CitationCheck, GroundingCheck, RepairCheck, SentenceGrounding } from "@workspace/api-client";

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
 * **Nothing is invented.** Every field this module reads is a field the
 * validator produced. "Which claims were checked" is `marker_count`; "which
 * passed" is `cited`; "which did not" is `out_of_range` and `malformed`. There
 * IS now a per-sentence verdict — `grounding`, from `grade_grounding` — and it
 * is exactly one thing: a LEXICAL SUPPORT TEST. "Verified" means the words that
 * sentence uses are present in the passage it cites. It does not mean the
 * sentence is true: a negated or misattributed claim assembled from the
 * passage's own words passes, and a correct paraphrase in different words
 * fails. Every string below says "supported by the words of the passage it
 * cites" or something equally narrow, and none of them says "true", "correct"
 * or "fact-checked". If a copy change cannot keep that line, the drawer is the
 * part to cut — not the honesty.
 *
 * **A verdict is only shown when there was something to check.** Every casual
 * turn retrieves nothing, and a badge on all of them trains people to stop
 * reading badges. `evidence_count === 0` renders nothing at all, and a
 * grounding block with `scored === 0` renders nothing either — nothing in that
 * answer made a checkable claim, which is not the same as 0% grounded.
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
    // Each degrades to null on anything malformed, independently of the eight
    // fields above: a broken grounding block must cost the drawer, never the
    // plate that was already correct.
    grounding: readGrounding(data.grounding),
    repair: readRepair(data.repair),
  };
}

const VERDICTS = [
  "verified",
  "cited_unsupported",
  "uncited",
  "ignored",
  "attributed",
] as const;

/** The grounding block, or null when there is not a usable one. */
export function readGrounding(value: unknown): GroundingCheck | null {
  if (!isRecord(value)) return null;
  if (typeof value.score !== "number" || typeof value.scored !== "number") return null;
  return {
    score: value.score,
    scored: value.scored,
    verified: count(value.verified),
    cited_unsupported: count(value.cited_unsupported),
    uncited: count(value.uncited),
    ignored: count(value.ignored),
    // Both default to the pre-field reading — no attributed sentences, nothing
    // past the cap — so a verdict stored before they existed renders exactly
    // as it did, rather than as a confident zero about a question nobody asked.
    attributed: count(value.attributed),
    floor: count(value.floor),
    truncated: value.truncated === true,
    n_sentences: count(value.n_sentences),
    sentences_truncated: value.sentences_truncated === true,
    sentences: readSentences(value.sentences),
  };
}

/**
 * The sentences, dropping any entry that is not fully readable.
 *
 * Dropped rather than defaulted: a sentence rendered from `undefined` offsets
 * with an invented verdict is a confident claim nobody made, which is the one
 * thing this file exists to prevent.
 */
function readSentences(value: unknown): SentenceGrounding[] {
  if (!Array.isArray(value)) return [];
  const out: SentenceGrounding[] = [];
  for (const entry of value) {
    if (!isRecord(entry)) continue;
    if (typeof entry.start !== "number" || typeof entry.end !== "number") continue;
    if (typeof entry.coverage !== "number") continue;
    if (typeof entry.verdict !== "string") continue;
    if (!(VERDICTS as readonly string[]).includes(entry.verdict)) continue;
    out.push({
      start: entry.start,
      end: entry.end,
      text: typeof entry.text === "string" ? entry.text : "",
      verdict: entry.verdict,
      citations: numbers(entry.citations),
      fabricated: numbers(entry.fabricated),
      coverage: entry.coverage,
      missing_numerals: strings(entry.missing_numerals),
    });
  }
  return out;
}

/** The repair record, or null when there is not a usable one. */
export function readRepair(value: unknown): RepairCheck | null {
  if (!isRecord(value)) return null;
  if (typeof value.attempted !== "boolean" || typeof value.applied !== "boolean") return null;
  return {
    attempted: value.attempted,
    applied: value.applied,
    reason: typeof value.reason === "string" ? value.reason : "",
    score_before: count(value.score_before),
    score_after: count(value.score_after),
    unsupported_before: count(value.unsupported_before),
    unsupported_after: count(value.unsupported_after),
    // False on a record written before the field existed, which reads as "the
    // whole answer was graded" — the behaviour of every report until the cap
    // was made visible.
    tail_ungraded: value.tail_ungraded === true,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function count(value: unknown): number {
  return typeof value === "number" ? value : 0;
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

/** What each per-sentence verdict is called, in the reader's words. */
export const SENTENCE_LABELS: Record<string, string> = {
  verified: "Verified",
  cited_unsupported: "Cited, not supported",
  uncited: "Uncited",
  ignored: "Not scored",
  // Deliberately not a shade of "verified". This sentence cites a web page
  // whose text never reached this app, so there was nothing here to check it
  // against — the publisher asserts it, and that is the whole claim.
  attributed: "Attributed, unchecked",
};

/** The one line that says what an attributed sentence rests on. */
export const ATTRIBUTED_MEANING =
  "Attributed means the source's publisher asserts it and nothing here checked " +
  "it: the page's own words never reached this app, so those sentences are " +
  "left out of the percentage rather than counted as verified.";

export type GroundingTone = "grounded" | "partial" | "ungrounded";

export type GroundingVerdict = {
  tone: GroundingTone;
  /** The headline: the percentage and the counts behind it. */
  title: string;
  /** What "verified" means here. It says support, never truth. */
  detail: string;
  score: number;
};

/**
 * The grounding block as a reader should meet it, or null when there is
 * nothing honest to say.
 *
 * Null when the answer was never graded, and null at `scored === 0` with
 * nothing attributed — an answer whose sentences made no checkable claim has
 * not scored 0%, it has not been scored. Rendering those two the same is
 * exactly the over-reading this module refuses.
 *
 * An answer built ENTIRELY from web citations lands at `scored === 0` with
 * attributed sentences, and that is worth saying out loud rather than
 * swallowing: it is the case the old grader reported as 100%, because a hosted
 * search hands back no page text and the "passage" each sentence was checked
 * against was a slice of the sentence itself.
 */
export function describeGrounding(report: CitationCheck): GroundingVerdict | null {
  const grounding = report.grounding;
  const attributed = grounding?.attributed ?? 0;
  // Null only when there is nothing to say at all. `scored === 0` WITH
  // attributed sentences is something to say, and the honest thing to say:
  // the answer cited web pages this app never read.
  if (!grounding || (grounding.scored === 0 && attributed === 0)) return null;

  const { verified, scored, cited_unsupported: unsupported, uncited } = grounding;
  const share = Math.round(grounding.score * 100);
  const counted = `${verified} of ${scored} checked ${plural(scored, "sentence", "sentences")} verified`;
  // The one sentence that has to stay true no matter how the copy moves.
  const meaning =
    "Verified means the words of the sentence appear in the passage it cites — " +
    "a check of support, not of truth.";
  // Two facts the headline percentage cannot carry on its own, and both of
  // which make it read stronger than it is: sentences resting on a web
  // source's own assertion, and a tail of the answer past the grader's cap
  // that was never looked at.
  const ungraded = grounding.sentences_truncated
    ? Math.max((grounding.n_sentences ?? 0) - grounding.sentences.length, 0)
    : 0;
  const caveats: string[] = [];
  if (attributed > 0) {
    caveats.push(
      `${attributed} attributed to web ${plural(attributed, "source", "sources")}, unchecked`,
    );
  }
  if (ungraded > 0) {
    caveats.push(`${ungraded} after the first ${grounding.sentences.length} not graded`);
  }
  const suffix = caveats.length > 0 ? `; ${caveats.join("; ")}` : "";
  const notes =
    (attributed > 0 ? ` ${ATTRIBUTED_MEANING}` : "") +
    (ungraded > 0
      ? ` The grader stops at ${grounding.sentences.length} sentences, so the rest of this answer was not looked at.`
      : "");

  if (scored === 0) {
    return {
      tone: "partial",
      score: 0,
      title: `Grounding — nothing checkable${suffix}`,
      detail: `No sentence in this answer could be checked locally.${notes}`,
    };
  }
  if (unsupported > 0) {
    return {
      tone: "ungrounded",
      score: grounding.score,
      title: `Grounding ${share}% — ${counted}, ${unsupported} cited but not supported${suffix}`,
      detail:
        `${unsupported} ${plural(unsupported, "sentence cites", "sentences cite")} a passage ` +
        `whose words do not carry ${plural(unsupported, "it", "them")}. ${meaning}${notes}`,
    };
  }
  if (uncited > 0) {
    return {
      tone: "partial",
      score: grounding.score,
      title: `Grounding ${share}% — ${counted}, ${uncited} uncited${suffix}`,
      detail:
        `${uncited} ${plural(uncited, "sentence makes", "sentences make")} a claim with no ` +
        `[n] marker, so there was nothing to check ${plural(uncited, "it", "them")} against. ${meaning}${notes}`,
    };
  }
  return {
    tone: attributed > 0 || ungraded > 0 ? "partial" : "grounded",
    score: grounding.score,
    title: `Grounding ${share}% — ${counted}${suffix}`,
    detail: `${meaning}${notes}`,
  };
}

/**
 * What the repair pass did, in one line, or null when there is nothing to say.
 *
 * Only three outcomes are worth a reader's attention: it rewrote something, it
 * tried and threw the rewrite away, or it could not run at all on this
 * deployment. "Nothing to repair" is silence — the ordinary case, and a line
 * on every clean answer is how a real notice gets tuned out.
 */
export function describeRepair(repair: RepairCheck | null | undefined): string | null {
  if (!repair) return null;
  if (repair.applied) {
    const fixed = repair.unsupported_before - repair.unsupported_after;
    return (
      `${fixed} unsupported ${plural(fixed, "sentence was", "sentences were")} rewritten ` +
      `against the cited passages before this answer was saved.`
    );
  }
  if (repair.reason === "no_provider") {
    return "The rewrite pass is not configured on this deployment, so nothing was corrected.";
  }
  if (repair.attempted) {
    return "A rewrite was attempted and discarded because it was not better than the original.";
  }
  return null;
}
