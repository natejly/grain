"use client";

import type { Bootstrap, Citation, Conversation, GeneratedApp, Source } from "@workspace/api-client";
import { Maximize2, Minimize2 } from "lucide-react";
import {
  Fragment,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent,
  type ReactNode,
} from "react";
import { ChatPane } from "../chat-pane";
import { AttachmentPane } from "./attachment-pane";
import type { DashboardPinning } from "./dashboard-pin-bar";
import {
  SPLIT_SIZES_KEY,
  applyDelta,
  equalSizes,
  parseStoredSizes,
  serializeStoredSizes,
} from "./split-sizes";

export type ChatPaneRef = { id: string; conversationId: string };

/**
 * An attached file open for editing beside the chat.
 *
 * A separate list from `panes` rather than a widened union, and deliberately
 * not persisted. The chat panes are a layout the user curates and expects back
 * after a reload; a file pane is a working surface tied to the thread in front
 * of them — reviving it on load would reopen files they had closed. Keeping it
 * out of `chat-panes` also means the pane store, its pruning and the saved
 * layouts keep the one shape they already have.
 */
export type FilePaneRef = { id: string; documentId: string; filename: string };

/** How many attached files may be open at once, beside whatever chats are.
 *  Two, not the chat panes' three: a file column and a chat column are
 *  competing for the same width, and past this nothing is wide enough to
 *  read. */
export const MAX_FILE_PANES = 2;

export type ChatSplitProps = {
  /**
   * The shell's own chat, rendered byte-for-byte as today. It is passed in
   * rather than reconstructed here so the single-pane user's ChatView keeps the
   * shell's rich `onRunSettled` and rail wiring — the no-regression guarantee.
   */
  primary: ReactNode;
  /** The EXTRA panes beside the primary; empty === today's single-pane shell. */
  panes: ChatPaneRef[];
  /** Attached files open for editing, as further columns in the same split. */
  filePanes?: FilePaneRef[];
  closeFilePane?: (paneId: string) => void;
  conversations: Conversation[];
  bootstrap: Bootstrap | null;
  sources: Source[];
  apps: GeneratedApp[];
  openCitation: (citation: Citation) => Promise<void>;
  closePane: (paneId: string) => void;
  focusedPane: string | null;
  focusPane: (paneId: string | null) => void;
  onSettled: () => Promise<void> | void;
  onApprovalChanged: (updated: Conversation) => void;
  /** The shell's one pin bundle, handed to every extra pane — a dashboard
   *  made in a side pane deserves the same finish-the-job bar. */
  pinning?: DashboardPinning;
  /**
   * Bumped by the shell after it writes ratios to storage on the split's
   * behalf (applying a saved layout). The split re-reads the store when the
   * column count changes; a bump forces the same re-read when the count did
   * NOT change, so an applied layout's ratios still land.
   */
  resetKey?: number;
  /**
   * The ratios an applied layout wants for the count it restores, delivered
   * WITH the `resetKey` bump rather than only through localStorage: in
   * private mode the storage write is silently swallowed, and a re-read
   * would land on stale ratios while the shell believed the layout applied.
   * Consulted only on a forced re-read, and only when the length matches
   * the rendered column count.
   */
  forcedSizes?: number[] | null;
};

const MIN_PERCENT = 15;

/**
 * The ratios a `count`-column split last held, or an even split. Guarded for
 * private-mode / server render like the pane layout itself: a throwing or
 * absent store is simply the even share, never a crash. The decode lives in
 * `split-sizes` so it can be tested without a DOM; this only adds the guard.
 * Exported for the shell, which snapshots the current ratios into a saved
 * layout with the same read the split itself trusts.
 */
export function readStoredSizes(count: number): number[] {
  if (typeof window === "undefined") return equalSizes(count);
  try {
    return parseStoredSizes(window.localStorage.getItem(SPLIT_SIZES_KEY), count);
  } catch {
    return equalSizes(count);
  }
}

/**
 * The chat surface: the shell's primary chat, plus any extra concurrent panes
 * in a resizable horizontal split.
 *
 * When there are no extra panes it renders the primary ChatView alone with no
 * wrapper — the single-pane user sees exactly today's shell. The split, its
 * dividers and their resize state only exist once a second pane is open, so the
 * common path costs nothing and cannot regress.
 *
 * The divider is hand-rolled (a pointer-drag over flex-grow ratios) rather than
 * a docking dependency: the requirement is a horizontal split of a few panes,
 * which CSS flex already does — the codebase splits the Documents view the same
 * way. Sizes are ratios, so adding or closing a pane re-shares the width evenly
 * without measuring anything.
 */
