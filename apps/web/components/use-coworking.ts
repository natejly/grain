"use client";

import type { CoworkingPresence, CoworkingRun } from "@workspace/api-client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";

/**
 * The shell's live-coworking state: one SSE connection per workspace, however
 * many surfaces draw from it.
 *
 * Three things come OUT — runs in flight, everyone's presence (cursors,
 * typing, live drafts), and a callback ping when a durable event (a claim, a
 * tick) says some list state moved. One thing goes IN: `report`, the throttled
 * heartbeat a surface calls as its user types and moves. The hook re-beats
 * occupied surfaces on an interval below the server's TTL, so "reading
 * quietly" stays visibly present without the views having to think about
 * timers at all.
 */

/** Trailing-edge throttle for heartbeats; a keystroke burst sends one write. */
const REPORT_THROTTLE_MS = 350;
/** Re-beat occupied surfaces this often — under the server's 15s TTL. */
const REBEAT_MS = 8_000;
/** How long to wait before redialing a dropped stream. */
const REDIAL_MS = 2_000;

export type CoworkingState = {
  /** Runs in flight across the workspace, per the latest snapshot frame. */
  runs: CoworkingRun[];
  /** Everyone's live presence, self included — filter with `othersOn`. */
  presences: CoworkingPresence[];
  /** Presences on one surface that are not this user's own. */
  othersOn: (surface: string) => CoworkingPresence[];
  /** Say where this user is and what they're doing. Throttled; fire-and-forget. */
  report: (surface: string, state: CoworkingPresence["state"]) => void;
  /** The explicit goodbye a surface sends on unmount, so chips clear fast. */
  leave: (surface: string) => void;
};

type PendingBeat = {
  state: CoworkingPresence["state"];
  timer: number | null;
  lastSent: number;
};

export function useCoworking(
  selfId: string,
  onEvent?: (eventType: string) => void,
): CoworkingState {
  const [runs, setRuns] = useState<CoworkingRun[]>([]);
  const [presences, setPresences] = useState<CoworkingPresence[]>([]);
  // The callback rides a ref so a new identity per render does not tear down
  // the stream — the connection's lifetime belongs to the session, not to
  // whichever closure the caller built this render.
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;
  const pending = useRef<Map<string, PendingBeat>>(new Map());

  useEffect(() => {
    if (!selfId) return;
    const controller = new AbortController();
    let alive = true;
    void (async () => {
      while (alive) {
        try {
          const snapshot = await api.coworkingActivity();
          if (!alive) return;
          setRuns(snapshot.runs);
          setPresences(snapshot.presences);
          for await (const frame of api.streamCoworking(
            snapshot.last_event_sequence,
            controller.signal,
          )) {
            if (!alive) return;
            if (frame.event === "runs") {
              setRuns(frame.data as CoworkingRun[]);
            } else if (frame.event === "presence") {
              setPresences(frame.data as CoworkingPresence[]);
            } else {
              onEventRef.current?.(frame.event);
            }
          }
        } catch {
          // Fall through to the redial pause below.
        }
        if (!alive) return;
        await new Promise((resolve) => setTimeout(resolve, REDIAL_MS));
      }
    })();
    return () => {
      alive = false;
      controller.abort();
    };
  }, [selfId]);

  const send = useCallback((surface: string, state: CoworkingPresence["state"]) => {
    void api.heartbeatPresence(surface, state).catch(() => undefined);
  }, []);

  const report = useCallback(
    (surface: string, state: CoworkingPresence["state"]) => {
      const now = Date.now();
      const entry = pending.current.get(surface) ?? {
        state,
        timer: null,
        lastSent: 0,
      };
      entry.state = state;
      pending.current.set(surface, entry);
      if (now - entry.lastSent >= REPORT_THROTTLE_MS) {
        entry.lastSent = now;
        send(surface, state);
      } else if (entry.timer === null) {
        // Trailing edge: the last position in a burst must land, or a remote
        // cursor freezes one keystroke behind until the next pause.
        entry.timer = window.setTimeout(() => {
          entry.timer = null;
          entry.lastSent = Date.now();
          send(surface, entry.state);
        }, REPORT_THROTTLE_MS);
      }
    },
    [send],
  );

  const leave = useCallback((surface: string) => {
    const entry = pending.current.get(surface);
    if (entry?.timer !== null && entry !== undefined) {
      window.clearTimeout(entry.timer);
    }
    pending.current.delete(surface);
    void api.leavePresence(surface).catch(() => undefined);
  }, []);

  // Occupied surfaces re-beat on an interval below the TTL, so presence means
  // "the surface is open", not "the user moved recently".
  useEffect(() => {
    if (!selfId) return;
    const timer = window.setInterval(() => {
      for (const [surface, entry] of pending.current) {
        if (Date.now() - entry.lastSent >= REBEAT_MS) {
          entry.lastSent = Date.now();
          send(surface, entry.state);
        }
      }
    }, REBEAT_MS);
    return () => window.clearInterval(timer);
  }, [selfId, send]);

  const othersOn = useCallback(
    (surface: string) =>
      presences.filter(
        (presence) => presence.surface === surface && presence.actor_id !== selfId,
      ),
    [presences, selfId],
  );

  return useMemo(
    () => ({ runs, presences, othersOn, report, leave }),
    [runs, presences, othersOn, report, leave],
  );
}
