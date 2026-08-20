"use client";

import type {
  Citation,
  Dashboard,
  DashboardPin,
  DashboardTemplate,
  Dataset,
  GeneratedApp,
  Source,
} from "@workspace/api-client";
import { Sparkles } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useSubjectThread } from "../use-subject-thread";
import { SubjectChatPanel } from "./subject-chat";
import { DashboardCatalog, DashboardTemplates } from "./dashboard-catalog";
import { DashboardGrid, type DashboardResultState } from "./dashboard-grid";
import type { Tile } from "./dashboard-format";

/**
 * One page, one kind of thing.
 *
 * At the top is *your* screen: the dashboards you pinned, arranged where you
 * put them. Nothing on it was made here — the agent writes dashboards during a
 * conversation and this page never offers to — which is the whole product
 * decision made visible. Underneath are templates waiting for data. The
 * generated apps that used to share this page have a tab of their own
 * (`apps.tsx`): they are programs, not charts, and the button here that said
 * "Add dashboard" was secretly opening their editor.
 */

export type DashboardsViewProps = {
  /**
   * Still needed with the gallery gone: a chat turn about a dashboard can name
   * a published app, and the side panel embeds it rather than only naming it.
   */
  apps: GeneratedApp[];

  dashboards: Dashboard[];
  templates: DashboardTemplate[];
  datasets: Dataset[];
  pins: DashboardPin[];
  pinnedIds: Set<string>;
  results: Record<string, DashboardResultState>;
  runDashboard: (dashboardId: string, force?: boolean) => void;
  pinDashboard: (dashboardId: string) => Promise<void>;
  unpinDashboard: (dashboardId: string) => Promise<void>;
  saveDashboardLayout: (tiles: Tile[]) => Promise<void>;
  bindDashboardTemplate: (
    template: DashboardTemplate,
    payload: {
      name: string;
      dataset_id: string;
      column_bindings: Record<string, string>;
    },
  ) => Promise<Dashboard>;
  removeDashboard: (dashboard: Dashboard) => Promise<void>;
  /** Which pinned tile to reveal, set by the rail. Cleared once revealed. */
  focused: string | null;
  setFocused: (dashboardId: string | null) => void;
  /**
   * The honest create path, as a button: dashboards are written by the agent,
   * so "new dashboard" means asking for a chart — this prefills the composer
   * and goes to Chat. Where the old "Add dashboard" button stood, which
   * secretly opened the sandbox-app editor.
   */
  askForChart?: () => void;
  /** What the side chat needs to be the same chat as the rail's. */
  chat?: DashboardChatDeps;
};

/**
 * Everything the panel beside a dashboard needs from the shell. Optional as a
 * bundle rather than field by field, so a caller either wires the panel or does
 * not have one — there is no half-wired state where the composer sends into
 * nothing.
 */
export type DashboardChatDeps = {
  agentId?: string;
  sources: Source[];
  openCitation: (citation: Citation) => Promise<void>;
  /** Re-read the dashboards and re-run the revised one after a turn settles. */
  reloadDashboards: () => Promise<void>;
  /** `DEV_UNRESTRICTED_AGENT` is on, so the panel wears the warning. */
  unrestricted?: boolean;
};

export function DashboardsView({
  apps,
  dashboards,
  templates,
  datasets,
  pins,
  pinnedIds,
  results,
  runDashboard,
  pinDashboard,
  unpinDashboard,
  saveDashboardLayout,
  bindDashboardTemplate,
  removeDashboard,
  focused,
  setFocused,
  askForChart,
  chat,
}: DashboardsViewProps) {
  const revealed = useRef<string | null>(null);
  // Which dashboard the panel is about. The page shows many at once, so the
  // subject is a *selection* here rather than "the open one" — a chat that
  // silently followed the most recently pinned tile would be about something
  // the reader never chose.
  const [chatting, setChatting] = useState<string | null>(null);
  const subject = dashboards.find((item) => item.id === chatting) ?? null;

  const onRunSettled = useCallback(async () => {
    await chat?.reloadDashboards().catch(() => undefined);
  }, [chat]);

  const thread = useSubjectThread({
    kind: "dashboard",
    subjectId: chat && subject ? subject.id : "",
    agentId: chat?.agentId,
    onRunSettled,
  });

  useEffect(() => {
    if (!focused || revealed.current === focused) return;
    revealed.current = focused;
    // Runs after paint and does nothing if the tile is not there yet — a
    // dashboard focused from the rail the moment it was pinned arrives one
    // render later, and the outline alone is enough to find it then.
    document
      .getElementById(`pinned-dashboard-${focused}`)
      ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    // The outline is a "here it is", not a selection: it clears itself so the
    // next visit to this page does not open with a tile mysteriously ringed.
    const timer = window.setTimeout(() => setFocused(null), 2500);
    return () => window.clearTimeout(timer);
  }, [focused, setFocused]);

  return (
    <div
      className={
        subject && chat ? "dashboards-layout with-chat" : "dashboards-layout"
      }
    >
    <section className="content-page dashboards-page">
      <div className="page-heading">
        <div>
          <h1>Dashboards</h1>
          <p>The agent writes dashboards — ask for a chart.</p>
        </div>
        <div className="page-heading-actions">
          {askForChart && (
            <button className="primary-button" onClick={askForChart}>
              <Sparkles size={14} />
              Ask the agent for a chart
            </button>
          )}
          <DashboardCatalog
            dashboards={dashboards}
            pinnedIds={pinnedIds}
            pin={pinDashboard}
            unpin={unpinDashboard}
            remove={removeDashboard}
            // Launching *is* pinning: there is no single-dashboard page to
            // send someone to, and a chart you opened once is a chart you
            // wanted on your screen. Already pinned, it is merely revealed.
            open={(dashboardId) => {
              if (!pinnedIds.has(dashboardId)) void pinDashboard(dashboardId);
              setFocused(dashboardId);
            }}
          />
        </div>
      </div>

      <DashboardGrid
        pins={pins}
        results={results}
        run={runDashboard}
        unpin={unpinDashboard}
        chat={chat ? setChatting : undefined}
        saveLayout={saveDashboardLayout}
        focused={focused}
      />

      <DashboardTemplates
        templates={templates}
        datasets={datasets}
        bind={bindDashboardTemplate}
      />
    </section>

    {subject && chat && (
      <SubjectChatPanel
        className="dashboard-chat"
        heading="Chat about this dashboard"
        label={`Chat about ${subject.name}`}
        close={() => setChatting(null)}
        thread={thread}
        sources={chat.sources}
        apps={apps}
        openCitation={chat.openCitation}
        unrestricted={chat.unrestricted}
      />
    )}
    </div>
  );
}
