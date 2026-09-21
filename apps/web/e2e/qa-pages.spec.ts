import { expect, test, type Page } from "@playwright/test";
import { newThread, openThreadActions, openView, sendPrompt } from "./shell";

/**
 * Publishing a thread as a Page: the frozen copy, the cookie-less reader, and
 * the revoke that turns the link dark.
 *
 * A page is a thread's citations pinned to the passages that were actually
 * checked, so the reader's copy carries the excerpts themselves rather than a
 * live lookup. That is what the anonymous half asserts: the quote is served
 * from the page, to a browser that has never signed in.
 *
 * Scripted provider; the page, the share link and the 404 are all real. The
 * source, the thread, the page and the link are all removed before it returns.
 */

const ANSWER_TIMEOUT = 45_000;
test.describe.configure({ timeout: 180_000 });


const FILE = "harbour-e2e.md";
const PROMPT = "Audit the Meridian rollout for publication.";
const PAGE_TITLE = "Meridian rollout review";

function watchForErrors(page: Page): string[] {
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") failures.push(message.text());
  });
  return failures;
}

test("a thread publishes as a frozen page, served cookie-less until revoked", async ({
  page,
  browser,
}) => {
  const errors = watchForErrors(page);
  await page.goto("/");

  // Something to cite. The answer is scripted against this text, so the page's
  // frozen citation is a known quote rather than whatever retrieval found.
  await openView(page, "Library", /^Sources/);
  await page.locator('input[type="file"]').setInputFiles({
    name: FILE,
    mimeType: "text/markdown",
    buffer: Buffer.from(
      "# Meridian\n\nThe Meridian rollout reduces onboarding time by forty percent. " +
        "Meridian is owned by the platform team.",
    ),
  });
  await expect(
    page.locator(".source-row", { hasText: FILE }).getByText("Indexed"),
  ).toBeVisible({ timeout: 30_000 });

  await newThread(page);
  await sendPrompt(page, PROMPT);
  await expect(page.locator(".message")).toHaveCount(2, { timeout: ANSWER_TIMEOUT });
  await expect(page.locator(".grounding-plate")).toBeVisible({
    timeout: ANSWER_TIMEOUT,
  });

  // Publish from the thread's own action menu — the third way a thread leaves
  // this workspace, beside the download and the public transcript link. The
  // title is asked for rather than derived, through window.prompt.
  const active = page.locator(".thread.active");
  await openThreadActions(active);
  page.once("dialog", (dialog) => {
    expect(dialog.message()).toContain("Its citations will be frozen");
    void dialog.accept(PAGE_TITLE);
  });
  await active.getByRole("button", { name: /^Publish .* as a page$/ }).click();

  // Publishing lands on the Pages view. The list is what it lands on — the
  // detail pane still says "Select a page." — so the new row is opened here.
  const listed = page.locator(".workflow-item", { hasText: PAGE_TITLE }).first();
  await expect(listed).toBeVisible({ timeout: 30_000 });
  await listed.click();
  await expect(page.getByRole("heading", { name: PAGE_TITLE })).toBeVisible();
  await expect(page.locator(".page-subtitle")).toContainText("frozen citation");
  await expect(page.getByRole("heading", { name: "Frozen citations" })).toBeVisible();
  const frozen = page.locator(".page-citations blockquote").first();
  await expect(frozen).toContainText("reduces onboarding time by forty percent");
  await page.screenshot({ path: "test-results/page-published.png", fullPage: true });

  // Share it. The URL is shown exactly once, in the modal.
  await page.locator(".page-actions").getByRole("button", { name: "Share" }).click();
  const modal = page.getByRole("dialog", { name: `Share ${PAGE_TITLE}` });
  await expect(modal).toBeVisible();
  await modal.getByRole("button", { name: "Create share link" }).click();
  const minted = await modal.locator(".invite-link code").innerText();
  expect(minted).toContain("/share/");

  // A browser with no cookie jar at all — the token is the whole credential.
  const anon = await browser.newContext();
  try {
    const reader = await anon.newPage();
    const served = await reader.goto(minted);
    expect(served?.status()).toBe(200);
    await expect(reader.getByRole("heading", { name: PAGE_TITLE })).toBeVisible();
    await expect(reader.getByText("Shared read-only")).toBeVisible();
    // The answer's prose and the passage behind it, both frozen into the page.
    await expect(reader.locator(".document-preview")).toContainText(
      "reduces onboarding time by forty percent",
    );
    await expect(reader.getByRole("heading", { name: "Citations" })).toBeVisible();
    await expect(reader.locator(".shared-page-citations blockquote").first()).toContainText(
      "Meridian is owned by the platform team",
    );
    // No sharing machinery reaches the reader: the page carries the content.
    await expect(reader.getByRole("button", { name: "Revoke" })).toHaveCount(0);
    await reader.screenshot({
      path: "test-results/page-shared-anon.png",
      fullPage: true,
    });

    // Revoke, and the same URL answers the uniform 404 — the same answer an
    // unknown token gets, so a revoked link cannot be told from a wrong one.
    await modal.getByRole("button", { name: "Revoke" }).click();
    await expect(modal.getByRole("button", { name: "Revoke" })).toHaveCount(0);
    const dark = await reader.goto(minted);
    expect(dark?.status()).toBe(404);
  } finally {
    await anon.close();
  }
  await modal.getByRole("button", { name: "Close share dialog" }).click();

  // Clean up: the page, then the thread, then the source.
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator(".page-actions").getByRole("button", { name: "Delete" }).click();
  await expect(page.getByRole("heading", { name: PAGE_TITLE })).toHaveCount(0);

  await openView(page, "Chat");
  await page.locator(".thread", { hasText: PROMPT }).first().locator("button.thread-open").click();
  page.once("dialog", (dialog) => dialog.accept());
  await openThreadActions(page.locator(".thread.active"));
  await page
    .locator(".thread.active")
    .getByRole("button", { name: /^Delete / })
    .click();
  await expect(page.locator(".thread", { hasText: PROMPT })).toHaveCount(0);

  await openView(page, "Library", /^Sources/);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: `Delete ${FILE}` }).click();
  await expect(page.getByText(FILE)).toHaveCount(0);

  expect(errors).toEqual([]);
});
