import { expect, test as setup } from "@playwright/test";
import { DEMO_EMAIL, DEMO_PASSWORD, STORAGE_STATE } from "./credentials";

/**
 * Runs once, before every other project. The API no longer resolves cookie-less
 * requests to anybody, so the suite signs in the way a user does and hands the
 * resulting cookie jar to the rest of the specs.
 */
setup("sign in as the demo workspace owner", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Email").fill(DEMO_EMAIL);
  await page.getByLabel("Password").fill(DEMO_PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  // The sidebar is the proof: app chrome only renders for a resolved session.
  await expect(page.getByRole("button", { name: "New thread" })).toBeVisible();

  await page.context().storageState({ path: STORAGE_STATE });
});
