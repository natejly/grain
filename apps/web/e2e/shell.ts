import { expect, type Page } from "@playwright/test";

/**
 * Reaching a view, the way a user does.
 *
 * The shell has three surfaces and they are addressed by accessible name, not
 * by class: the left rail holds the places you work (Inbox included — the
 * approval queue is work, not configuration), the workspace-settings menu in
 * the top right holds the places you configure, and a group's siblings sit in
 * a tab strip above the view. Names are used because labels like "Create" and
 * "Documents" also appear on buttons *inside* the views — an unscoped
 * getByRole would match those too.
 *
 * One copy of these helpers rather than one per spec: four near-identical
 * `openView` definitions is four places to update the next time navigation
 * moves, which is exactly what happened this time.
 */

export const rail = (page: Page) => page.getByRole("navigation", { name: "Workspace" });

/**
 * Open a fresh thread and wait until it is really the active one.
 *
 * "New thread" creates the conversation over the network; a fill+Enter typed
 * before the switch lands puts the prompt in the PREVIOUS thread. That race is
 * how the agent-write specs kept failing under load with two specs' tool cards
 * in one transcript. The empty-transcript marker renders only when the active
 * conversation has no messages — but when NO thread was active before the
 * click (a fresh page load), it is already up, so it cannot be the switch
 * signal on its own. The rail row is: the "New conversation" entry
 * appears exactly when the created thread lands and becomes active, so waiting
 * for one more of them is what actually observes the switch.
 */
export async function newThread(page: Page) {
  // "New thread" lives in Chat's contextual sidebar now — a spec standing on
  // Boards or Sources has to walk through the Chat door first, same as a user.
  await openView(page, "Chat");
  await page.getByRole("button", { name: "New thread" }).click();
  // No counting: every count-based signal tried here raced the rail's first
  // load, because the "No conversations." placeholder also shows while the
  // list is still fetching, so a baseline read then is wrong by however many
  // threads earlier specs left. The switch itself is what must be observed,
  // and it has a conjunction all its own: the ACTIVE rail row is the fresh
  // untitled thread, and the transcript is empty. The one thread that
  // satisfies both is an empty "New conversation" that is currently active —
  // which is the state this helper exists to reach.
  await expect(page.locator(".thread.active")).toContainText("New conversation");
  await expect(page.locator(".message-scroll.empty")).toBeVisible();
}

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

/** Open a destination behind the workspace-settings menu — Connections, Admin. */
export async function openSettings(page: Page, group: string, tab?: RegExp | string) {
  await page.getByRole("button", { name: "Workspace settings" }).click();
  await page
    .getByRole("group", { name: "Workspace settings" })
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
