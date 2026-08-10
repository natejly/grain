"use client";

import { useSyncExternalStore } from "react";
import { THEME_CHANGE_EVENT, THEME_STORAGE_KEY } from "./theme-script";

export type Theme = "light" | "dark";

/**
 * The resolved theme, as an external store rather than a context.
 *
 * Two consumers need the *resolved* value in JavaScript rather than in CSS:
 * `Graph3D`, which hands literal colours to three.js, and `SandboxFrame`,
 * which forwards the theme across postMessage to generated app code. Both sit
 * deep in the tree, and a store means neither has to be wrapped in a provider
 * — and neither re-renders when the theme has not actually changed.
 *
 * The source of truth is the DOM: `data-theme` when the user has chosen, the
 * OS preference otherwise. That is exactly what the CSS cascade reads, so JS
 * and CSS can never disagree.
 */

const DARK_QUERY = "(prefers-color-scheme: dark)";

export function resolveTheme(): Theme {
  if (typeof document === "undefined") return "light";
  const explicit = document.documentElement.getAttribute("data-theme");
  if (explicit === "light" || explicit === "dark") return explicit;
  return window.matchMedia(DARK_QUERY).matches ? "dark" : "light";
}

function subscribe(onChange: () => void): () => void {
  const media = window.matchMedia(DARK_QUERY);
  media.addEventListener("change", onChange);
  window.addEventListener(THEME_CHANGE_EVENT, onChange);
  // Another tab wrote the preference.
  window.addEventListener("storage", onChange);
  return () => {
    media.removeEventListener("change", onChange);
    window.removeEventListener(THEME_CHANGE_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

/**
 * Persist an explicit choice and apply it. Storing the resolved theme (rather
 * than a tri-state) keeps the toggle honest: whatever the user is looking at
 * is what a reload gives them back, on any machine, whatever the OS does next.
 */
export function setTheme(theme: Theme): void {
  document.documentElement.setAttribute("data-theme", theme);
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    // Private mode or a blocked origin: the choice still applies to this page.
  }
  window.dispatchEvent(new Event(THEME_CHANGE_EVENT));
}

export function useTheme(): Theme {
  // The server snapshot is "light" because bare :root is light; the first
  // client render corrects it, and no styling depends on this value.
  return useSyncExternalStore(subscribe, resolveTheme, () => "light" as Theme);
}
