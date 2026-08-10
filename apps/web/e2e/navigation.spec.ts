import { expect, test, type Page } from "@playwright/test";

/**
 * The sidebar is five groups, not eleven subsystems. What this pins down is the
 * part a user feels: a group opens somewhere sensible, its siblings are one
 * click away in the tab strip, and coming back to a group returns you to where
 * you were rather than to its front page.
 *
 * These tests only navigate — they create and delete nothing, so they are safe
 * beside the specs that share this workspace.
 */
const sidebar = (page: Page) => page.getByRole("navigation", { name: "Workspace" });
const tabs = (page: Page, group: string) =>
  page.getByRole("navigation", { name: `${group} views` });

test("the sidebar is five task-shaped groups", async ({ page }) => {
  await page.goto("/");
  await expect(sidebar(page).getByRole("button")).toHaveCount(5);
  for (const label of ["Chat", "Create", "Knowledge", "Connections", "Activity"]) {
    await expect(sidebar(page).getByRole("button", { name: new RegExp(`^${label}`) }))
      .toBeVisible();
  }
  // The subsystem names are gone from the top level: they live inside a group.
  for (const gone of ["Documents", "Databases", "MCP", "Integrations", "Graph"]) {
    await expect(sidebar(page).getByRole("button", { name: gone })).toHaveCount(0);
  }
});

test("each group opens to a default view with its siblings in reach", async ({ page }) => {
  await page.goto("/");

  await sidebar(page).getByRole("button", { name: /^Create/ }).click();
  await expect(page.locator(".documents-layout")).toBeVisible();
  // Documents, Projects, Sandbox, Boards, Dashboards.
  await expect(tabs(page, "Create").getByRole("button")).toHaveCount(5);

  await sidebar(page).getByRole("button", { name: /^Knowledge/ }).click();
  await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
  for (const tab of ["Sources", "Memory", "Graph"]) {
    await expect(tabs(page, "Knowledge").getByRole("button", { name: tab })).toBeVisible();
  }

  await sidebar(page).getByRole("button", { name: /^Connections/ }).click();
  await expect(tabs(page, "Connections").getByRole("button")).toHaveCount(3);
  await tabs(page, "Connections").getByRole("button", { name: /MCP/ }).click();
  await expect(page.getByRole("heading", { name: "MCP servers" })).toBeVisible();

  // Single-view groups get no strip — a tab bar with one tab is noise.
  await sidebar(page).getByRole("button", { name: /^Activity/ }).click();
  await expect(page.locator(".view-tabs")).toHaveCount(0);
});

test("memory is a first-class surface under Knowledge", async ({ page }) => {
  await page.goto("/");
  await sidebar(page).getByRole("button", { name: /^Knowledge/ }).click();
  await tabs(page, "Knowledge").getByRole("button", { name: /Memory/ }).click();

  await expect(page.getByRole("heading", { name: "Memory" })).toBeVisible();
  // The topbar says which group you are in, so the title is never context-free.
  await expect(page.locator(".page-context")).toContainText("Knowledge");

  // Either it has memories — and then they are searchable and forgettable — or
  // it says so. Both are the surface working; which one depends on the run.
  const rows = page.locator(".memory-row");
  if ((await rows.count()) > 0) {
    await expect(rows.first().getByRole("button", { name: "Forget this memory" }))
      .toBeVisible();
    await page.getByRole("searchbox", { name: "Search memory" }).fill("zzzz-no-match");
    await expect(page.locator(".memory-row")).toHaveCount(0);
    await expect(page.getByText(/No memory matches/)).toBeVisible();
  } else {
    await expect(page.getByText(/Nothing remembered yet/)).toBeVisible();
  }

  // Memory left the graph page when it got its own; it must not be in both.
  await tabs(page, "Knowledge").getByRole("button", { name: "Graph" }).click();
  await expect(page.locator(".memory-row")).toHaveCount(0);
});

test("a group reopens where you left it", async ({ page }) => {
  await page.goto("/");
  await sidebar(page).getByRole("button", { name: /^Knowledge/ }).click();
  await tabs(page, "Knowledge").getByRole("button", { name: /Memory/ }).click();
  await expect(page.getByRole("heading", { name: "Memory" })).toBeVisible();

  await sidebar(page).getByRole("button", { name: "Chat" }).click();
  await expect(page.locator(".view-tabs")).toHaveCount(0);

  await sidebar(page).getByRole("button", { name: /^Knowledge/ }).click();
  await expect(page.getByRole("heading", { name: "Memory" })).toBeVisible();
});
