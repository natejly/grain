import { expect, test } from "@playwright/test";
import { openView } from "./shell";

/**
 * The browser half of chat attachments: the paperclip's two-destination menu,
 * the chip strip, the split-pane editor, and the negative that makes the
 * feature safe — a file attached to a chat never surfaces in the workspace
 * library. The scope mechanics behind these are proved by
 * test_chat_attachments.py; this proves a person can reach them.
 *
 * Both files are attached into threads this spec creates, and a scoped source
 * is invisible to the Sources view by design, so nothing here disturbs the
 * specs that treat the shared library as their own.
 */

test("a text file attached to a chat becomes an editable chip", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New thread" }).click();

  await page.getByRole("button", { name: "Attach a file" }).click();
  const menu = page.getByRole("group", { name: "Attach a file" });
  const picker = menu.locator('input[type="file"]');
  await picker.setInputFiles({
    name: "field-guide.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("# Field guide\n\nHerons stand still.\n"),
  });
  await menu.getByRole("button", { name: "Attach to this chat" }).click();

  // The chip is the feature's face: named, present after the menu closes, and
  // for a text file it is a button that opens the editor beside the chat.
  const strip = page.locator(".attachment-strip");
  await expect(strip).toContainText("field-guide.md");
  // The chip regression half of the preview work: a text file becomes a
  // document, and a document's preview IS the editor its chip opens — no CSV
  // peek, no thumbnail, the plain chip it always was.
  await expect(strip.locator(".attachment-csv-peek")).toHaveCount(0);
  await strip.getByRole("button", { name: "field-guide.md", exact: true }).click();
  await expect(page.locator(".attachment-pane textarea")).toHaveValue(
    /Herons stand still/,
  );
});

test("a chat-scoped upload stays out of the workspace library", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New thread" }).click();

  await page.getByRole("button", { name: "Attach a file" }).click();
  const menu = page.getByRole("group", { name: "Attach a file" });
  await menu.locator('input[type="file"]').setInputFiles({
    name: "sightings.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("bird,count\nheron,2\n"),
  });
  await menu.getByRole("button", { name: "Attach to this chat" }).click();
  await expect(page.locator(".attachment-strip")).toContainText("sightings.csv");

  // A small CSV earns a peek: its first rows as a table under the chip. The
  // optimistic chip from the upload itself carries no media type, so the
  // enrichment arrives with the next listing — a reload performs one, and the
  // thread rides the URL through it.
  await page.reload();
  const strip = page.locator(".attachment-strip");
  await expect(strip).toContainText("sightings.csv");
  const peek = strip.locator(".attachment-csv-peek");
  await expect(peek).toBeVisible();
  await expect(peek.locator("th").first()).toHaveText("bird");
  await expect(peek.locator("td", { hasText: "heron" })).toBeVisible();
  // Decoration, not replacement: the chip and its filename stay the handle.
  await expect(strip.locator(".attachment-chip.with-peek")).toContainText(
    "sightings.csv",
  );

  // The negative half: the library never lists it. This is the difference
  // between "attach to this chat" and "add to workspace", made visible.
  await openView(page, "Library", /Sources/);
  await expect(page.getByText("sightings.csv")).toHaveCount(0);
});
