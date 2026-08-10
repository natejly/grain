import { expect, test, type Page } from "@playwright/test";

/**
 * The demo account is in two workspaces (see apps/api/scripts/serve_e2e.py), and
 * only the second one holds a conversation called "Radio silence". That is the
 * whole trick: a title that exists in exactly one workspace turns "the views
 * refetched" into something a test can see, rather than something to hope for.
 *
 * These tests create and delete nothing — they only switch, and switch back —
 * so they are safe beside the specs that share this workspace.
 */
const DEMO = "Acme Knowledge Lab";
const SECOND = "Field notes";
const SECOND_THREAD = "Radio silence";

const switcher = (page: Page) =>
  page.getByRole("button", { name: /^Switch workspace/ });

async function switchTo(page: Page, name: string) {
  await switcher(page).click();
  await page
    .getByRole("group", { name: "Your workspaces" })
    .getByRole("button", { name: new RegExp(name) })
    .click();
}

test("switching workspace replaces what every view is showing", async ({ page }) => {
  await page.goto("/");
  await expect(switcher(page)).toContainText(DEMO);
  await expect(page.locator(".thread-list")).not.toContainText(SECOND_THREAD);

  await switchTo(page, SECOND);

  await expect(switcher(page)).toContainText(SECOND);
  await expect(page.locator(".thread-list")).toContainText(SECOND_THREAD);
  // The shell remounted rather than patching a list or two, so the open
  // conversation is the new workspace's — a partial refresh would have left the
  // topbar naming a thread that belongs to the workspace we just left.
  await expect(page.locator(".page-context")).toContainText(SECOND_THREAD);
  await expect(page.locator(".error-toast")).toHaveCount(0);

  // The choice is persisted, not just held in this component's state.
  await page.reload();
  await expect(switcher(page)).toContainText(SECOND);
  await expect(page.locator(".thread-list")).toContainText(SECOND_THREAD);

  // Put the browser back where every other spec expects to find it. Contexts
  // are per-test so this cannot leak, but the suite has been broken twice by a
  // spec that left the shared workspace changed.
  await switchTo(page, DEMO);
  await expect(switcher(page)).toContainText(DEMO);
  await expect(page.locator(".thread-list")).not.toContainText(SECOND_THREAD);
});

test("a stored workspace the user is no longer in falls back", async ({ page }) => {
  await page.goto("/");
  await page.evaluate(() =>
    window.localStorage.setItem(
      "jasmine.workspace-id",
      "00000000-0000-4000-8000-0000deadbeef",
    ),
  );
  await page.reload();

  // Sending that id would 403 every request behind it, so a membership removed
  // while the tab was closed must not brick the app.
  await expect(switcher(page)).toContainText(DEMO);
  await expect(page.getByRole("button", { name: "New thread" })).toBeVisible();
  await expect(page.locator(".error-toast")).toHaveCount(0);

  await page.evaluate(() => window.localStorage.removeItem("jasmine.workspace-id"));
});
