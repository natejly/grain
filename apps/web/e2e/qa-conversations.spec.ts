import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { newThread, openThreadActions, openView } from "./shell";

/**
 * The conversations half of desktop parity, from the outside: the rail search
 * finds what was SAID and opens the thread; a transcript leaves the product as
 * a Markdown or JSON download; a public link shows the live transcript to a
 * browser with no cookies and dies on revoke; "> words" in the palette sends
 * without visiting the composer; and a chat draft becomes a schedule's prompt.
 *
 * Every prompt here is either from agent-script.json or deliberately
 * unmatched (the scripted double answers unmatched prompts from retrieved
 * passages) — no live provider anywhere. Threads created are deleted, because
 * the suite shares one workspace and runs in file order.
 */

const composer = (page: Page) => page.getByRole("textbox", { name: "Message" });

async function settled(page: Page) {
  await expect(page.getByRole("button", { name: "Stop generating" })).toHaveCount(0, {
    timeout: 30_000,
  });
}

/** Open a fresh thread, ask, and wait for the whole turn — the 5s default
 *  flakes under the full suite, so the answer waits carry 30s. */
async function namedThread(page: Page, prompt: string) {
  await newThread(page);
  await composer(page).fill(prompt);
  await composer(page).press("Enter");
  await expect(page.locator(".message")).toHaveCount(2, { timeout: 30_000 });
  await settled(page);
}

