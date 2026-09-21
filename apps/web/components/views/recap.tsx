"use client";

import type { MeRecap, RecapGroup } from "@workspace/api-client";
import { Activity, Bot, Brain, Layers, MessageSquare, Zap } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../api";
import { describeError } from "./shared";

/**
 * The member's month, deterministically counted — GET /api/me/recap rendered
 * as three stat tiles and two ranked lists. Deliberately text and numbers
 * only: no LLM narration, no polling, nothing that could say more than the
 * five aggregates honestly know. Self-contained like SkillsView — nobody
 * needs these counts until they open this page.
 */
export function RecapView({ setError }: { setError: (message: string) => void }) {
  const [recap, setRecap] = useState<MeRecap | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let live = true;
    void api
      .getRecap()
      .then((payload) => {
        if (!live) return;
        setRecap(payload);
        setLoaded(true);
      })
      .catch((caught) => {
        if (!live) return;
        setLoaded(true);
        setError(describeError(caught, "Could not load your recap"));
      });
    return () => {
      live = false;
    };
  }, [setError]);

  const since = recap
    ? new Date(recap.since).toLocaleDateString(undefined, {
        month: "long",
        day: "numeric",
      })
    : "";
  const empty =
    recap !== null &&
    recap.threads_started === 0 &&
    recap.runs_started === 0 &&
    recap.memories_learned === 0;

  return (
    <section className="content-page recap-page">
      <div className="page-heading">
        <div>
          <h1>Recap</h1>
          <p>
            Your month so far{since ? ` — since ${since}` : ""}, counted
            exactly. Only your own activity in this workspace.
          </p>
        </div>
      </div>

      {!loaded && <p className="recap-empty">Counting…</p>}

      {loaded && recap && (
        <>
          <div className="recap-tiles">
            <div className="recap-tile">
              <MessageSquare size={15} aria-hidden="true" />
              <strong>{recap.threads_started}</strong>
              <span>
                thread{recap.threads_started === 1 ? "" : "s"} started
              </span>
            </div>
            <div className="recap-tile">
              <Zap size={15} aria-hidden="true" />
              <strong>{recap.runs_started}</strong>
              <span>run{recap.runs_started === 1 ? "" : "s"}</span>
            </div>
            <div className="recap-tile">
              <Brain size={15} aria-hidden="true" />
              <strong>{recap.memories_learned}</strong>
              <span>
                memor{recap.memories_learned === 1 ? "y" : "ies"} learned
              </span>
            </div>
          </div>

          {empty ? (
            <p className="recap-empty">Nothing yet this month.</p>
          ) : (
            <div className="recap-lists">
              <RecapList
                title="Top spaces"
                icon={<Layers size={14} aria-hidden="true" />}
                groups={recap.top_spaces}
                unit="thread"
              />
              <RecapList
                title="Top agents"
                icon={<Bot size={14} aria-hidden="true" />}
                groups={recap.top_agents}
                unit="run"
              />
            </div>
          )}
        </>
      )}

      {loaded && !recap && (
        <p className="recap-empty">
          <Activity size={14} aria-hidden="true" /> The recap could not be
          loaded.
        </p>
      )}
    </section>
  );
}

function RecapList({
  title,
  icon,
  groups,
  unit,
}: {
  title: string;
  icon: React.ReactNode;
  groups: RecapGroup[];
  unit: string;
}) {
  return (
    <div className="recap-list">
      <div className="panel-title">
        <div>
          {icon}
          <strong>{title}</strong>
        </div>
      </div>
      {groups.length === 0 ? (
        <p className="recap-empty">None this month.</p>
      ) : (
        <ol>
          {groups.map((group) => (
            <li key={group.id}>
              {/* A deleted space or agent keeps its id and loses its name;
                  showing the id is the honest rendering of "this happened,
                  in something that is gone". */}
              <span className="recap-list-name">{group.name || group.id}</span>
              <span className="recap-list-count">
                {group.count} {unit}
                {group.count === 1 ? "" : "s"}
              </span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
