import { expect, test, type Page } from "@playwright/test";
import { createFromMenu, newThread, openThreadActions, openView } from "./shell";

/**
 * The surfaces half of desktop parity: a space's knowledge shelf says how
 * full it is, a document's history is a stepper with real line diffs and a
 * working Restore, a small CSV attached to a chat peeks its first rows
 * instead of standing as a bare chip, and the member's month has a page of
 * its own behind Library's "You" shelf.
 *
 * Everything created here — a space, a document, a thread — is deleted before
 * its test returns, because the suite shares one workspace in file order.
 */

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

test("a space's knowledge meter counts its files and bytes", async ({ page }) => {
  const SPACE = "QA Meter Space";
  const list = page.locator(".spaces-list");
  const detail = page.locator(".space-detail");

  await page.goto("/");
  await openView(page, "Chat", /^Spaces/);
  await expect(page.locator(".spaces-layout")).toBeVisible();

  // Leftovers from a retried run would 422 the create below.
  const stale = list.getByRole("button", { name: new RegExp(`^${SPACE}`) });
  if ((await stale.count()) > 0) {
    await stale.first().click();
    page.once("dialog", (dialog) => dialog.accept());
    await detail.getByRole("button", { name: `Actions for ${SPACE}` }).click();
    await page.getByRole("button", { name: `Delete ${SPACE}` }).click();
    await expect(stale).toHaveCount(0);
  }

  await list.getByRole("textbox", { name: "Space name" }).fill(SPACE);
  await list.getByRole("button", { name: "Create space" }).click();
  await expect(detail.getByRole("textbox", { name: "Rename space" })).toHaveValue(SPACE);

  // Empty shelf, honest meter: zero files, zero bytes, and the meter is a
  // real meter to assistive tech, not a styled div.
  const meter = detail.getByRole("meter", { name: "Knowledge capacity" });
  await expect(meter).toBeVisible();
  await expect(detail.locator(".capacity-meter-line")).toHaveText("0 files, 0 B");

  await detail.locator('input[type="file"]').setInputFiles({
    name: "qa-meter-note.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("The kestrel ridge count lives here.\n"),
  });
  await expect(detail.getByText("qa-meter-note.md")).toBeVisible();
  // The meter sums the same rows the list below draws, so one file in the
  // list is one file on the line, with its size in honest units.
  await expect(detail.locator(".capacity-meter-line")).toHaveText(/^1 file, \d+ B$/);

  page.once("dialog", (dialog) => dialog.accept());
  await detail.getByRole("button", { name: `Actions for ${SPACE}` }).click();
  await page.getByRole("button", { name: `Delete ${SPACE}` }).click();
  await expect(
    list.getByRole("button", { name: new RegExp(`^${SPACE}`) }),
  ).toHaveCount(0);
});

