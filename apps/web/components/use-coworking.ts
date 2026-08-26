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
/**
 * Pointers move far more often than text changes and are far cheaper to send
 * (two numbers, no draft), so they get their own faster throttle. The
 * receiving end interpolates between beats with a CSS transition, which is
 * what makes ~11/second read as a smooth glide rather than a hop — matching
 * the rate to the frame rate would be an order of magnitude more writes for
 * motion the transition already draws.
 */
const POINTER_THROTTLE_MS = 90;
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
  /**
   * Say where this user's mouse is on a surface, in fractions of its box, or
   * `null` on the way out (the mouse left, the tab blurred).
   *
   * A channel of its own rather than a key callers fold into `report`,
   * because the two have opposite update rules. `report` REPLACES a surface's
   * state — that is how a save retires a live draft — so a pointer passed
   * through it would be erased by the next editing beat, and a draft would be
   * erased by the next mouse move. The pointer is held here instead and
   * merged in at send time, so each side can forget the other exists.
   */
  reportPointer: (surface: string, pointer: { x: number; y: number } | null) => void;
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
  //: The pointer channel, kept beside the per-surface beat rather than inside
  //: it. See `reportPointer` on `CoworkingState` for why the two are separate.
  const pointers = useRef<Map<string, { x: number; y: number }>>(new Map());

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
    // The pointer is merged here rather than stored in `state`, so the two
    // channels cannot overwrite each other — see `reportPointer`.
    const pointer = pointers.current.get(surface);
    const payload = pointer ? { ...state, pointer } : state;
    void api.heartbeatPresence(surface, payload).catch(() => undefined);
  }, []);

  /**
   * The shared leading-edge-plus-trailing-edge throttle both channels ride.
   * `throttleMs` differs between them, but the guarantee does not: the last
   * value in a burst always lands, or a remote cursor freezes mid-motion
   * until its owner happens to move again.
   */
  const queue = useCallback(
    (surface: string, state: CoworkingPresence["state"], throttleMs: number) => {
      const now = Date.now();
      const entry = pending.current.get(surface) ?? {
        state,
        timer: null,
        lastSent: 0,
      };
      entry.state = state;
      pending.current.set(surface, entry);
      if (now - entry.lastSent >= throttleMs) {
        entry.lastSent = now;
        send(surface, state);
      } else if (entry.timer === null) {
        entry.timer = window.setTimeout(() => {
          entry.timer = null;
          entry.lastSent = Date.now();
          send(surface, entry.state);
        }, throttleMs);
      }
    },
    [send],
  );

  const report = useCallback(
    (surface: string, state: CoworkingPresence["state"]) =>
      queue(surface, state, REPORT_THROTTLE_MS),
    [queue],
  );

  const reportPointer = useCallback(
    (surface: string, pointer: { x: number; y: number } | null) => {
      // Whatever the surface last said about itself, re-sent carrying (or no
      // longer carrying) the pointer. A surface whose view never calls
      // `report` — a board, a dashboard — still beats from here, which is
      // what makes a cursor visible on a surface nobody is typing on.
      const entry = pending.current.get(surface);
      const held = entry?.state ?? {};
      if (pointer) {
        pointers.current.set(surface, pointer);
        queue(surface, held, POINTER_THROTTLE_MS);
        return;
      }
      pointers.current.delete(surface);
      // The clear goes out NOW, and any beat still queued behind it is
      // dropped. Both halves matter on unmount: the surface's `leave` fires
      // immediately after this, and a throttled beat landing after that DELETE
      // would recreate the presence row — a stranger left standing on the
      // surface, cursor and all, until the TTL swept them fifteen seconds
      // later. Going out immediately also means a cursor disappears when the
      // mouse leaves rather than a tenth of a second afterwards.
      if (entry?.timer != null) {
        window.clearTimeout(entry.timer);
        entry.timer = null;
      }
      if (entry) entry.lastSent = Date.now();
      send(surface, held);
    },
    [queue, send],
  );

  const leave = useCallback((surface: string) => {
    const entry = pending.current.get(surface);
    if (entry?.timer !== null && entry !== undefined) {
      window.clearTimeout(entry.timer);
    }
    pending.current.delete(surface);
    pointers.current.delete(surface);
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
    () => ({ runs, presences, othersOn, report, reportPointer, leave }),
    [runs, presences, othersOn, report, reportPointer, leave],
  );
}
