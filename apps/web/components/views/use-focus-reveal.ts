"use client";

import { useEffect, useRef } from "react";

/**
 * The landing half of a cross-link: the arriving page scrolls to the row the
 * link named and rings it briefly. Same recipe views/dashboards.tsx uses for a
 * rail-focused tile, extracted because the three Knowledge views all consume
 * it — the caller styles `focused === id` and gives the row `${prefix}-${id}`.
 *
 * scrollIntoView runs after paint and does nothing if the row is not there
 * yet; the outline alone is enough to find it a render later. The self-clear
 * is what makes the ring a "here it is", not a selection — the next visit to
 * the page must not open with a row mysteriously ringed.
 */
export function useFocusReveal(
  prefix: string,
  focused: string | null,
  setFocused: (id: string | null) => void,
) {
  const revealed = useRef<string | null>(null);
  useEffect(() => {
    if (!focused || revealed.current === focused) return;
    revealed.current = focused;
    document
      .getElementById(`${prefix}-${focused}`)
      ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    const timer = window.setTimeout(() => setFocused(null), 2500);
    return () => window.clearTimeout(timer);
  }, [prefix, focused, setFocused]);
}
