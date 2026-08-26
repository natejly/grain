"use client";

import type { CoworkingPresence, CoworkingRun } from "@workspace/api-client";
import { Bot, Pencil } from "lucide-react";
import type { CoworkingState } from "./use-coworking";

/**
 * The topbar's who-is-here strip: one chip per agent run in flight and one
 * per *other* person with a surface open, so working alongside your agents
 * feels like working alongside anyone — you can see them moving.
 *
 * Deliberately glanceable and deliberately quiet: chips carry a short label
 * and a pulse, details ride the tooltip, and an empty workspace renders
 * nothing at all rather than an empty frame.
 */

/** Where a presence is, in words a tooltip can use. */
export function surfacePhrase(surface: string): string {
  const [kind] = surface.split(":", 1);
  switch (kind) {
    case "document":
      return "in a document";
    case "conversation":
      return "in a chat";
    case "board":
      return "on a board";
    default:
      return "here";
  }
}

/**
 * One row per actor, keeping their most recent surface — a person with a
 * document AND a chat open is one presence, not a crowd.
 */
export function dedupeByActor(presences: CoworkingPresence[]): CoworkingPresence[] {
  const byActor = new Map<string, CoworkingPresence>();
  for (const presence of presences) {
    const held = byActor.get(presence.actor_id);
    if (!held || presence.updated_at > held.updated_at) {
      byActor.set(presence.actor_id, presence);
    }
  }
  return [...byActor.values()];
}

function RunChip({ run }: { run: CoworkingRun }) {
  const working = run.status === "running";
  return (
    <span
      className={working ? "cowork-chip agent working" : "cowork-chip agent"}
      title={`${run.agent_label} (${run.status}): ${run.intent}`}
    >
      <Bot size={13} aria-hidden />
      <span className="cowork-chip-label">{run.agent_label}</span>
      {working && <span className="cowork-pulse" aria-hidden />}
    </span>
  );
}

function PersonChip({ presence }: { presence: CoworkingPresence }) {
  const typing = Boolean(presence.state.typing);
  return (
    <span
      className="cowork-chip person"
      title={`${presence.actor_label} is ${surfacePhrase(presence.surface)}`}
    >
      <span className="cowork-dot" aria-hidden />
      <span className="cowork-chip-label">{presence.actor_label}</span>
      {typing && <Pencil size={12} aria-hidden className="cowork-typing" />}
    </span>
  );
}

export function CoworkingStrip({
  coworking,
  selfId,
}: {
  coworking: CoworkingState;
  selfId: string;
}) {
  const people = dedupeByActor(
    coworking.presences.filter((presence) => presence.actor_id !== selfId),
  );
  if (coworking.runs.length === 0 && people.length === 0) return null;
  return (
    <div className="cowork-strip" role="status" aria-label="Who is working here now">
      {coworking.runs.map((run) => (
        <RunChip key={run.run_id} run={run} />
      ))}
      {people.map((presence) => (
        <PersonChip key={presence.actor_id} presence={presence} />
      ))}
    </div>
  );
}
