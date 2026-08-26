import { expect, test, type Page } from "@playwright/test";
import { openView } from "./shell";

/**
 * Spaces, from the outside: create one, give it instructions and a knowledge
 * file, start a thread in it, and watch the rail chip appear.
 *
 * Specs share one workspace and run in file order, so everything created here
 * is deleted before the file returns. Deleting the space is the cleanup: the
 * server cascades over the space's threads and files — that is the feature's
 * own contract (the confirm dialog says so), and the end of this spec asserts
 * it rather than unwinding piece by piece.
 */

const SPACE = "E2E Space";
const INSTRUCTIONS = "Always cite the kestrel figures.";

const list = (page: Page) => page.locator(".spaces-list");
const detail = (page: Page) => page.locator(".space-detail");

async function openSpaces(page: Page) {
  await page.goto("/");
  await openView(page, "Chat", /^Spaces/);
  await expect(page.locator(".spaces-layout")).toBeVisible();
}

/** Leftovers from a retried run would 422 the create below; clear them first. */
async function deleteIfPresent(page: Page, name: string) {
  const row = list(page).getByRole("button", { name: new RegExp(`^${name}`) });
  if ((await row.count()) === 0) return;
  await row.first().click();
  page.once("dialog", (dialog) => void dialog.accept());
  await detail(page).getByRole("button", { name: `Delete ${name}` }).click();
  await expect(row).toHaveCount(0);
}

test("a space carries instructions, knowledge and threads, and dies whole", async ({
  page,
}) => {
  await openSpaces(page);
  await deleteIfPresent(page, SPACE);

  // Create, and land selected.
  await list(page).getByRole("textbox", { name: "Space name" }).fill(SPACE);
  await list(page).getByRole("button", { name: "Create space" }).click();
  await expect(detail(page).getByRole("textbox", { name: "Rename space" })).toHaveValue(
    SPACE,
  );

  // Instructions persist across a reload — they live on the server, not in
  // the buffer.
  await detail(page)
    .getByRole("textbox", { name: "Space instructions" })
    .fill(INSTRUCTIONS);
  // Exact, because "Save <space> as template" also lives on the detail now and
  // role-name matching is a substring match by default.
  await detail(page).getByRole("button", { name: "Save", exact: true }).click();
  await expect(detail(page).getByRole("button", { name: "Save", exact: true })).toBeDisabled();
  await page.reload();
  await openView(page, "Chat", /^Spaces/);
  await list(page).getByRole("button", { name: new RegExp(`^${SPACE}`) }).click();
  await expect(
    detail(page).getByRole("textbox", { name: "Space instructions" }),
  ).toHaveValue(INSTRUCTIONS);

  // A knowledge file lands on the space. Assert on the filename, not on
  // "Indexed" — ingestion is async and its status may lag.
  await detail(page)
    .locator('input[type="file"]')
    .setInputFiles({
      name: "e2e-space-note.md",
      mimeType: "text/markdown",
      buffer: Buffer.from("The kestrel figures live in table 4."),
    });
  await expect(detail(page).getByText("e2e-space-note.md")).toBeVisible();

  // A thread started here is an ordinary rail thread wearing the space's chip.
  await detail(page).getByRole("button", { name: "New thread" }).click();
  await expect(page.locator(".message-scroll.empty")).toBeVisible();
  const railRow = page
    .locator(".thread-list")
    .getByRole("button", { name: /^New conversation/ })
    .first();
  await expect(railRow.locator(".thread-space-chip")).toHaveText(SPACE);

  // And the space page lists it back.
  await openView(page, "Chat", /^Spaces/);
  await list(page).getByRole("button", { name: new RegExp(`^${SPACE}`) }).click();
  await expect(
    detail(page).getByRole("button", { name: /^New conversation/ }),
  ).toBeVisible();

  // Delete — the confirm names the stakes, and the cascade IS the cleanup:
  // afterwards the thread is out of the rail and the file is out of Sources.
  page.once("dialog", (dialog) => {
    expect(dialog.message()).toContain("threads");
    void dialog.accept();
  });
  await detail(page).getByRole("button", { name: `Delete ${SPACE}` }).click();
  await expect(
    list(page).getByRole("button", { name: new RegExp(`^${SPACE}`) }),
  ).toHaveCount(0);
  await expect(page.locator(".thread-space-chip")).toHaveCount(0);
  await openView(page, "Library", /^Sources/);
  await expect(page.getByText("e2e-space-note.md")).toHaveCount(0);
});
