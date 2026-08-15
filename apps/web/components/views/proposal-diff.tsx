"use client";

/**
 * The one way a proposed change is drawn, wherever a person meets it.
 *
 * There is a single renderer here on purpose. This markup had drifted into two
 * copies — one private to the chat card, one shared by the document panel, the
 * activity feed and the workflow approval — which is how a proposal made from a
 * subject sidebar could read as a wall of grey text while the identical proposal
 * made from the rail read as a red/green diff. The classes are styled once in
 * globals.css (`.diff`, `.diff-line add|del|ctx|hunk|file`, `.proposal-note`),
 * so a surface that wants the diff narrower says so there, not by rebuilding it.
 *
 * The per-hunk reviewer in `document-review.tsx` is deliberately *not* folded in
 * here: it emits the same `diff-line` classes but is a different offer — accept
 * or reject each hunk — and it owns its own decision state. What keeps the two
 * from colliding is upstream, in `documents.tsx`: an edit the inline reviewer
 * has taken is listed in the `hidden` set the chat panel filters by, so exactly
 * one of them ever asks about a given proposal.
 */

/**
 * Where the diff starts inside a preview, or -1.
 *
 * A preview may be a bare unified diff (a document edit, a project file write)
 * or a sentence with a diff under it (a dashboard edit, where "it shows a table
 * and would show a bar" is the summary a reviewer reads first and the spec diff
 * is the detail underneath). Splitting rather than choosing is what lets a tool
 * offer both without the prose being coloured as context lines.
 */
function diffStart(lines: readonly string[]): number {
  // A `@@` hunk header is the thing that makes it a diff; without one this is
  // prose, however many dashes it happens to contain.
  if (!lines.some((line) => line.startsWith("@@"))) return -1;
  const header = lines.findIndex(
    (line, index) => line.startsWith("--- ") && lines[index + 1]?.startsWith("+++ "),
  );
  return header >= 0 ? header : lines.findIndex((line) => line.startsWith("@@"));
}

/** The class that colours one unified-diff line. */
function lineKind(line: string): string {
  if (line.startsWith("+++") || line.startsWith("---")) return "file";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "ctx";
}

/**
 * Render a proposal preview: any leading sentence as a note, the unified diff
 * below it in red and green. A preview with no diff in it is all note, which is
 * the honest rendering of "Move Ship it from Todo to Done".
 */
export function ProposalDiff({ preview }: { preview: string }) {
  const lines = preview.split("\n");
  const at = diffStart(lines);
  const note = (at < 0 ? preview : lines.slice(0, at).join("\n")).trim();
  const diff = at < 0 ? [] : lines.slice(at);
  return (
    <>
      {note && <div className="proposal-note">{note}</div>}
      {diff.length > 0 && (
        <div className="diff">
          {diff.map((line, index) => (
            <div key={index} className={`diff-line ${lineKind(line)}`}>
              {line || " "}
            </div>
          ))}
        </div>
      )}
    </>
  );
}
