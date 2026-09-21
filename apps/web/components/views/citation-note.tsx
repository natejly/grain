"use client";

import type { CitationCheck } from "@workspace/api-client";
import { ChevronDown, ShieldAlert, ShieldCheck } from "lucide-react";
import { useState } from "react";
import {
  SENTENCE_LABELS,
  describeCitationCheck,
  describeGrounding,
  describeRepair,
} from "./citation-format";

/**
 * The citation verdict and the grounding drawer, for the two surfaces that
 * show them: the chat transcript and the grounded-answer receipt viewer.
 *
 * One renderer rather than two, because these two surfaces render the SAME
 * stored payload — `messages.citation_report_json` and
 * `grounded_receipts.report_json` are both `grounded.compose_report()` output
 * — and a second copy of this markup is how the transcript and the ledger come
 * to disagree about what a verdict looks like.
 *
 * On honesty, which is the whole point of the drawer: `verified` is a LEXICAL
 * SUPPORT test. The words of that sentence are in the passage it cites. It is
 * not a fact check, it cannot see a negation, and every string here comes from
 * `citation-format.ts`, which is where that wording is kept in one place.
 */

/**
 * The citation validator's verdict on an answer, where the answer is.
 *
 * Not a decoration. `services/citations.py` is what backs the product's claim
 * that a `[n]` in an answer names a passage that really was retrieved, and its
 * report went to an audit row for a year — so a fabricated `[4]` in an answer
 * built from three passages reached the reader looking exactly like a real
 * citation, with the checker's objection filed where nobody looks.
 */
export function CitationVerdictNote({ report }: { report: CitationCheck }) {
  const verdict = describeCitationCheck(report);
  if (!verdict) return null;
  const Icon = verdict.tone === "clean" ? ShieldCheck : ShieldAlert;
  return (
    <div
      className={`citation-check ${verdict.tone}`}
      // Announced only for the one tone that is a defect. An uncited passage
      // is not a contract violation — the validator says so — and interrupting
      // a screen reader for every tool-driven turn is how a real alert gets
      // tuned out before it ever fires.
      role={verdict.tone === "fabricated" ? "alert" : undefined}
    >
      <Icon size={14} aria-hidden="true" />
      <div>
        <strong>{verdict.title}</strong>
        <span>{verdict.detail}</span>
      </div>
    </div>
  );
}

/**
 * The per-sentence verdicts, folded away under the plate.
 *
 * A DRAWER, not inline badges in the prose, and that is a deliberate choice
 * rather than a shortcut. The grader hands back character offsets into the
 * answer; decorating the answer in place would mean annotating ranges inside a
 * ReactMarkdown render, which cannot be done without re-parsing the markdown
 * and would break fences, list items and links the moment a span crossed one.
 * The drawer carries the same offsets and the same badges, and the markdown
 * render upstream stays byte-identical.
 *
 * `ignored` sentences are left out: they made no checkable claim, and a row
 * saying so for every "Here is what I found." is noise that buries the rows
 * that matter. `attributed` sentences are NOT left out — they made a claim,
 * and the fact that nothing here could check it is precisely what a reader
 * needs to see, with its own badge rather than a quieter shade of verified.
 */
export function GroundingDrawer({ report }: { report: CitationCheck }) {
  const [open, setOpen] = useState(false);
  const verdict = describeGrounding(report);
  if (!verdict) return null;
  const repair = describeRepair(report.repair);
  const grounding = report.grounding;
  const sentences = (grounding?.sentences ?? []).filter(
    (sentence) => sentence.verdict !== "ignored",
  );
  // The tail past the grader's cap. Said in the drawer as well as the plate,
  // because the list of rows below is otherwise a complete-looking account of
  // an answer the grader only read the front of.
  const ungraded = grounding?.sentences_truncated
    ? Math.max((grounding.n_sentences ?? 0) - grounding.sentences.length, 0)
    : 0;

  return (
    <div className={`grounding-plate ${verdict.tone}`}>
      <button
        type="button"
        className={`grounding-summary ${verdict.tone}`}
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <span>{verdict.title}</span>
        <ChevronDown size={13} aria-hidden="true" />
      </button>
      {open && (
        <div className="grounding-detail">
          <p className="grounding-meaning">{verdict.detail}</p>
          {repair && <p className="grounding-repair">{repair}</p>}
          <ol className="grounding-sentences">
            {sentences.map((sentence) => (
              <li
                key={`${sentence.start}-${sentence.end}`}
                className={`grounding-sentence ${sentence.verdict}`}
              >
                <span className="grounding-badge">
                  {SENTENCE_LABELS[sentence.verdict] ?? sentence.verdict}
                </span>
                {sentence.citations.length > 0 && (
                  <span className="grounding-markers">
                    {sentence.citations.map((n) => `[${n}]`).join("")}
                  </span>
                )}
                <span className="grounding-text">{sentence.text}</span>
              </li>
            ))}
            {ungraded > 0 && (
              <li className="grounding-sentence ungraded">
                <span className="grounding-badge">Not graded</span>
                <span className="grounding-text">
                  {ungraded} further {ungraded === 1 ? "sentence" : "sentences"} after
                  the first {grounding?.sentences.length} were not checked.
                </span>
              </li>
            )}
          </ol>
        </div>
      )}
    </div>
  );
}
