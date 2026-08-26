"use client";

import type { CoworkingPresence } from "@workspace/api-client";
import { Eye, Pencil } from "lucide-react";
import { Fragment } from "react";

/**
 * The Google-Docs layer over the document editor: remote carets with name
 * flags, live selections, and the "following" reading of a coworker's draft
 * as they type it — before anything is saved.
 *
 * The mechanics are the classic textarea trick: a *mirror* — a read-only
 * element wearing the textarea's exact font, padding and wrap — renders the
 * same text with markers spliced in at the remote offsets, and sits behind
 * the real textarea with its own text painted transparent. Identical layout
 * in, identical positions out; no per-character measurement, no drift.
 */

/** The slices a marker splits one text into. Pure, and tested as such. */
export type CaretSplit = {
  before: string;
  selected: string;
  after: string;
};

/**
 * Split `text` for one remote cursor/selection. Offsets are clamped — a
 * remote cursor can reference a draft a keystroke newer than what this
 * client holds, and a caret pinned to the end is the right degradation.
 */
export function splitForCaret(
  text: string,
  cursor?: number,
  selectionStart?: number,
  selectionEnd?: number,
): CaretSplit {
  const clamp = (value: number) => Math.max(0, Math.min(text.length, value));
  const start = clamp(selectionStart ?? cursor ?? text.length);
  const end = clamp(selectionEnd ?? cursor ?? text.length);
  const [low, high] = start <= end ? [start, end] : [end, start];
  return {
    before: text.slice(0, low),
    selected: text.slice(low, high),
    after: text.slice(high),
  };
}

/**
 * Whether a coworker's presence is an *editing* one — the signal that turns
 * a passive avatar into a live draft worth following.
 */
export function isEditing(presence: CoworkingPresence): boolean {
  return Boolean(presence.state.typing) || typeof presence.state.draft === "string";
}

/** The live draft to follow, if exactly the newest editor is carrying one. */
export function liveDraftOf(others: CoworkingPresence[]): CoworkingPresence | null {
  const editors = others.filter(
    (presence) => typeof presence.state.draft === "string",
  );
  if (editors.length === 0) return null;
  return editors.reduce((newest, presence) =>
    presence.updated_at > newest.updated_at ? presence : newest,
  );
}

/**
 * The mirror with markers, one layer per remote presence. Mounted inside the
 * editor's wrapper, behind the textarea; scroll is synced by the caller
 * because only the caller holds the textarea.
 */
export function RemoteCaretLayer({
  text,
  others,
  scrollRef,
}: {
  /** The text the LOCAL pane is showing — markers are spliced into this. */
  text: string;
  others: CoworkingPresence[];
  /** Handed to the container so the textarea's onScroll can mirror into it. */
  scrollRef?: React.RefObject<HTMLDivElement | null>;
}) {
  const carets = others.filter(
    (presence) =>
      typeof presence.state.cursor === "number" ||
      typeof presence.state.selection_start === "number",
  );
  if (carets.length === 0) return null;
  return (
    <div className="document-caret-layers" aria-hidden ref={scrollRef}>
      {carets.map((presence) => {
        const split = splitForCaret(
          text,
          presence.state.cursor,
          presence.state.selection_start,
          presence.state.selection_end,
        );
        return (
          <pre className="document-caret-mirror" key={presence.actor_id}>
            <Fragment>
              {split.before}
              {split.selected ? (
                <span className="remote-selection">{split.selected}</span>
              ) : null}
              <span
                className={
                  presence.actor_kind === "agent"
                    ? "remote-caret agent"
                    : "remote-caret"
                }
              >
                <span className="remote-caret-flag">{presence.actor_label}</span>
              </span>
              {split.after}
            </Fragment>
          </pre>
        );
      })}
    </div>
  );
}

/**
 * The banner over a document someone else is working in. Two honest modes,
 * not a merge that is not there: following (they type, you watch, your pane
 * is read-only until you touch it) and both-editing (you both typed; last
 * save wins, and the banner says so before it happens).
 */
export function LiveEditBanner({
  editor,
  following,
}: {
  editor: CoworkingPresence;
  following: boolean;
}) {
  return (
    <div
      className={following ? "document-live-banner following" : "document-live-banner clash"}
      role="status"
    >
      {following ? <Eye size={14} aria-hidden /> : <Pencil size={14} aria-hidden />}
      {following ? (
        <span>
          <strong>{editor.actor_label}</strong> is editing — you’re seeing
          their draft live. Start typing to edit your own copy.
        </span>
      ) : (
        <span>
          <strong>{editor.actor_label}</strong> is editing this too. Whoever
          saves last wins — consider waiting for their save.
        </span>
      )}
    </div>
  );
}
