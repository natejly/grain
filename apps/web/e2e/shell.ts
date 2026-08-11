import type { Page } from "@playwright/test";

/**
 * Reaching a view, the way a user does.
 *
 * The shell has three surfaces and they are addressed by accessible name, not
 * by class: the left rail holds the places you work, the Settings menu in the
 * top right holds the places you configure, and a group's siblings sit in a tab
 * strip above the view. Names are used because labels like "Create" and "Files"
 * also appear on buttons *inside* the views — an unscoped getByRole would match
 * those too.
 *
 * One copy of these helpers rather than one per spec: four near-identical
 * `openView` definitions is four places to update the next time navigation
 * moves, which is exactly what happened this time.
 */

export const rail = (page: Page) => page.getByRole("navigation", { name: "Workspace" });

export const tabs = (page: Page, group: string) =>
  page.getByRole("navigation", { name: `${group} views` });

/** Open a rail destination — Chat, Files, Knowledge — and optionally a tab. */
export async function openView(page: Page, group: string, tab?: RegExp | string) {
  // A group's badge is part of its accessible name ("Files 4"), so anchor
  // on the label rather than asking for an exact match.
  await rail(page)
    .getByRole("button", { name: new RegExp(`^${group}`) })
    .click();
  if (!tab) return;
  await tabs(page, group).getByRole("button", { name: tab }).click();
}

/** Open a destination behind the Settings menu — Connections, Activity, Admin. */
export async function openSettings(page: Page, group: string, tab?: RegExp | string) {
  await page.getByRole("button", { name: /^Settings/ }).click();
  await page
    .getByRole("group", { name: "Settings" })
    .getByRole("button", { name: new RegExp(`^${group}`) })
    .click();
  if (!tab) return;
  await tabs(page, group).getByRole("button", { name: tab }).click();
}

/**
 * Make something from the Create menu. `name` is required for the things that
 * cannot be renamed afterwards (document, project, board) and omitted for the
 * two that ask for nothing first (dashboard, workflow).
 */
export async function createFromMenu(page: Page, thing: string, name?: string) {
  await page.getByRole("button", { name: "Create new" }).click();
  const menu = page.getByRole("group", { name: "Create" });
  await menu.getByRole("button", { name: thing, exact: true }).click();
  if (name === undefined) return;
  await menu.getByRole("textbox").fill(name);
  await menu.getByRole("button", { name: new RegExp(`^Create ${thing}`, "i") }).click();
}