export function ChatSplit({
  primary,
  panes,
  filePanes = [],
  closeFilePane,
  conversations,
  bootstrap,
  sources,
  apps,
  openCitation,
  closePane,
  focusedPane,
  focusPane,
  onSettled,
  onApprovalChanged,
  pinning,
  resetKey,
  forcedSizes,
}: ChatSplitProps) {
  // Only panes whose conversation still exists. A conversation deleted between
  // sessions leaves a persisted pane pointing at nothing; dropping it here keeps
  // the split honest even before the workspace prunes the stored layout.
  const resolved = panes
    .map((pane) => ({
      pane,
      conversation: conversations.find((item) => item.id === pane.conversationId),
    }))
    .filter((entry): entry is { pane: ChatPaneRef; conversation: Conversation } =>
      Boolean(entry.conversation),
    );

  const columnCount = 1 + resolved.length + filePanes.length;
  const containerRef = useRef<HTMLDivElement>(null);
  const [sizes, setSizes] = useState<number[]>(() => readStoredSizes(columnCount));
  // The ratios to render for the CURRENT column count. A pane opened or closed
  // changes `columnCount` one render before the effect below rewrites `sizes`;
  // rendering straight from `sizes` would commit a mismatched-length array for
  // that frame — the new pane at `flexGrow: undefined`, i.e. zero width, until
  // the next paint. Falling back to an even share here means the DOM never sees
  // the mismatch, and the effect only persists the reconciliation for the drag.
  const effectiveSizes = useMemo(
    () => (sizes.length === columnCount ? sizes : equalSizes(columnCount)),
    [sizes, columnCount],
  );
  // A count change lands on the ratios this count last held, not on a blind
  // even reset: the store is keyed per column count, so closing a third pane
  // restores the drag the user made at two. A `resetKey` bump re-reads
  // unconditionally — an applied layout may write this SAME count's ratios,
  // which the length check alone would never notice; the ref keeps the mount
  // run on the identity-preserving path.
  const appliedReset = useRef(resetKey);
  const forcedSizesRef = useRef(forcedSizes);
  useEffect(() => {
    forcedSizesRef.current = forcedSizes;
  }, [forcedSizes]);
  useEffect(() => {
    const forced = appliedReset.current !== resetKey;
    appliedReset.current = resetKey;
    setSizes((current) => {
      if (!forced && current.length === columnCount) return current;
      // A forced re-read prefers the sizes the apply handed over directly —
      // the storage copy is best-effort and vanishes in private mode.
      const applied = forced ? forcedSizesRef.current : null;
      return applied && applied.length === columnCount
        ? applied
        : readStoredSizes(columnCount);
    });
  }, [columnCount, resetKey]);

  /** Remember `next` as this column count's ratios — on a drag end or a nudge,
   *  never per pointer-move. Private mode just does not survive a reload. */
  function persistSizes(next: number[]) {
    if (typeof window === "undefined") return;
    try {
      window.localStorage.setItem(
        SPLIT_SIZES_KEY,
        serializeStoredSizes(window.localStorage.getItem(SPLIT_SIZES_KEY), columnCount, next),
      );
    } catch {
      // Private mode: the drag just does not survive a reload.
    }
  }

  const drag = useRef<{
    index: number;
    startX: number;
    width: number;
    sizes: number[];
    /** The last ratios this drag committed, so the pointer-up can persist them. */
    latest: number[] | null;
  } | null>(null);

  function startDrag(index: number, event: PointerEvent<HTMLDivElement>) {
    const width = containerRef.current?.getBoundingClientRect().width;
    if (!width) return;
    event.preventDefault();
    drag.current = { index, startX: event.clientX, width, sizes: [...effectiveSizes], latest: null };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveDrag(event: PointerEvent<HTMLDivElement>) {
    const state = drag.current;
    if (!state) return;
    const deltaPercent = ((event.clientX - state.startX) / state.width) * 100;
    // Neither neighbour may collapse below a usable width; `applyDelta` refuses
    // the move that would rather than letting a pane vanish behind its divider.
    const next = applyDelta(state.sizes, state.index, deltaPercent, MIN_PERCENT);
    if (next === state.sizes) return;
    state.latest = next;
    setSizes(next);
  }

  function endDrag(event: PointerEvent<HTMLDivElement>) {
    const state = drag.current;
    if (!state) return;
    drag.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
    // Only if the split still has the shape the drag was over: a pane pruned
    // mid-drag changes columnCount, and persisting the stale ratios would
    // store one count's drag under another count's key.
    if (state.latest && state.latest.length === columnCount) {
      persistSizes(state.latest);
    }
  }

  /** Keyboard resize: a separator is focusable, so arrow keys nudge the split. */
  function nudge(index: number, delta: number) {
    const next = applyDelta(effectiveSizes, index, delta, MIN_PERCENT);
    if (next === effectiveSizes) return;
    setSizes(next);
    persistSizes(next);
  }

  // One pane the whole surface, on request: the pane's id, "primary" for pane
  // 0, or null for the ordinary split. Sizes are untouched — restoring brings
  // back exactly the split that was maximized away.
  const [maximized, setMaximized] = useState<string | null>(null);

  // Escape restores the split, scoped: the listener only exists while a pane
  // is maximized, so the key keeps its meaning everywhere else.
  useEffect(() => {
    if (!maximized) return;
    const onKey = (event: KeyboardEvent) => {
      // An Escape a nested surface already answered (the palette backing out,
      // a picker closing) is not also this restore's to consume.
      if (event.key !== "Escape" || event.defaultPrevented) return;
      event.preventDefault();
      setMaximized(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [maximized]);

  // Any change to the split's shape hands the surface back. This is what keeps
  // a maximized pane's own close honest — the pane unmounts, and without this
  // every survivor would stay display:none behind a maximize nobody holds —
  // and it makes a newly opened pane visible rather than born hidden.
  useEffect(() => {
    setMaximized(null);
  }, [columnCount]);

  // No extra panes — the primary alone, no wrapper that could alter its layout.
  if (resolved.length === 0 && filePanes.length === 0) return <>{primary}</>;

  /**
   * One separator, between column `index` and the next. Shared by both column
   * loops: the chat panes and the file panes resize by the same rules, and two
   * copies of this markup would be two places for the keyboard affordance and
   * the bounds to drift apart.
   */
  function renderDivider(index: number) {
    return (
      <div
        className="pane-divider"
        role="separator"
        aria-orientation="vertical"
        // Per-index, so a three-pane split's separators are distinct to a
        // screen reader rather than three controls answering to one name.
        aria-label={`Resize panes ${index + 1} and ${index + 2}`}
        // Window-splitter pattern: the value is the left pane's width percent,
        // bounded by the same MIN_PERCENT the drag refuses to cross.
        aria-valuemin={MIN_PERCENT}
        aria-valuemax={100 - MIN_PERCENT}
        aria-valuenow={Math.round(effectiveSizes[index])}
        tabIndex={0}
        onPointerDown={(event) => startDrag(index, event)}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
        onKeyDown={(event) => {
          if (event.key === "ArrowLeft") {
            event.preventDefault();
            nudge(index, -3);
          } else if (event.key === "ArrowRight") {
            event.preventDefault();
            nudge(index, 3);
          }
        }}
      />
    );
  }

  /** The wrapper class for one column: hidden while another pane is maximized. */
  function paneClass(id: string): string {
    return maximized && maximized !== id ? "chat-split-pane pane-hidden" : "chat-split-pane";
  }

  return (
    <div
      className={maximized ? "chat-split has-maximized" : "chat-split"}
      ref={containerRef}
    >
      <div
        className={paneClass("primary")}
        style={{ flexGrow: effectiveSizes[0], flexBasis: 0 }}
        onPointerDown={() => focusPane(null)}
        // A stable, focusable landing spot: closing the last extra pane moves
        // DOM focus here rather than letting it fall to <body>.
        tabIndex={-1}
      >
        {/* The primary is the shell's own ChatView and owns no pane head, so
            its maximize control is the split's: a corner button that only
            exists while there is a split to take the surface from. */}
        <button
          className="icon-button pane-max-corner"
          title={maximized === "primary" ? "Restore split" : "Maximize pane"}
          aria-label={maximized === "primary" ? "Restore split" : "Maximize primary pane"}
          onClick={() => setMaximized((current) => (current === "primary" ? null : "primary"))}
        >
          {maximized === "primary" ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
        </button>
        {primary}
      </div>
      {resolved.map(({ pane, conversation }, index) => (
        <Fragment key={pane.id}>
          {renderDivider(index)}
          <div
            className={paneClass(pane.id)}
            style={{ flexGrow: effectiveSizes[index + 1], flexBasis: 0 }}
          >
            <ChatPane
              conversation={conversation}
              bootstrap={bootstrap}
              sources={sources}
              apps={apps}
              openCitation={openCitation}
              onClose={() => closePane(pane.id)}
              focused={focusedPane === pane.id}
              onFocus={() => focusPane(pane.id)}
              onSettled={onSettled}
              onApprovalChanged={onApprovalChanged}
              pinning={pinning}
              maximized={maximized === pane.id}
              onToggleMaximize={() =>
                setMaximized((current) => (current === pane.id ? null : pane.id))
              }
            />
          </div>
        </Fragment>
      ))}
      {filePanes.map((pane, offset) => {
        // The file columns sit to the right of every chat column, so their
        // divider index continues the same sequence the chat loop left off at.
        const index = resolved.length + offset;
        return (
          <Fragment key={pane.id}>
            {renderDivider(index)}
            <div
              className={paneClass(pane.id)}
              style={{ flexGrow: effectiveSizes[index + 1], flexBasis: 0 }}
            >
              <AttachmentPane
                documentId={pane.documentId}
                filename={pane.filename}
                onClose={() => closeFilePane?.(pane.id)}
              />
            </div>
          </Fragment>
        );
      })}
    </div>
  );
}