/** Delete the thread whose rail row carries this title. */
async function deleteThread(page: Page, title: string) {
  await openView(page, "Chat");
  const row = page.locator(".thread", { hasText: title }).first();
  await row.locator("button.thread-open").click();
  await expect(page.locator(".thread.active")).toContainText(title.slice(0, 40));
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

test("rail search finds a thread by what was said and opens it", async ({ page }) => {
  const PROMPT = "the osprey migration ledger needs a summary";
  await page.goto("/");
  await namedThread(page, PROMPT);
  // A second thread, so opening the hit is a real switch and not a no-op.
  await newThread(page);

  const input = page.getByRole("textbox", { name: "Search chats" });
  const results = page.getByRole("listbox", { name: "Chat search results" });
  // The transcript index is written when the run finalizes; retry the query
  // rather than trusting one debounce to land after the indexer.
  await expect(async () => {
    await input.fill("");
    await input.fill("osprey");
    await expect(
      results.getByRole("option").filter({ hasText: PROMPT }).first(),
    ).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 30_000 });

  await results.getByRole("option").filter({ hasText: PROMPT }).first().click();
  // Opening the hit clears the search and lands in the thread itself.
  await expect(page.locator(".thread.active")).toContainText("osprey migration");
  await expect(page.locator(".message").first()).toContainText(PROMPT);
  await expect(input).toHaveValue("");

  await deleteThread(page, PROMPT);
  await deleteThread(page, "New conversation");
});

test("a transcript downloads as Markdown and as JSON, named after itself", async ({
  page,
}) => {
  const PROMPT = "export ledger checklist please";
  await page.goto("/");
  await namedThread(page, PROMPT);

  const active = page.locator(".thread.active");
  // Markdown first. The download event is the assertion: the export leaves
  // through a blob anchor, so nothing navigates and nothing else observes it.
  await openThreadActions(active);
  const mdDownload = page.waitForEvent("download");
  await active.getByRole("button", { name: /^Export .* as Markdown$/ }).click();
  expect((await mdDownload).suggestedFilename()).toBe(
    "export-ledger-checklist-please.md",
  );

  // JSON second — the menu closed itself on the first pick.
  await openThreadActions(active);
  const jsonDownload = page.waitForEvent("download");
  await active.getByRole("button", { name: /^Export .* as JSON$/ }).click();
  const download = await jsonDownload;
  expect(download.suggestedFilename()).toBe("export-ledger-checklist-please.json");
  // The JSON is the transcript, not a receipt: the thread's wire view under
  // `conversation`, the full message list beside it.
  const payload = JSON.parse(readFileSync(await download.path(), "utf8")) as {
    conversation?: { title?: string };
    messages?: { role: string; content: string }[];
  };
  expect(payload.conversation?.title).toBe(PROMPT);
  expect(payload.messages?.length ?? 0).toBeGreaterThanOrEqual(2);
  expect(payload.messages?.[0]?.content).toBe(PROMPT);

  await deleteThread(page, PROMPT);
});

test("a public conversation link serves the live transcript cookie-less, until revoked", async ({
  page,
  browser,
}) => {
  const PROMPT = "check the rollout date";
  await page.goto("/");
  await namedThread(page, PROMPT);

  // Mint from the thread menu. The URL is shown exactly once, in the modal.
  const active = page.locator(".thread.active");
  await openThreadActions(active);
  await active.getByRole("button", { name: /^Public link for / }).click();
  const modal = page.getByRole("dialog", { name: /^Share / });
  await expect(modal).toBeVisible();
  await modal.getByRole("button", { name: "Create share link" }).click();
  const minted = await modal.locator(".invite-link code").innerText();
  expect(minted).toContain("/share/");

  // A browser with no cookie jar at all — the token is the whole credential.
  const anon = await browser.newContext();
  try {
    const pub = await anon.newPage();
    const served = await pub.goto(minted);
    expect(served?.status()).toBe(200);
    await expect(pub.locator(".shared-transcript")).toContainText(PROMPT);
    await expect(pub.locator(".shared-turn.assistant").first()).toContainText(
      "Assistant",
    );
    await expect(pub.getByText("Shared read-only")).toBeVisible();
    // No token leak beyond the one in the address bar: the page carries the
    // transcript, not the sharing machinery.
    await expect(pub.getByRole("button", { name: "Revoke" })).toHaveCount(0);

    // Revoke, and the same URL answers the uniform 404.
    await modal.getByRole("button", { name: "Revoke" }).click();
    await expect(modal.getByRole("button", { name: "Revoke" })).toHaveCount(0);
    const dark = await pub.goto(minted);
    expect(dark?.status()).toBe(404);
  } finally {
    await anon.close();
  }

  await modal.getByRole("button", { name: "Close share dialog" }).click();
  await deleteThread(page, PROMPT);
});

test("the palette's '>' prefix sends the words and lands in the new thread", async ({
  page,
}) => {
  const WORDS = "falcon quick compose probe";
  await page.goto("/");
  await openView(page, "Chat");

  await page.keyboard.press("ControlOrMeta+k");
  const palette = page.getByRole("dialog", { name: "Command palette" });
  await palette.getByRole("textbox").fill(`> ${WORDS}`);
  // Compose mode is exactly one row, so Enter can only mean send.
  await expect(palette.getByRole("option")).toHaveCount(1);
  await page.keyboard.press("Enter");
  await expect(palette).toHaveCount(0);

  // The words landed as the first message of a thread that is now active.
  await expect(page.locator(".message").first()).toContainText(WORDS, {
    timeout: 30_000,
  });
  await expect(page.locator(".thread.active")).toContainText(WORDS);
  await settled(page);

  await deleteThread(page, WORDS);
});

test("the composer's '+' menu hands the draft to the schedule composer", async ({
  page,
}) => {
  const DRAFT = "Summarise the falcon ledger every morning";
  await page.goto("/");
  await newThread(page);
  await composer(page).fill(DRAFT);

  await page.getByRole("button", { name: "Open tools" }).click();
  await page
    .getByRole("group", { name: "Tools" })
    .getByRole("button", { name: "Do this on a schedule…" })
    .click();

  // Landed on the cron composer with the words already in the Prompt field.
  await expect(page.getByRole("heading", { name: "New automation" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "Prompt" })).toHaveValue(DRAFT);

  // The Plus button is still the blank composer — a seed must not haunt it.
  await page.getByRole("button", { name: "New automation" }).click();
  await expect(page.getByRole("textbox", { name: "Prompt" })).toHaveValue("");

  // Nothing was created; the only leftover is the thread the draft sat in.
  await deleteThread(page, "New conversation");
});
