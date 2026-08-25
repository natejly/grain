"use client";

import { ArrowRight, LayoutDashboard, Pin } from "lucide-react";
import type { AgentToolCall, Dashboard } from "@workspace/api-client";
import { useState } from "react";
import {
  dashboardForCall,
  hasChartImage,
  makeDashboardSeed,
} from "./dashboard-pin-format";

/**
 * What the pin bar needs from the shell. Grouped and optional for the same
 * reason as ChatView's `approval` and `todos`: the panels beside a document or
 * dashboard mount ChatView without it, and where it is absent no bar renders.
 */
export type DashboardPinning = {
  dashboards: Dashboard[];
  pinnedIds: Set<string>;
  pin: (dashboardId: string) => Promise<void>;
  open: (dashboardId: string) => void;
  askForChart: (seed: string) => void;
};

/**
 * The finish-the-job row under a chart-shaped tool card.
 *
 * A succeeded dashboard tool call ends with a bar that completes the pin in
 * place — the server's own success line says "pin it from the dashboards
 * screen", and this is the button that makes the sentence unnecessary. Once
 * pinned (here or anywhere else — `pinnedIds` is the shell's one source of
 * truth) the bar says so quietly and offers the jump instead.
 *
 * A sandbox chart image is a different animal: pixels with no dataset or
 * query behind them, so there is nothing to pin. The honest offer is the
 * shipped cross-link idiom — hand the composer the sentence that asks the
 * agent for the pinnable version.
 */
export function DashboardPinBar({
  call,
  pinning,
}: {
  call: AgentToolCall;
  pinning?: DashboardPinning;
}) {
  const [busy, setBusy] = useState(false);
  if (!pinning) return null;

  const dashboard = dashboardForCall(call, pinning.dashboards);
  if (dashboard) {
    const pinned = pinning.pinnedIds.has(dashboard.id);
    return (
      <div className="artifact-pin-bar">
        <LayoutDashboard size={14} aria-hidden="true" />
        <span className="pin-bar-name">{dashboard.name}</span>
        {/* Always mounted, empty until pinned: an announcement only lands if
            the live region existed before the text arrived. */}
        <span className="pin-bar-done" role="status">
          {pinned ? "Pinned" : ""}
        </span>
        {/* One button, swapped in place: the element pinning flips into
            "Show me" is the element the keyboard user is focused on, so
            focus survives the flip. aria-disabled rather than disabled while
            the pin is in flight, for the same reason. */}
        <button
          type="button"
          className={pinned ? "ghost-button" : "primary-button"}
          aria-disabled={busy}
          aria-label={
            pinned
              ? `Show “${dashboard.name}”`
              : `Pin “${dashboard.name}” to your home screen`
          }
          onClick={() => {
            if (busy) return;
            if (pinned) {
              pinning.open(dashboard.id);
              return;
            }
            setBusy(true);
            // The handler is optimistic and reports its own failure through
            // the shell's error surface; `pinnedIds` flipping is what turns
            // this button into the Show-me state.
            void pinning.pin(dashboard.id).finally(() => setBusy(false));
          }}
        >
          {pinned ? (
            <>
              Show me <ArrowRight size={13} aria-hidden="true" />
            </>
          ) : (
            <>
              <Pin size={13} aria-hidden="true" /> Pin to home screen
            </>
          )}
        </button>
      </div>
    );
  }

  if (hasChartImage(call)) {
    return (
      <div className="artifact-pin-bar">
        <button
          type="button"
          className="ghost-button"
          onClick={() => pinning.askForChart(makeDashboardSeed(call.name))}
        >
          <LayoutDashboard size={13} aria-hidden="true" /> Make this a dashboard
        </button>
      </div>
    );
  }

  return null;
}
