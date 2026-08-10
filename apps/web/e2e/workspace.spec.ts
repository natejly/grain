import { expect, test, type Page } from "@playwright/test";

/**
 * Navigation is grouped: the sidebar has five groups and a group's siblings sit
 * in a tab strip above the view. Both navs are addressed by accessible name so
 * a group label ("Create") never collides with a form button of the same name.
 */
async function openView(page: Page, group: string, tab?: RegExp | string) {
  // The group badge is part of the button's accessible name ("Create 4"), so
  // anchor on the label rather than asking for an exact match.
  await page
    .getByRole("navigation", { name: "Workspace" })
    .getByRole("button", { name: new RegExp(`^${group}`) })
    .click();
  if (!tab) return;
  await page
    .getByRole("navigation", { name: `${group} views` })
    .getByRole("button", { name: tab })
    .click();
}

test("upload, cited answer, provenance, graph, approval, and deletion", async ({
  page,
}) => {
  await page.goto("/");
  await openView(page, "Knowledge", /Sources/);
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

  await openView(page, "Knowledge", /Graph/);
  await expect(page.getByText("Project Northstar", { exact: true }).first()).toBeVisible();

  await page.getByRole("button", { name: "Chat", exact: true }).click();
  await composer.fill("/tool github-zen");
  await composer.press("Enter");
  await openView(page, "Activity");
  await expect(page.getByText("github-zen", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Deny" }).click();
  await expect(page.getByText("No pending requests")).toBeVisible();

  await openView(page, "Knowledge", /Sources/);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByTitle("Delete source").click();
  await expect(page.getByText("northstar-e2e.md")).toHaveCount(0);
});

test("build a dashboard from chat, then publish it", async ({ page }) => {
  await page.goto("/");
  await openView(page, "Knowledge", /Sources/);
  await page.locator('input[type="file"]').setInputFiles({
    name: "revenue-e2e.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(
      "team,month,revenue\nNorth,2026-01,10\nSouth,2026-01,20\nNorth,2026-02,15\n",
    ),
  });
  await expect(page.getByText("Indexed").last()).toBeVisible();

  await openView(page, "Create", /Dashboards/);
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

function chatComposer(page: Page) {
  // The placeholder tracks whether the workspace has an indexed source, which
  // earlier tests change; either wording is the same box.
  return page.getByPlaceholder(/Ask your workspace|Upload a source/);
}

/**
 * The two tests below cover the approval flow, driven by the scripted model
 * provider the e2e API server runs (apps/web/e2e/agent-script.json). Only the
 * model is faked: the loop parks the run, the card renders the tool's own
 * preview, and the decision goes through POST
 * /api/agent-tool-calls/{id}/decision with its normal guards.
 */
test("an agent write is proposed with a diff, applied on approve, dropped on deny", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New thread" }).click();
  const composer = chatComposer(page);

  await composer.fill("Draft the launch runbook.");
  await composer.press("Enter");

  const createCard = page.locator(".tool-card", { hasText: "create_document" });
  await expect(createCard).toBeVisible({ timeout: 20_000 });
  await expect(createCard.getByText("Needs approval")).toBeVisible();
  await expect(
    createCard.locator(".diff-line.add", { hasText: "Step two: run the migrations." }),
  ).toBeVisible();

  await createCard.getByRole("button", { name: "Approve" }).click();
  await expect(
    page.getByText("Drafted the Launch Runbook with three steps."),
  ).toBeVisible({ timeout: 20_000 });

  await composer.fill("Tighten step two of the runbook.");
  await composer.press("Enter");

  const editCard = page.locator(".tool-card", { hasText: "edit_document" });
  await expect(editCard).toBeVisible({ timeout: 20_000 });
  const added = editCard.locator(".diff-line.add");
  const removed = editCard.locator(".diff-line.del");
  await expect(removed).toHaveText("-Step two: run the migrations.");
  await expect(added).toHaveText(
    "+Step two: run the migrations, then smoke-test checkout.",
  );
  // Colour is the only thing separating the two halves of a diff, so measure it.
  const [addFill, removeFill] = await Promise.all([
    added.evaluate((node) => getComputedStyle(node).backgroundColor),
    removed.evaluate((node) => getComputedStyle(node).backgroundColor),
  ]);
  expect(addFill).not.toBe(removeFill);

  await editCard.getByRole("button", { name: "Deny" }).click();
  await expect(page.getByText("I left the Launch Runbook untouched.")).toBeVisible({
    timeout: 20_000,
  });

  await openView(page, "Create", /Documents/);
  await page.getByRole("button", { name: /Launch Runbook/ }).click();
  const body = page.locator(".document-source");
  await expect(body).toHaveValue(/Step two: run the migrations\./);
  await expect(body).not.toHaveValue(/smoke-test/);
});

async function openPlaybook(page: Page) {
  await openView(page, "Create", /Documents/);
  await page.getByRole("button", { name: /Rollback Playbook/ }).click();
}

test("a parked write is decidable from the Documents view", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New thread" }).click();
  const composer = chatComposer(page);

  await composer.fill("Draft the rollback playbook.");
  await composer.press("Enter");
  const createCard = page.locator(".tool-card", { hasText: "create_document" });
  await expect(createCard).toBeVisible({ timeout: 20_000 });
  await createCard.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("Drafted the Rollback Playbook.")).toBeVisible({
    timeout: 20_000,
  });

  await composer.fill("Name the on-call rotation in the playbook.");
  await composer.press("Enter");
  await expect(page.locator(".tool-card", { hasText: "edit_document" })).toBeVisible({
    timeout: 20_000,
  });

  // Walk away from chat with the run still parked: the diff has to find the
  // user again beside the document it would change.
  await page.reload();
  await openPlaybook(page);
  const panel = page.locator(".document-pending .tool-card");
  await expect(panel).toContainText("The assistant wants to edit");
  await expect(panel).toContainText("Rollback Playbook");
  await expect(panel.locator(".diff-line.del")).toHaveText(
    "-Page the on-call engineer.",
  );
  await expect(panel.locator(".diff-line.add")).toHaveText(
    "+Page the on-call engineer in the payments rotation.",
  );

  await panel.getByRole("button", { name: "Approve" }).click();
  await expect(page.locator(".document-pending")).toHaveCount(0);

  // No reload: the handler follows the run's event stream and refetches once the
  // tool reports completion, so the open editor picks the write up on its own.
  // The value can only come from the server, so this also proves the write landed.
  await expect(page.locator(".document-source")).toHaveValue(/payments rotation/, {
    timeout: 20_000,
  });
});
