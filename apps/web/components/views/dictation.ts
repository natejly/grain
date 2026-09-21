/**
 * Voice dictation's pure half — the reducer the mic button in chat.tsx
 * adapts the Web Speech API onto. The reducer is the unit-tested truth; the
 * button is a thin adapter, because the API itself cannot be exercised in CI
 * (headless Chrome exposes the constructor and then fails recognition with a
 * network error at runtime — presence without function).
 *
 * Everything is browser-local: the Web Speech API does its own capture, and
 * no audio or transcript ever leaves the page except as ordinary typed draft
 * text the user then chooses to send.
 */

/** One recognition alternative, as the reducer sees it. */
export type DictationResult = {
  transcript: string;
  isFinal: boolean;
};

export type DictationState = {
  /** What the composer held when the mic went live. Never rewritten. */
  base: string;
  /** Finalised speech, accumulated across result events. */
  committed: string;
  /** The in-flight hypothesis — replaced wholesale by each event, never
   *  appended, which is the Web Speech `resultIndex` contract. */
  interim: string;
};

/**
 * SSR-safe feature detect: constructor presence on whatever `window`-like
 * thing the caller has. `unknown` so the server (no window at all) and the
 * test (a plain object) both answer without a reference error.
 */
export function supportsDictation(w: unknown): boolean {
  if (typeof w !== "object" || w === null) return false;
  const candidate = w as Record<string, unknown>;
  return (
    typeof candidate.webkitSpeechRecognition === "function" ||
    typeof candidate.SpeechRecognition === "function"
  );
}

/** A fresh session over the current draft. Restarting after a stop begins a
 *  new base from whatever the draft says now — edits included. */
export function beginDictation(draft: string): DictationState {
  return { base: draft, committed: "", interim: "" };
}

function joinSpoken(left: string, right: string): string {
  if (!left) return right;
  if (!right) return left;
  return `${left.trimEnd()} ${right.trimStart()}`;
}

/**
 * Fold one result event's NEW results (the slice from `event.resultIndex`)
 * into the state: finals append to `committed` with single-space joining,
 * and the non-final tail replaces `interim` wholesale.
 */
export function applyResult(
  state: DictationState,
  results: DictationResult[],
): DictationState {
  let committed = state.committed;
  let interim = "";
  for (const result of results) {
    if (result.isFinal) {
      committed = joinSpoken(committed, result.transcript);
    } else {
      interim = joinSpoken(interim, result.transcript);
    }
  }
  return { base: state.base, committed, interim };
}

/**
 * What a result event may write over the draft.
 *
 * The mic's stop-on-manual-edit guard is a post-commit effect, so a
 * keystroke's setDraft can be queued — invisible to everything but a
 * functional updater — when a result event lands. Overwriting then would
 * silently erase the typed character with a `composed` built from the frozen
 * base. So the write is conditional: only a draft that still reads exactly
 * as the last thing dictation wrote (`lastComposed`) is dictation's to
 * replace; anything else is the person typing, and their text stands (the
 * stop-on-edit effect then ends the session, because the advanced composed
 * ref no longer matches the draft it kept).
 */
export function resultWrite(
  currentDraft: string,
  lastComposed: string,
  composed: string,
): string {
  return currentDraft === lastComposed ? composed : currentDraft;
}

/**
 * What the composer should say right now: the untouched base, a smart
 * separator (nothing after "" or trailing whitespace, one space mid-word),
 * then the speech so far.
 */
export function composedDraft(state: DictationState): string {
  const spoken = joinSpoken(state.committed, state.interim);
  if (!spoken) return state.base;
  if (state.base === "" || /\s$/.test(state.base)) return state.base + spoken;
  return `${state.base} ${spoken}`;
}
