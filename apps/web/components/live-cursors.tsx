"use client";

import type { CoworkingPresence } from "@workspace/api-client";
import { useCallback, useEffect, useMemo, useRef } from "react";
import type { CoworkingState } from "./use-coworking";

/**
 * Everyone else's mouse, drawn on top of a shared surface — the Google-Docs
 * gesture: you see the other person's cursor glide, with their name on it,
 * before they have typed anything at all.
 *
 * This is a *layer*, not a view: wrap whatever the surface already renders and
 * it gains cursors, because the thing being pointed at (a document, a board, a
 * chat) has no business knowing that presence exists. It rides the presence
 * channel `use-coworking` already holds open — no second socket, no second
 * poll — and reports through `reportPointer`, which is kept apart from the
 * editing heartbeat so a mouse move cannot erase a live draft.
 *
 * Positions are fractions of THIS element's box in both directions, so two
 * people at different window sizes point at the same *place* rather than the
 * same pixel offset. That is exact for a surface whose content scales with it
 * and approximate for one that reflows; approximate is the right trade, since
 * the alternative is a cursor that is precisely wrong on every screen but the
 * sender's.
 */

/**
 * How long a remote cursor animates toward its next beat. Matched to the
 * sender's pointer throttle (`POINTER_THROTTLE_MS`) so the glide finishes just
 * as the next position lands: shorter stutters, longer lags behind the truth.
 */
const GLIDE_MS = 90;

/**
 * A remote cursor with nothing new to say for this long is drawn as idle.
 * Shorter than the server's presence TTL on purpose — a person who stopped
 * moving is still here, and should fade rather than vanish.
 */
const IDLE_AFTER_MS = 4_000;

/**
 * A stable colour per actor, chosen the same way in every browser so a person
 * is the same colour to everyone looking at them — the property that makes
 * "the blue cursor" a thing two people can say to each other. A hash of the
 * id, not an index into the presence list: index colours would reshuffle
 * every time somebody joined or left.
 */
export function actorHue(actorId: string): number {
  let hash = 0;
  for (let i = 0; i < actorId.length; i += 1) {
    hash = (hash * 31 + actorId.charCodeAt(i)) | 0;
  }
  return Math.abs(hash) % 360;
}

/** The presences on this surface that carry a drawable pointer. */
export function pointing(presences: CoworkingPresence[]): CoworkingPresence[] {
  return presences.filter((presence) => {
    const pointer = presence.state.pointer;
    return (
      typeof pointer?.x === "number" &&
      typeof pointer?.y === "number" &&
      Number.isFinite(pointer.x) &&
      Number.isFinite(pointer.y)
    );
  });
}

function RemoteCursor({ presence }: { presence: CoworkingPresence }) {
  const pointer = presence.state.pointer as { x: number; y: number };
  const hue = actorHue(presence.actor_id);
  const idle = Date.now() - new Date(presence.updated_at).getTime() > IDLE_AFTER_MS;
  return (
    <div
      className={idle ? "live-cursor idle" : "live-cursor"}
      style={{
        left: `${pointer.x * 100}%`,
        top: `${pointer.y * 100}%`,
        // The colour is per-actor and therefore per-element; only the two
        // custom properties vary, so the shape itself stays in the stylesheet.
        ["--cursor-hue" as string]: String(hue),
        transitionDuration: `${GLIDE_MS}ms`,
      }}
      aria-hidden
    >
      {/* An arrow, not an emoji or an icon-font glyph: it has to sit exactly
          on the reported point, and only a path whose origin is its own tip
          does that at every zoom level. */}
      <svg viewBox="0 0 16 16" width="16" height="16" className="live-cursor-arrow">
        <path d="M1 1 L1 13 L4.5 9.8 L7 15 L9.4 13.9 L6.9 8.9 L11.5 8.7 Z" />
      </svg>
      <span className="live-cursor-label">{presence.actor_label}</span>
    </div>
  );
}

export function LiveCursorLayer({
  surface,
  coworking,
  className,
  children,
}: {
  /** "document:<id>", "board:<id>", … — the same address `report` uses. */
  surface: string;
  /**
   * Optional so a surface can render identically when coworking is off (a
   * solo workspace, a share link) without every call site branching.
   */
  coworking?: CoworkingState;
  className?: string;
  children: React.ReactNode;
}) {
  const box = useRef<HTMLDivElement | null>(null);
  // Extracted rather than used off `coworking`, which changes identity on
  // every presence frame: depending on the object would re-bind the listeners
  // eleven times a second.
  const reportPointer = coworking?.reportPointer;

  const move = useCallback(
    (event: React.PointerEvent<HTMLDivElement>) => {
      const element = box.current;
      if (!element || !surface || !reportPointer) return;
      const rect = element.getBoundingClientRect();
      // A zero-sized box (display:none, a pane mid-collapse) would divide to
      // Infinity; the server drops that, but not sending it is cheaper.
      if (rect.width <= 0 || rect.height <= 0) return;
      reportPointer(surface, {
        x: (event.clientX - rect.left) / rect.width,
        y: (event.clientY - rect.top) / rect.height,
      });
    },
    [surface, reportPointer],
  );

  const clear = useCallback(() => {
    if (!surface || !reportPointer) return;
    reportPointer(surface, null);
  }, [surface, reportPointer]);

  // A cursor must not outlive the surface, the tab's focus, or the pointer
  // itself. `pointerleave` covers the mouse leaving the box; this covers the
  // window losing focus with the mouse still inside it, and the unmount that
  // `pointerleave` never fires for.
  useEffect(() => {
    if (!surface || !reportPointer) return;
    window.addEventListener("blur", clear);
    return () => {
      window.removeEventListener("blur", clear);
      reportPointer(surface, null);
    };
  }, [surface, reportPointer, clear]);

  const others = coworking && surface ? coworking.othersOn(surface) : [];
  const cursors = useMemo(() => pointing(others), [others]);

  return (
    <div
      ref={box}
      className={className ? `live-cursor-layer ${className}` : "live-cursor-layer"}
      onPointerMove={move}
      onPointerLeave={clear}
    >
      {children}
      {cursors.map((presence) => (
        <RemoteCursor key={presence.actor_id} presence={presence} />
      ))}
    </div>
  );
}
