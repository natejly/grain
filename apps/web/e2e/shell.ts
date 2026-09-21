import { expect, type Locator, type Page } from "@playwright/test";

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

/**
 * Put a prompt in the composer and make sure it is really in the composer.
 *
 * A bare `fill()` is not enough right after `newThread`, and that is a product
 * race rather than a test one: the draft store in `use-workspace` is keyed by
 * the active conversation through a ref that is assigned during render
 * (`draftKeyRef.current = draftKey`). An onChange that lands between the switch
 * to the new thread and the re-render that re-keys it files the text under the
 * PREVIOUS key, so the controlled textarea re-renders empty and the words are
 * gone. Enter then sends nothing, and the failure reads as "no messages" rather
 * than as a lost draft — which is exactly how it presented in a full-suite run.
 *
 * Retrying the typing is what a person does, and it is honest here: the
 * assertion below is that the composer HOLDS the prompt, so a spec can never go
 * on to press Enter against an empty draft.
 */
export async function typePrompt(page: Page, text: string) {
  const composer = page.getByRole("textbox", { name: "Message" });
  await expect(async () => {
    await composer.fill(text);
    await expect(composer).toHaveValue(text, { timeout: 1_000 });
  }).toPass({ timeout: 15_000 });
}

/**
 * Type a prompt and send it, retrying until the turn is actually in the
 * transcript.
 *
 * The same race as `typePrompt`, one step further on: the draft can be re-keyed
 * between the moment the composer is seen holding the text and the Enter that
 * follows, and Enter against an empty draft is a no-op. The failure then reads
 * as a transcript that never grew, which is how it presented under a full-suite
 * run — a spec waiting 45s for a message nobody ever sent.
 *
 * It cannot double-send: the landed user message is checked BEFORE anything is
 * typed, so a retry whose first Enter did work returns without touching the
 * composer.
 */
export async function sendPrompt(page: Page, text: string) {
  const composer = page.getByRole("textbox", { name: "Message" });
  const sent = page.locator(".message.user").filter({ hasText: text });
  await expect(async () => {
    if (await sent.count()) return;
    await composer.fill(text);
    await expect(composer).toHaveValue(text, { timeout: 1_000 });
    await composer.press("Enter");
    await expect(sent).toHaveCount(1, { timeout: 5_000 });
  }).toPass({ timeout: 30_000 });
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

/**
 * Open one thread row's action menu, so Rename/Share/Delete can be clicked.
 *
 * The rail row used to carry its actions as icons revealed on hover, and specs
 * hovered the row to reach them. Six of them left the title 35px wide, so they
 * live in a disclosure now: one always-visible trigger, and the actions inside
 * it. That means a spec must OPEN the menu rather than hover the row — the
 * same thing a person now has to do, which is the point of routing it through
 * a helper instead of leaving five specs to each remember.
 */
export async function openThreadActions(row: Locator) {
  await row.getByRole("button", { name: /^Actions for / }).click();
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
