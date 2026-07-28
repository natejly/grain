import { expect, test } from "@playwright/test";

test("upload, cited answer, provenance, graph, approval, and deletion", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: /Sources/ }).click();
  await page.locator('input[type="file"]').setInputFiles({
    name: "northstar-e2e.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(
      "# Northstar\n\nProject Northstar is owned by Maya Chen at Atlas Labs. " +
        "Maya Chen is responsible for reducing onboarding time by forty percent.",
    ),
  });
  await expect(page.getByText("northstar-e2e.md")).toBeVisible();
  await expect(page.getByText("Indexed").last()).toBeVisible();

  await page.getByRole("button", { name: "Chat", exact: true }).click();
  const composer = page.getByPlaceholder("Ask your workspace…");
  await composer.fill("Who owns Project Northstar?");
  await composer.press("Enter");
  const citation = page.getByRole("button", { name: /northstar-e2e\.md/ });
  await expect(citation).toBeVisible();
  await citation.click();
  await expect(page.locator(".provenance-content")).toContainText(
    "Maya Chen is responsible",
  );
  await page.getByRole("button", { name: "Close provenance" }).click();

  await page.getByRole("button", { name: /Graph/ }).click();
  await expect(page.getByText("Project Northstar", { exact: true }).first()).toBeVisible();

  await page.getByRole("button", { name: "Chat", exact: true }).click();
  await composer.fill("/tool github-zen");
  await composer.press("Enter");
  await page.getByRole("button", { name: /Activity/ }).click();
  await expect(page.getByText("github-zen", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Deny" }).click();
  await expect(page.getByText("No pending requests")).toBeVisible();

  await page.getByRole("button", { name: /Sources/ }).click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByTitle("Delete source").click();
  await expect(page.getByText("northstar-e2e.md")).toHaveCount(0);
});

test("build a dashboard from chat, then publish it", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /Sources/ }).click();
  await page.locator('input[type="file"]').setInputFiles({
    name: "revenue-e2e.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(
      "team,month,revenue\nNorth,2026-01,10\nSouth,2026-01,20\nNorth,2026-02,15\n",
    ),
  });
  await expect(page.getByText("Indexed").last()).toBeVisible();

  await page.getByRole("button", { name: /Dashboards/ }).click();
  await page.getByRole("button", { name: "Add dashboard" }).first().click();

  await page.getByLabel("Dashboard name").fill("E2E revenue app");
  await page.getByLabel("Public link").check();
  // The ready CSV becomes a dataset on its own; the chip proves it landed.
  const chip = page.getByRole("button", { name: "revenue-e2e" });
  await expect(chip).toBeVisible();
  await expect(chip).toHaveAttribute("aria-pressed", "true");

  const composer = page.getByPlaceholder("Describe this dashboard…");
  await composer.fill("Show revenue by team");
  await composer.press("Enter");

  await expect(page.getByText("Built v1")).toBeVisible({ timeout: 30_000 });
  const preview = page.frameLocator(".editor-preview iframe");
  await expect(preview.getByText("revenue-e2e")).toBeVisible();
  await expect(preview.getByText("North").first()).toBeVisible();

  const editor = page.locator(".dashboard-editor");
  await editor.getByRole("button", { name: "Publish v1" }).click();
  await expect(page.getByRole("button", { name: "Publish v1" })).toHaveCount(0);
  await page.getByRole("button", { name: "Close editor" }).click();

  await expect(page.getByText("v1 · published")).toBeVisible();
  const openLink = page.getByRole("link", { name: "Open" });
  const publishedPath = await openLink.getAttribute("href");
  expect(publishedPath).toBe("/apps/e2e-revenue-app");

  await page.goto(publishedPath!);
  await expect(page.getByRole("heading", { name: "E2E revenue app" })).toBeVisible();
  const published = page.frameLocator(".published-code-app iframe");
  await expect(published.getByText("North").first()).toBeVisible();
});
