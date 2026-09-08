import { expect, test, type Page } from "@playwright/test";

/**
 * The "New workspace" row inside the switcher dropdown. Its backend has ten
 * tests; this is the browser half — the row exists, the form submits, and the
 * shell lands inside the workspace it just made.
 *
 * Isolation: the e2e database is deleted and reseeded on every run
 * (serve_e2e.py), contexts are per-test, and the created workspace's name is
 * unique to this run — so the row it leaves behind can collide with nothing.
 * The demo account's default stays "Acme Knowledge Lab" because defaults order
 * by membership age, and this row is always younger.
 */
const DEMO = "Acme Knowledge Lab";
const CREATED = `Bench ${Date.now().toString(36)}`;

const switcher = (page: Page) =>
  page.getByRole("button", { name: /^Switch workspace/ });

test("a workspace created from the switcher is entered and persists", async ({
  page,
}) => {
  await page.goto("/");
  await expect(switcher(page)).toContainText(DEMO);

  await switcher(page).click();
  const menu = page.getByRole("group", { name: "Your workspaces" });
  await menu.getByRole("button", { name: "New workspace" }).click();

  // The button swapped in place for the inline form.
  const input = menu.getByRole("textbox");
  await expect(input).toBeFocused();
  await input.fill(CREATED);
  await menu.getByRole("button", { name: /^Creat/ }).click();

  // Creating selects: the shell remounts inside the new workspace, which
  // starts empty of the old one's threads.
  await expect(switcher(page)).toContainText(CREATED);
  await expect(page.getByRole("button", { name: "New thread" })).toBeVisible();
  await expect(page.locator(".thread-list")).not.toContainText("Radio silence");
  await expect(page.locator(".error-toast")).toHaveCount(0);

  // Chosen, not just rendered — the reload comes back to the same workspace,
  // and the switcher lists both.
  await page.reload();
  await expect(switcher(page)).toContainText(CREATED);
  await switcher(page).click();
  await expect(
    page.getByRole("group", { name: "Your workspaces" }).getByRole("button", {
      name: new RegExp(DEMO),
    }),
  ).toBeVisible();
});

test("escape backs out of the form without closing the menu", async ({ page }) => {
  await page.goto("/");
  await switcher(page).click();
  const menu = page.getByRole("group", { name: "Your workspaces" });
  await menu.getByRole("button", { name: "New workspace" }).click();
  await menu.getByRole("textbox").press("Escape");

  // The form is gone, the menu is not — Escape peels one layer at a time.
  await expect(menu.getByRole("textbox")).toHaveCount(0);
  await expect(menu.getByRole("button", { name: "New workspace" })).toBeVisible();
  await expect(menu.getByRole("button", { name: new RegExp(DEMO) })).toBeVisible();
});
