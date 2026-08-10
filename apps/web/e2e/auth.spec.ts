import { expect, test } from "@playwright/test";

/**
 * The one spec that starts with no session at all — every other spec inherits
 * the signed-in jar from auth.setup.ts. It walks the whole loop a new customer
 * walks: sign up, sign in, do something that needs the CSRF header, sign out,
 * and sign back in to find the work still there.
 */
test.use({ storageState: { cookies: [], origins: [] } });

const PASSWORD = "correct-horse-battery";

function freshEmail(): string {
  return `e2e-${Date.now()}-${Math.floor(Math.random() * 1000)}@example.com`;
}

test("sign up, land in a workspace, sign out, sign back in", async ({ page }) => {
  const email = freshEmail();

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  // No app chrome for a signed-out visitor.
  await expect(page.getByRole("button", { name: "New thread" })).toHaveCount(0);

  await page.getByRole("button", { name: "Create an account" }).click();
  await page.getByLabel("Name").fill("Sweep Owner");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Create account" }).click();

  // Signup never signs anybody in, and never says whether the address was
  // already taken — it only says it sent mail.
  await expect(page.getByRole("heading", { name: "Check your email" })).toBeVisible();
  await page.getByRole("button", { name: "Back to sign in" }).click();

  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();

  // Their own workspace, with their own starter agent — not the demo tenant.
  await expect(page.getByRole("button", { name: "New thread" })).toBeVisible();
  await expect(page.locator(".workspace-identity")).toContainText(email);

  // A POST that only succeeds with the CSRF header attached.
  await page.getByRole("button", { name: "New thread" }).click();
  await expect(page.locator(".thread")).toHaveCount(1);
  await expect(page.locator(".error-toast")).toHaveCount(0);

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.getByRole("button", { name: "New thread" })).toHaveCount(0);

  // The cookie is gone server-side too, not just cleared in this component.
  await page.reload();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();

  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("button", { name: "New thread" })).toBeVisible();
  await expect(page.locator(".thread")).toHaveCount(1);
});

test("a wrong password and an unknown address get the same answer", async ({ page }) => {
  await page.goto("/");

  await page.getByLabel("Email").fill("nobody-at-all@example.com");
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.locator(".auth-error")).toHaveText("Invalid email or password");

  await page.getByLabel("Email").fill("demo@example.com");
  await page.getByLabel("Password").fill("definitely-not-the-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  // Byte-identical: the screen must not become an account-enumeration oracle.
  await expect(page.locator(".auth-error")).toHaveText("Invalid email or password");
  await expect(page.getByRole("button", { name: "New thread" })).toHaveCount(0);
});