test("document history steps through versions, shows the diff, and restores", async ({
  page,
}) => {
  // A version row is the snapshot of what a save REPLACED (documents.py
  // stores the pre-save content; Restore puts exactly that back), so three
  // saves yield versions holding "", V1 and V2. Each row's diff is the save
  // it NAMES: its own snapshot against the next row's — or against the live
  // document for the newest row, so the most recent save (V2→V3 here) is
  // viewable too rather than nowhere.
  const V1 = "alpha line one\nshared tail line";
  const V2 = "alpha line two\nshared tail line";
  const V3 = "alpha line three\nshared tail line";
  await page.goto("/");
  await createFromMenu(page, "Document", "QA Stepper Doc");
  await expect(page.getByRole("heading", { name: "QA Stepper Doc" })).toBeVisible();

  const source = page.getByRole("textbox", { name: "Document source" });
  const save = async () => {
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByRole("button", { name: "Saved", exact: true })).toBeVisible();
  };
  await source.fill(V1);
  await save();
  await source.fill(V2);
  await save();
  await source.fill(V3);
  await save();

  await page.getByRole("button", { name: "History" }).click();
  const history = page.locator(".document-history");
  // Three manual saves, three snapshots, oldest to newest.
  await expect(history.locator(".document-history-pick")).toHaveCount(3);

  // The newest version's save is the V2→V3 edit — its snapshot (V2)
  // against the live document (V3), the red/green pair a person expects
  // under that row's summary, in the same renderer proposals use.
  await history.locator(".document-history-pick").last().click();
  await expect(history).toContainText("Version 3 of 3");
  const diff = history.locator(".document-history-diff");
  await expect(diff.locator(".diff-line.del", { hasText: "alpha line two" })).toBeVisible();
  await expect(diff.locator(".diff-line.add", { hasText: "alpha line three" })).toBeVisible();

  // Step back: the middle row's save replaced V1 with V2.
  await page.getByRole("button", { name: "Older version" }).click();
  await expect(history).toContainText("Version 2 of 3");
  await expect(diff.locator(".diff-line.del", { hasText: "alpha line one" })).toBeVisible();
  await expect(diff.locator(".diff-line.add", { hasText: "alpha line two" })).toBeVisible();

  // And the oldest row is the first save over the empty document the create
  // made, so its whole content reads as additions.
  await page.getByRole("button", { name: "Older version" }).click();
  await expect(history).toContainText("Version 1 of 3");
  await expect(diff.locator(".diff-line.add", { hasText: "alpha line one" })).toBeVisible();
  await expect(diff.locator(".diff-line.del")).toHaveCount(0);

  // Restore the middle version — the V1 snapshot — and watch the editor
  // follow.
  await page.getByRole("button", { name: "Newer version" }).click();
  await expect(history).toContainText("Version 2 of 3");
  await history
    .locator("li.selected")
    .getByRole("button", { name: "Restore" })
    .click();
  await expect(source).toHaveValue(V1);
  // The restore snapshots what it replaced — history grows, never rewrites.
  await expect(history.locator(".document-history-pick")).toHaveCount(4);

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete document" }).click();
  await expect(page.getByText("QA Stepper Doc")).toHaveCount(0);
});

test("a small CSV attached to a chat peeks its first rows under the chip", async ({
  page,
}) => {
  await page.goto("/");
  await newThread(page);

  await page.getByRole("button", { name: "Attach a file" }).click();
  const menu = page.getByRole("group", { name: "Attach a file" });
  await menu.locator('input[type="file"]').setInputFiles({
    name: "qa-peek.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("bird,count\nheron,2\nkestrel,4\n"),
  });
  await menu.getByRole("button", { name: "Attach to this chat" }).click();

  const strip = page.locator(".attachment-strip");
  await expect(strip).toContainText("qa-peek.csv");

  // The optimistic chip carries no media type; the enriched row arrives with
  // the next listing, which a reload performs. The thread survives the reload
  // through the URL, so this is the same page a returning user sees.
  await page.reload();
  await expect(strip).toContainText("qa-peek.csv");
  const peek = strip.locator(".attachment-csv-peek");
  await expect(peek).toBeVisible();
  await expect(peek.locator("th").first()).toHaveText("bird");
  await expect(peek.locator("td", { hasText: "heron" })).toBeVisible();
  await expect(peek.locator("td", { hasText: "kestrel" })).toBeVisible();
  // The peek decorates the chip rather than replacing it — the filename is
  // still the handle for detach and for telling files apart.
  await expect(strip.locator(".attachment-chip.with-peek")).toContainText("qa-peek.csv");

  await deleteActiveThread(page);
});

test("the recap opens from Library's You shelf and counts the month", async ({
  page,
}) => {
  await page.goto("/");
  // At least one thread this month is this test's own doing, so the count
  // below is a floor the test itself established.
  await newThread(page);

  await openView(page, "Library", /^Recap/);
  await expect(page.getByRole("heading", { name: "Recap" })).toBeVisible();
  await expect(page.getByText(/Your month so far/)).toBeVisible();

  // Three tiles, counted exactly: threads, runs, memories.
  const tiles = page.locator(".recap-tile");
  await expect(tiles).toHaveCount(3);
  await expect(tiles.nth(0)).toContainText(/threads? started/);
  await expect(tiles.nth(1)).toContainText(/runs?/);
  await expect(tiles.nth(2)).toContainText(/memor(y|ies) learned/);
  const threadsStarted = Number(await tiles.nth(0).locator("strong").innerText());
  expect(threadsStarted).toBeGreaterThanOrEqual(1);

  await deleteActiveThread(page);
});
