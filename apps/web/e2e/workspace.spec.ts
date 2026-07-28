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

test("build a dataset, dashboard, and published app", async ({ page }) => {
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
  const datasetBuilder = page.locator(".builder-card").first();
  await datasetBuilder.getByLabel("Name").fill("E2E revenue");
  await datasetBuilder.getByLabel("Description").fill("Release-gate dataset");
  await datasetBuilder.getByRole("button", { name: "Create dataset" }).click();
  await expect(page.getByText("E2E revenue", { exact: true }).first()).toBeVisible();

  const dashboardBuilder = page.locator(".builder-card").nth(1);
  await dashboardBuilder.getByLabel("Name").fill("E2E revenue by team");
  await dashboardBuilder.getByLabel("Group").selectOption("team");
  await dashboardBuilder.getByLabel("Metric").selectOption("revenue");
  await dashboardBuilder.getByRole("button", { name: "Create dashboard" }).click();
  await expect(page.getByText("E2E revenue by team", { exact: true })).toBeVisible();
  await expect(page.getByText("North", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: /Apps/ }).click();
  await page.getByLabel("Name").fill("E2E revenue app");
  await page.getByLabel("Visibility").selectOption("public");
  await page.getByLabel("E2E revenue by team").check();
  await page.getByRole("button", { name: "Create draft" }).click();
  await expect(page.getByText("E2E revenue app", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Publish v1" }).click();
  await expect(page.getByRole("link", { name: "Open" })).toBeVisible();
  const publishedPath = await page.getByRole("link", { name: "Open" }).getAttribute("href");
  expect(publishedPath).toBeTruthy();
  await page.goto(publishedPath!);
  await expect(page.getByRole("heading", { name: "E2E revenue app" })).toBeVisible();
  await expect(page.getByText("E2E revenue by team", { exact: true })).toBeVisible();
  await expect(page.getByText("North", { exact: true })).toBeVisible();
});
