import { expect, test, type Page } from "@playwright/test";
import { newThread, openThreadActions, openView } from "./shell";

/**
 * The shell affordances this branch added: a "New chat" that is reachable from
 * every group, a "?" cheat sheet that knows when you are typing, the
 * composer's "+" index of its own verbs, and a URL that can hand a focused
 * thread to a reload or a teammate.
 *
 * Each one is a promise about *reachability* rather than about data — the kind
 * of regression a unit test cannot see because it only exists once the whole
 * shell is mounted and the real listeners are stacked in their real order.
 */

const composer = (page: Page) => page.getByRole("textbox", { name: "Message" });

/** The turn is over, not merely answered — same doctrine as chat-composer. */
async function settled(page: Page) {
  await expect(page.getByRole("button", { name: "Stop generating" })).toHaveCount(0);
}

/** Delete the active thread and observe the rail shrink by one. */
async function deleteActiveThread(page: Page) {
  const threads = page.locator(".thread");
  const before = await threads.count();
  page.once("dialog", (dialog) => dialog.accept());
  await openThreadActions(page.locator(".thread.active"));
  await page
    .locator(".thread.active")
    .getByRole("button", { name: /^Delete / })
    .click();
  await expect(threads).toHaveCount(before - 1);
}

test("the topbar's New chat works from the Library group", async ({ page }) => {
  await page.goto("/");
  await openView(page, "Library");
  await expect(page.locator(".documents-layout")).toBeVisible();

  // The sidebar's "New thread" is chat-group-only; this one is the promise
  // that starting a conversation never requires walking back to Chat first.
  const newChat = page.getByRole("button", { name: "New chat" });
  await expect(newChat).toBeVisible();
  await newChat.click();

  // It landed: the chat view is up, the fresh thread is the active one, and
  // its transcript is empty — the same conjunction newThread() waits on.
  await expect(page.locator(".chat-layout")).toBeVisible();
  await expect(page.locator(".thread.active")).toContainText("New conversation");
  await expect(page.locator(".message-scroll.empty")).toBeVisible();

  // Put the shared rail back the way this test found it.
  await deleteActiveThread(page);
});

test("? opens the cheat sheet, Escape closes it, and typing never triggers it", async ({
  page,
}) => {
  await page.goto("/");
  // A view with no focused textbox, so the keypress lands on the shell.
  await openView(page, "Library");

  await page.keyboard.press("?");
  const sheet = page.getByRole("dialog", { name: "Keyboard shortcuts" });
  await expect(sheet).toBeVisible();
  // The three tiers it documents: the palette, the chords (data-driven from
  // chords.ts), and itself.
  await expect(sheet).toContainText("Command palette");
  await expect(sheet).toContainText("This cheat sheet");
  await page.screenshot({ path: "test-results/qa-shell-cheatsheet.png" });

  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);

  // A question mark typed into the composer is punctuation, not a request for
  // help: the sheet must not open, and the character must reach the draft.
  await openView(page, "Chat");
  await composer(page).click();
  await page.keyboard.press("?");
  await expect(composer(page)).toHaveValue("?");
  await expect(sheet).toHaveCount(0);
  // Do not leave a stray "?" draft behind for the next spec's thread.
  await composer(page).fill("");
});

test("the composer's + menu lists its verbs, and a nav row navigates", async ({
  page,
}) => {
  await page.goto("/");
  await newThread(page);

  await page.getByRole("button", { name: "Open tools" }).click();
  const menu = page.getByRole("group", { name: "Tools" });
  await expect(menu).toBeVisible();
  // The five rows the primary mount wires: the two composer verbs, then the
  // three jump-offs (which only exist where the shell handed over setView).
  for (const row of ["Attach a file", "Use a skill", "Sources", "Datasets", "Gallery"]) {
    await expect(menu.getByRole("button", { name: row, exact: true })).toBeVisible();
  }
  await page.screenshot({ path: "test-results/qa-shell-plus-menu.png" });

  // A nav row is a door, not a label: picking Sources leaves the composer and
  // lands on the Sources view, with the menu closed behind it.
  await menu.getByRole("button", { name: "Sources", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
  await expect(menu).toHaveCount(0);

  // Clean up the empty thread this test opened.
  await openView(page, "Chat");
  await deleteActiveThread(page);
});

test("a focused thread rides the URL: reload restores it, leaving chat drops it", async ({
  page,
}) => {
  const PROMPT = "qa shell deep link anchor";
  await page.goto("/");
  await newThread(page);
  await composer(page).fill(PROMPT);
  await composer(page).press("Enter");
  // The prompt and its answer, and the turn over — the rail row now carries
  // the prompt as its title, which is what the reload assertion needs.
  await expect(page.locator(".message")).toHaveCount(2, { timeout: 30_000 });
  await settled(page);
  const row = page.locator(".thread", { hasText: PROMPT }).first();
  await expect(row).toBeVisible();

  // ?t= is written on a microtask after the focus lands, so poll rather than
  // read the URL once.
  await expect
    .poll(() => new URL(page.url()).searchParams.get("t"))
    .not.toBeNull();
  const threadId = new URL(page.url()).searchParams.get("t");

  // A reload is the deep link used on yourself: the same thread comes back
  // focused, with the same id in the URL.
  await page.reload();
  await expect(page.locator(".thread.active")).toContainText(PROMPT);
  await expect
    .poll(() => new URL(page.url()).searchParams.get("t"))
    .toBe(threadId);

  // ?t= is a chat-view fact. Any other view would deep-link to a screen that
  // never reads it, so leaving chat must delete it.
  await openView(page, "Library");
  await expect
    .poll(() => new URL(page.url()).searchParams.get("t"))
    .toBeNull();

  // Clean up: the named thread would make later title lookups ambiguous.
  await openView(page, "Chat");
  await row.locator("button.thread-open").click();
  await expect(page.locator(".thread.active")).toContainText(PROMPT);
  await deleteActiveThread(page);
});
