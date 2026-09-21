import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { DEMO_PASSWORD } from "./credentials";
import { newThread, openSettings, openThreadActions, openView } from "./shell";

/**
 * The person, not the workspace: the response style that follows the member
 * into every thread, the display name teammates see, the password, a verdict
 * on an answer that survives a reload, and the memory ledger leaving and
 * entering as a file.
 *
 * Everything here edits the ONE shared harness account, so every test puts
 * the member back the way it found them — style to normal, name to its seeded
 * value, password to the one the cookie jar was minted with, memories
 * forgotten. A failure between change and restore is why the restores sit as
 * close to the change as the assertion allows.
 */

const composer = (page: Page) => page.getByRole("textbox", { name: "Message" });

async function settled(page: Page) {
  await expect(page.getByRole("button", { name: "Stop generating" })).toHaveCount(0, {
    timeout: 30_000,
  });
}

async function deleteActiveThread(page: Page) {
  await openView(page, "Chat");
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

const settingsMenu = async (page: Page) => {
  await page.getByRole("button", { name: "Workspace settings" }).click();
  return page.getByRole("group", { name: "Workspace settings" });
};

/** The one memory row holding this text — content is this suite's identity. */
const memoryRow = (page: Page, text: string) =>
  page.locator(".memory-row", { hasText: text });

async function forgetMemory(page: Page, text: string) {
  page.once("dialog", (dialog) => dialog.accept());
  await memoryRow(page, text)
    .getByRole("button", { name: "Forget this memory" })
    .click();
  await expect(memoryRow(page, text)).toHaveCount(0);
}

test("the response style persists across a reload and shows on the composer", async ({
  page,
}) => {
  await page.goto("/");

  // Set "concise" from Settings, and wait for the PUT — the optimistic
  // select answers the pick, but only the server survives a reload.
  let menu = await settingsMenu(page);
  const stylePick = () => menu.getByLabel("Response style", { exact: true });
  await expect(stylePick()).toHaveValue("normal");
  let saved = page.waitForResponse("**/api/me/style");
  await stylePick().selectOption("concise");
  await saved;
  await page.keyboard.press("Escape");

  await page.reload();
  menu = await settingsMenu(page);
  await expect(stylePick()).toHaveValue("concise");
  await page.keyboard.press("Escape");

  // The composer indicator names the same choice, scoped "· you" because it
  // follows the member, not the thread.
  await newThread(page);
  const indicator = page.getByLabel("Response style · you");
  await expect(indicator).toHaveValue("concise");

  // Restore the harness default through the indicator itself — same handler,
  // same PUT — so later specs meet the member unstyled.
  saved = page.waitForResponse("**/api/me/style");
  await indicator.selectOption("normal");
  await saved;
  await expect(indicator).toHaveValue("normal");
  await deleteActiveThread(page);
});

test("renaming yourself shows up where teammates see you", async ({ page }) => {
  const SEEDED = "Nate";
  const RENAMED = "Nate Quill";
  await page.goto("/");

  // The seeded identity block, before anything is touched.
  await expect(page.locator(".workspace-identity strong")).toHaveText(SEEDED);

  await openSettings(page, "Profile");
  await expect(page.getByRole("heading", { name: "Profile" })).toBeVisible();
  // The login identity is read-only; the display name is the editable half.
  await expect(page.getByLabel("Email")).not.toBeEditable();

  const name = page.getByLabel("Display name");
  await name.fill(RENAMED);
  await page.getByRole("button", { name: "Save name" }).click();
  await expect(page.getByText("Name saved.")).toBeVisible();
  // The session refresh carries the new name into the identity block — the
  // attribution surface presence and messages read from — with no reload.
  await expect(page.locator(".workspace-identity strong")).toHaveText(RENAMED);

  // Put the member back for every spec after this one.
  await name.fill(SEEDED);
  await page.getByRole("button", { name: "Save name" }).click();
  await expect(page.getByText("Name saved.")).toBeVisible();
  await expect(page.locator(".workspace-identity strong")).toHaveText(SEEDED);
});

test("a password change round-trips, and this session survives it", async ({
  page,
}) => {
  // The harness account signs in with a password (auth.setup.ts proves it
  // every run), so the happy path is testable — no skip needed. The change is
  // reverted in the same test: the suite's cookie jar survives (the route
  // revokes every OTHER session), but auth.setup's next run must still know
  // the password.
  const TEMPORARY = `${DEMO_PASSWORD}-rotated`;
  await page.goto("/");
  await openSettings(page, "Profile");

  const change = async (current: string, next: string) => {
    await page.getByLabel("Current password").fill(current);
    await page.getByLabel("New password", { exact: true }).fill(next);
    await page.getByLabel("Confirm new password").fill(next);
    // Waited on the wire, not the success line: the line from the FIRST
    // change would still be on screen during the second, and a test that
    // reads it early could leave the account rotated for the rest of the run.
    const answered = page.waitForResponse(
      (response) =>
        response.url().includes("/api/me/password") &&
        response.request().method() === "POST",
    );
    await page.getByRole("button", { name: "Change password" }).click();
    expect((await answered).status()).toBe(200);
    await expect(page.locator(".profile-success")).toBeVisible();
  };

  await change(DEMO_PASSWORD, TEMPORARY);
  await change(TEMPORARY, DEMO_PASSWORD);

  // The caller's own session is the one that survives — a reload still lands
  // in the shell, not on the login screen.
  await page.reload();
  await expect(page.locator(".workspace-identity strong")).toBeVisible();
});

test("a thumbs-down with a note sticks to the message across a reload", async ({
  page,
}) => {
  const PROMPT = "feedback probe for the fable suite";
  await page.goto("/");
  await newThread(page);
  await composer(page).fill(PROMPT);
  await composer(page).press("Enter");
  await expect(page.locator(".message")).toHaveCount(2, { timeout: 30_000 });
  await settled(page);

  const answer = page.locator(".message.assistant").last();
  const thumbsDown = answer.getByRole("button", { name: "Bad answer" });
  // Down opens the note popover first; Send posts verdict and note together.
  await thumbsDown.click();
  await page
    .getByLabel("What went wrong? (optional)")
    .fill("Too vague for the ledger.");
  await page
    .locator(".feedback-note-pop")
    .getByRole("button", { name: "Send" })
    .click();
  await expect(thumbsDown).toHaveAttribute("aria-pressed", "true");

  // The reload reads my_feedback from the transcript itself — the verdict is
  // a stored row, not component state.
  await page.reload();
  await expect(page.locator(".message")).toHaveCount(2, { timeout: 30_000 });
  await expect(
    page.locator(".message.assistant").last().getByRole("button", { name: "Bad answer" }),
  ).toHaveAttribute("aria-pressed", "true");
  // The note never serializes back — the popover greets the next open empty.
  await expect(page.locator(".feedback-note-pop")).toHaveCount(0);

  await deleteActiveThread(page);
});

test("the memory ledger downloads as a dated JSON export", async ({ page }) => {
  const FACT = "The export fox archives every ledger";
  await page.goto("/");
  await openView(page, "Library", /Memory/);
  await expect(page.getByRole("heading", { name: "Memory" })).toBeVisible();

  // The Download button renders only over a non-empty ledger; seed one row.
  await page.getByRole("button", { name: "Add memory" }).click();
  await page.getByRole("textbox", { name: "New memory" }).fill(FACT);
  await page.locator(".memory-add-form").getByRole("button", { name: "Save" }).click();
  await expect(memoryRow(page, FACT)).toBeVisible();

  const downloading = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download memory" }).click();
  const download = await downloading;
  expect(download.suggestedFilename()).toMatch(/^grain-memory-\d{4}-\d{2}-\d{2}\.json$/);
  const payload = JSON.parse(readFileSync(await download.path(), "utf8")) as {
    format?: string;
    version?: number;
    items?: { content: string }[];
  };
  expect(payload.format).toBe("grain-memory-export");
  expect(payload.version).toBe(1);
  expect(payload.items?.some((item) => item.content === FACT)).toBe(true);

  await forgetMemory(page, FACT);
});

test("a memory import reports its accounting and lands rows on the page", async ({
  page,
}) => {
  const FIRST = "Imported: the heron census is quarterly";
  const SECOND = "Imported: the kestrel ridge count is four";
  await page.goto("/");
  await openView(page, "Library", /Memory/);

  await page.getByLabel("Memory file to import").setInputFiles({
    name: "qa-memory-import.json",
    mimeType: "application/json",
    buffer: Buffer.from(
      JSON.stringify({
        format: "grain-memory-export",
        version: 1,
        items: [{ content: FIRST }, { content: SECOND, kind: "preference" }],
      }),
    ),
  });

  // The dismissible line is the accounting; both contents are fresh, so both
  // count as added rather than reinforced. Filtered by its own words, because
  // the import also fires the workspace "Memory updated" toast beside it.
  const summary = page.locator(".notice-toast", { hasText: "reinforced" });
  await expect(summary).toContainText("2 added, 0 reinforced, 0 skipped");
  await expect(memoryRow(page, FIRST)).toBeVisible();
  await expect(memoryRow(page, SECOND)).toBeVisible();
  await expect(memoryRow(page, SECOND).locator(".memory-kind")).toHaveText(
    "preference",
  );
  await page.getByRole("button", { name: "Dismiss import summary" }).click();
  await expect(summary).toHaveCount(0);

  await forgetMemory(page, FIRST);
  await forgetMemory(page, SECOND);
});
