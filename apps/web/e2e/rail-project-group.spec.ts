import { expect, test, type Page } from "@playwright/test";
import { createFromMenu, openView } from "./shell";

/**
 * A project's thread, in the rail.
 *
 * The thread beside a project used to exist only inside the project page: the
 * listing filtered every subject thread out, so the one place it could be
 * reached was the panel that made it. It is listed now, grouped under the
 * project's own collapsible header — the same arrangement a space's threads
 * get — and this spec walks the whole life of that group from the outside:
 * it appears when the panel opens the thread, it folds, its row opens the
 * thread in Chat, and it dies with the project.
 *
 * The last step is the one no unit test can make: deleting a project cascades
 * over its thread server-side, so the rail must lose both the header AND the
 * row. A client that only re-read its projects would drop the row into the
 * flat rail as an orphan pointing at a thread that no longer exists.
 *
 * No model turn is needed anywhere here — the thread exists because the panel
 * asked for it, not because anything was said in it.
 */

const PROJECT = "E2E Rail Project";

/** The rail group whose header names this spec's project. */
const group = (page: Page) =>
  page.locator(".thread-project-group").filter({ hasText: PROJECT });

async function openProjects(page: Page) {
  await openView(page, "Library", /^Projects/);
  await expect(page.locator(".projects-layout")).toBeVisible();
}

/** Leftovers from a retried run would 422 the create below; clear them first. */
async function deleteIfPresent(page: Page, name: string) {
  await openProjects(page);
  const row = page.locator(".project-item", { hasText: name });
  if ((await row.count()) === 0) return;
  await row.first().click();
  await expect(page.getByRole("heading", { name })).toBeVisible();
  await page.getByRole("button", { name: "Delete project" }).click();
  await expect(row).toHaveCount(0);
}

test("a project's thread rides the rail under the project's own header", async ({
  page,
}) => {
  await page.goto("/");
  await deleteIfPresent(page, PROJECT);

  await createFromMenu(page, "Project", PROJECT);
  await expect(page.getByRole("heading", { name: PROJECT })).toBeVisible();

  // Opening the panel is what creates the thread — there is no other door to
  // it — so the rail learns about a conversation its own listing has never
  // returned. Scoped to the project's toolbar: "Chat" is also the rail's first
  // destination, and an unscoped name would leave the Projects view.
  await page
    .locator(".project-editor-actions")
    .getByRole("button", { name: "Chat", exact: true })
    .click();
  await expect(page.locator(".project-chat")).toBeVisible();

  await openView(page, "Chat");
  await expect(group(page)).toHaveCount(1);
  // One row, not one per open: the thread is get-or-create keyed on the
  // project, so reopening the panel must never grow this group.
  const row = group(page).locator(".thread-open");
  await expect(row).toHaveCount(1);
  await expect(row).toContainText(PROJECT);

  // The header is the fold, and the row goes with it.
  const toggle = group(page).getByRole("button", { name: `${PROJECT} threads` });
  await toggle.click();
  await expect(row).toBeHidden();
  await toggle.click();
  await expect(row).toBeVisible();

  // And the row is a real rail row: it opens the thread in Chat.
  await row.click();
  await expect(group(page).locator(".thread.active")).toContainText(PROJECT);
  await expect(page.locator(".thread.active")).toHaveCount(1);

  // Delete the project: the server takes the thread with it, so the group and
  // the row both go — the row must not survive as an orphan in the flat rail.
  await openProjects(page);
  await page.locator(".project-item", { hasText: PROJECT }).first().click();
  await expect(page.getByRole("heading", { name: PROJECT })).toBeVisible();
  await page.getByRole("button", { name: "Delete project" }).click();
  await expect(page.locator(".project-item", { hasText: PROJECT })).toHaveCount(0);

  await openView(page, "Chat");
  await expect(group(page)).toHaveCount(0);
  await expect(page.locator(".thread-open", { hasText: PROJECT })).toHaveCount(0);
});
