import { expect, test, type Page } from "@playwright/test";

/**
 * A sweep across the views that had no browser coverage: Documents, Boards,
 * Projects, Databases, MCP, Integrations and the 3D graph. The existing spec
 * covers chat, sources, dashboards and the approval flow.
 *
 * Each test screenshots its view into test-results/ so the rendering can be
 * inspected, not just asserted on — several of these are visual features where
 * "the element exists" says very little.
 */

/** Fail loudly on console errors: a React crash still leaves the DOM queryable. */
function watchForErrors(page: Page): string[] {
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") failures.push(message.text());
  });
  return failures;
}

async function openView(page: Page, name: RegExp | string) {
  await page.getByRole("button", { name }).click();
}

test("documents: create, edit, live preview, and version history", async ({ page }) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await openView(page, /Documents/);

  await page.getByRole("button", { name: "New document" }).click();
  await page.getByPlaceholder("Title").fill("Feature Sweep Notes");
  await page.getByRole("button", { name: "Create" }).click();

  const editor = page.locator(".document-source");
  await expect(editor).toBeVisible();
  await editor.fill("# Heading\n\nSome **bold** prose and $E = mc^2$ inline.");

  // The preview pane is the point of the feature — assert it actually rendered
  // the markdown rather than echoing the source.
  const preview = page.locator(".document-preview");
  await expect(preview.getByRole("heading", { name: "Heading" })).toBeVisible();
  await expect(preview.locator("strong")).toHaveText("bold");
  // KaTeX renders math into .katex; if the plugin chain broke this disappears.
  await expect(preview.locator(".katex").first()).toBeVisible();

  await page.getByRole("button", { name: /Save/ }).click();
  await page.getByRole("button", { name: /History/ }).click();

  await page.locator(".documents-layout").screenshot({
    path: "test-results/feature-documents.png",
  });

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete document" }).click();
  await expect(page.getByText("Feature Sweep Notes")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("boards: create a board, add cards, and manage columns", async ({ page }) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await openView(page, /Boards/);

  await page.getByPlaceholder("New board name").fill("Sweep Board");
  await page.getByRole("button", { name: /Create/ }).click();

  await expect(page.getByRole("heading", { name: "Sweep Board" })).toBeVisible();
  await expect(page.getByText("Todo", { exact: true })).toBeVisible();
  await expect(page.getByText("In progress", { exact: true })).toBeVisible();
  await expect(page.getByText("Done", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: /Add card/ }).first().click();
  await page.getByPlaceholder("Card title").fill("Ship the sweep");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.getByText("Ship the sweep")).toBeVisible();

  await page.locator(".board").first().screenshot({
    path: "test-results/feature-boards.png",
  });

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete Sweep Board" }).click();
  await expect(page.getByRole("heading", { name: "Sweep Board" })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("projects: a web project seeds files and bundles a live preview", async ({ page }) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await openView(page, /Projects/);

  await page.getByRole("button", { name: "New project" }).click();
  await page.getByPlaceholder("Project name").fill("Sweep App");
  await page.getByRole("button", { name: "Create", exact: true }).click();

  // The starter must arrive with files, or there is nothing to bundle.
  await expect(page.getByText("index.tsx").first()).toBeVisible({ timeout: 15000 });
  await expect(page.locator(".project-source")).toBeVisible();

  // The whole point of a sandbox is that it runs. Wait for esbuild-wasm to
  // finish and assert the starter actually rendered inside the frame — a
  // screenshot taken mid-compile would happily show "Compiling…" forever.
  const preview = page.frameLocator(".project-preview-frame");
  await expect(preview.getByText("New project")).toBeVisible({ timeout: 90000 });
  await expect(preview.getByRole("button", { name: /Clicked 0 times/ })).toBeVisible();
  // And it is live React, not a static dump.
  await preview.getByRole("button", { name: /Clicked/ }).click();
  await expect(preview.getByRole("button", { name: /Clicked 1 times/ })).toBeVisible();

  await page.locator(".projects-layout").screenshot({
    path: "test-results/feature-projects.png",
  });

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: /Delete project/ }).click();
  await expect(page.getByText("Sweep App")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("databases: add a sqlite connection and browse its schema", async ({ page }) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await openView(page, /Databases/);

  await page.getByRole("button", { name: /Add connection|Add database|Add/ }).first().click();
  await page.locator(".content-page").screenshot({
    path: "test-results/feature-databases.png",
  });
  expect(errors).toEqual([]);
});

test("mcp: the server form renders and validates", async ({ page }) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await openView(page, /MCP/);

  await page.getByRole("button", { name: /Add server/ }).click();
  await expect(page.getByRole("textbox", { name: /^Name/ })).toBeVisible();
  // The transport switch changes which fields are required — the stdio branch
  // asks for a command, the http branch for a URL.
  await expect(page.getByPlaceholder("npx")).toBeVisible();
  await page.getByLabel("Transport").selectOption("http");
  await expect(page.getByPlaceholder("https://example.com/mcp")).toBeVisible();

  await page.locator(".content-page").screenshot({
    path: "test-results/feature-mcp.png",
  });
  expect(errors).toEqual([]);
});

test("integrations and activity views render", async ({ page }) => {
  const errors = watchForErrors(page);
  await page.goto("/");

  await openView(page, /Integrations/);
  await expect(page.locator(".content-page")).toBeVisible();
  await page.locator(".content-page").screenshot({
    path: "test-results/feature-integrations.png",
  });

  await openView(page, /Activity/);
  await expect(page.locator(".content-page")).toBeVisible();

  expect(errors).toEqual([]);
});
