"use client";

import type { Bootstrap, Citation, Conversation, GeneratedApp, Source } from "@workspace/api-client";
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

export type ChatPaneRef = { id: string; conversationId: string };

export type ChatSplitProps = {
  /**
   * The shell's own chat, rendered byte-for-byte as today. It is passed in
   * rather than reconstructed here so the single-pane user's ChatView keeps the
   * shell's rich `onRunSettled` and rail wiring — the no-regression guarantee.
   */
  primary: ReactNode;
  /** The EXTRA panes beside the primary; empty === today's single-pane shell. */
  panes: ChatPaneRef[];
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
};

const MIN_PERCENT = 15;

/** Equal shares for `count` columns, as flex-grow ratios summing to 100. */
function equalSizes(count: number): number[] {
  return Array.from({ length: count }, () => 100 / count);
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

  const columnCount = 1 + resolved.length;
  const containerRef = useRef<HTMLDivElement>(null);
  const [sizes, setSizes] = useState<number[]>(() => equalSizes(columnCount));
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
  useEffect(() => {
    setSizes((current) => (current.length === columnCount ? current : equalSizes(columnCount)));
  }, [columnCount]);

  const drag = useRef<{ index: number; startX: number; width: number; sizes: number[] } | null>(
    null,
  );

  function startDrag(index: number, event: PointerEvent<HTMLDivElement>) {
    const width = containerRef.current?.getBoundingClientRect().width;
    if (!width) return;
    event.preventDefault();
    drag.current = { index, startX: event.clientX, width, sizes: [...effectiveSizes] };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function moveDrag(event: PointerEvent<HTMLDivElement>) {
    const state = drag.current;
    if (!state) return;
    const deltaPercent = ((event.clientX - state.startX) / state.width) * 100;
    const left = state.sizes[state.index] + deltaPercent;
    const right = state.sizes[state.index + 1] - deltaPercent;
    // Neither neighbour may collapse below a usable width; refuse the move that
    // would rather than letting a pane vanish behind its own divider.
    if (left < MIN_PERCENT || right < MIN_PERCENT) return;
    const next = [...state.sizes];
    next[state.index] = left;
    next[state.index + 1] = right;
    setSizes(next);
  }

  function endDrag(event: PointerEvent<HTMLDivElement>) {
    if (!drag.current) return;
    drag.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
  }

  /** Keyboard resize: a separator is focusable, so arrow keys nudge the split. */
  function nudge(index: number, delta: number) {
    setSizes((current) => {
      const left = current[index] + delta;
      const right = current[index + 1] - delta;
      if (left < MIN_PERCENT || right < MIN_PERCENT) return current;
      const next = [...current];
      next[index] = left;
      next[index + 1] = right;
      return next;
    });
  }

  // No extra panes — the primary alone, no wrapper that could alter its layout.
  if (resolved.length === 0) return <>{primary}</>;

  return (
    <div className="chat-split" ref={containerRef}>
      <div
        className="chat-split-pane"
        style={{ flexGrow: effectiveSizes[0], flexBasis: 0 }}
        onPointerDown={() => focusPane(null)}
        // A stable, focusable landing spot: closing the last extra pane moves
        // DOM focus here rather than letting it fall to <body>.
        tabIndex={-1}
      >
        {primary}
      </div>
      {resolved.map(({ pane, conversation }, index) => (
        <Fragment key={pane.id}>
          <div
            className="pane-divider"
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize panes"
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
          <div
            className="chat-split-pane"
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
            />
          </div>
        </Fragment>
      ))}
    </div>
  );
}
