import { expect, test, type Page } from "@playwright/test";
import { openSettings } from "./shell";

/**
 * The organization panel, driven against the real API.
 *
 * What is worth a browser for is the round trip, not the markup: a ceiling set
 * here has to be a row `evaluate_policy` reads, and the only way to know the
 * PUT body and the GET shape agree is to set one and reload. The unit test in
 * `tests/organization-panel.test.ts` covers the `null` / `[]` distinction
 * against a mock; this covers the part a mock cannot, which is that the server
 * agrees.
 *
 * The seeded dev owner is also the admin of the seeded organization — exactly
 * what signup produces — so the write controls are present. The *refusal* path,
 * a workspace owner who is not an org admin, is pinned server-side in
 * `tests/test_org_scope.py`, where it belongs: it is an authorization fact, and
 * proving it through a browser would prove the button was disabled rather than
 * that the request was refused.
 */

const orgPanel = (page: Page) =>
  page.locator(".admin-panel").filter({ hasText: "Organization" });

async function openAdmin(page: Page) {
  await page.goto("/");
  await openSettings(page, "Admin");
  await expect(page.getByRole("heading", { name: "Admin" })).toBeVisible();
}

/** Nothing inside a panel may be wider than the panel — see invites.spec.ts. */
async function fitsInsideItsPanel(panel: import("@playwright/test").Locator) {
  const overflow = await panel.evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(overflow, "content is wider than the panel it is in").toBeLessThanOrEqual(0);
}

test("the organization panel states the posture and survives a round trip", async ({
  page,
}) => {
  await openAdmin(page);
  const panel = orgPanel(page);
  await expect(panel).toBeVisible();
  await expect(panel.getByText("admin", { exact: true }).first()).toBeVisible();

  // A tool name unique to this run, so a re-run is not asserting on a leftover.
  const tool = `probe_${Date.now()}`;
  await panel.getByLabel("Tool").fill(tool);
  await panel.getByLabel("Scope").selectOption("workflow");
  await panel.getByLabel("Ceiling").selectOption("deny");
  await panel.getByRole("button", { name: "Set ceiling" }).click();
  await expect(panel.getByText(tool)).toBeVisible();

  await fitsInsideItsPanel(panel);
  // The panel rather than the page: this spec is about one card, and a
  // full-page shot of the admin screen buries it under the spend charts.
  await panel.screenshot({ path: "test-results/organization-panel.png" });

  // The round trip: come back to a freshly mounted panel and the ceiling is
  // still there, which is only true if the PUT wrote what the GET reads.
  // Re-navigated rather than reloaded because the view is client state and not
  // a URL, so a reload lands on chat and would prove nothing.
  await openAdmin(page);
  await expect(orgPanel(page).getByText(tool)).toBeVisible();

  // And clearing it removes the row rather than leaving a stale one on screen.
  await orgPanel(page)
    .getByLabel(`Clear the workflow ceiling for ${tool}`)
    .click();
  await expect(orgPanel(page).getByText(tool)).toHaveCount(0);
});
