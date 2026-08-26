import { expect, test, type Page } from "@playwright/test";
import { rail } from "./shell";

/**
 * The shell at phone width. Below the drawer breakpoint the icon rail is
 * gone; the four doors ride a bottom tab bar instead, and the drawer keeps
 * everything else (switcher, sections, threads, identity). These are the
 * mobile facts the desktop suite cannot see — every other spec runs at
 * Desktop Chrome size, where the tab bar is display:none — so this file sets
 * its own viewport and navigates through the bar directly. `shell.ts`'s
 * `openView` is deliberately not used here: it walks the desktop rail, which
 * is exactly the surface this file asserts is absent.
 */

test.use({ viewport: { width: 390, height: 844 } });

// The dev server's <nextjs-portal> badge floats bottom-left — exactly over
// the tab bar at a phone viewport — and intercepts pointer events aimed at
// the Chat door. A dev-only artifact production never renders; hide it
// rather than force-click past it, so the specs still prove the BAR's own
// hit targets work.
test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const style = document.createElement("style");
    style.textContent = "nextjs-portal { display: none !important; }";
    document.addEventListener("DOMContentLoaded", () =>
      document.head.appendChild(style),
    );
  });
});

const tabBar = (page: Page) =>
  page.getByRole("navigation", { name: "Destinations" });

test("the four doors ride the bottom bar, not the rail", async ({ page }) => {
  await page.goto("/");
  // The badge joins a door's accessible name ("Inbox 3"), so anchor on the
  // label the way the desktop specs anchor on the rail's.
  for (const label of ["Chat", "Inbox", "Library", "Automations"]) {
    await expect(
      tabBar(page).getByRole("button", { name: new RegExp(`^${label}`) }),
    ).toBeVisible();
  }
  await expect(tabBar(page).getByRole("button")).toHaveCount(4);
  // The rail is not merely off screen — it is display:none, or a screen
  // reader would announce the same four doors twice.
  await expect(rail(page)).toBeHidden();
});

test("the bar navigates: Inbox and back to Chat", async ({ page }) => {
  await page.goto("/");
  await tabBar(page)
    .getByRole("button", { name: /^Inbox/ })
    .click();
  await expect(page.getByRole("heading", { name: "Inbox" })).toBeVisible();
  // The bar itself must survive the trip — a tab bar that scrolls away with
  // the view is a drawer with worse manners.
  await expect(
    tabBar(page).getByRole("button", { name: /^Inbox/ }),
  ).toBeInViewport();

  await tabBar(page)
    .getByRole("button", { name: /^Chat/ })
    .click();
  await expect(
    tabBar(page).getByRole("button", { name: /^Chat/ }),
  ).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { name: "Inbox" })).toHaveCount(0);
});

test("the drawer still opens, minus the doors it used to carry", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Open menu" }).click();
  // In-viewport, not merely visible: the closed drawer is translated off
  // screen, and a transform leaves an element "visible" to the DOM.
  await expect(page.getByRole("button", { name: "Close menu" })).toBeInViewport();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeInViewport();
  // The destination list moved out of the drawer when the bar arrived; a
  // second labelled list here would be the drift the shared renderer exists
  // to prevent.
  await expect(
    page.locator("#workspace-rail").getByRole("navigation", { name: "Destinations" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Close menu" }).click();
  await expect(page.getByRole("button", { name: "Sign out" })).not.toBeInViewport();
});
