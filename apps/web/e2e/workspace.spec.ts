import { expect, test, type Page } from "@playwright/test";
import { openSettings, openView } from "./shell";

test("upload, cited answer, provenance, graph, and deletion", async ({
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
  const composer = page.getByRole("textbox", { name: "Message" });
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

  // The `/tool github-zen` approval that used to sit here is gone with the
  // panel that showed it. It only ever had something to display because
  // seed_dev_workspace writes a `Tool` row and no endpoint does — so it proved
  // the dev seed, not the product. The real queue is asserted further down,
  // against an approval the agent loop actually parks.

  await openView(page, "Knowledge", /Sources/);
  page.once("dialog", (dialog) => dialog.accept());
  // Named, not "the delete button": the title is identical on every row, so the
  // bare locator only works while this spec's upload is the workspace's only
  // source — which makes an unrelated file's leak fail *this* test.
  await page.getByRole("button", { name: "Delete northstar-e2e.md" }).click();
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

  await openView(page, "Files", /^Dashboards/);
  await page.getByRole("button", { name: "Add dashboard" }).first().click();

  await page.getByLabel("Dashboard name").fill("E2E revenue app");
  await page.getByLabel("Public link").check();
  // The ready CSV becomes a dataset on its own; the chip proves it landed.
  const chip = page.getByRole("button", { name: "revenue-e2e" });
  await expect(chip).toBeVisible();
  await expect(chip).toHaveAttribute("aria-pressed", "true");

  const composer = page.getByRole("textbox", { name: "Dashboard prompt" });
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
  return page.getByRole("textbox", { name: "Message" });
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

  await openView(page, "Files", /^Files/);
  // Anchored: a file row in the tree is named "<title> <kind>" and its move
  // menu is named "Move <title>", so an unanchored match finds both.
  await page.getByRole("button", { name: /^Launch Runbook/ }).click();
  const body = page.locator(".document-source");
  await expect(body).toHaveValue(/Step two: run the migrations\./);
  await expect(body).not.toHaveValue(/smoke-test/);
});

async function openPlaybook(page: Page) {
  await openView(page, "Files", /^Files/);
  // Anchored, for the same reason as Launch Runbook above.
  await page.getByRole("button", { name: /^Rollback Playbook/ }).click();
}

test("a parked write is decidable from the Files view", async ({ page }) => {
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

  // The same parked call, seen from the approvals queue. This is the assertion
  // the old `/tool github-zen` one should always have been: Activity read the
  // legacy `tool_calls` table, which only the dev seed can populate, so in a
  // real deployment it showed "No pending requests" no matter how many runs
  // were waiting — and the Settings badge, fed by the same list, never lit.
  await expect(page.getByRole("button", { name: /awaiting approval/ })).toBeVisible();
  await openSettings(page, "Activity");
  const queued = page.locator(".approval-card", { hasText: "edit_document" });
  await expect(queued).toBeVisible();
  await expect(queued.locator(".diff-line.add")).toHaveText(
    "+Page the on-call engineer in the payments rotation.",
  );

  // Walk away from chat with the run still parked: the diff has to find the
  // user again beside the document it would change.
  await page.reload();
  await openPlaybook(page);
  // The write is now decided hunk by hunk, in the document itself, rather than
  // accepted whole from a card beside it — so the panel to find is the review
  // section, and the diff lines carry no +/- prefix because the accept state is
  // on the hunk rather than in the text.
  const panel = page.getByRole("region", { name: "Proposed changes" });
  await expect(panel).toContainText("1 proposed change");
  // The title belongs to the editor the panel is docked in: the point of this
  // test is that the diff found the user *beside the document it changes*.
  await expect(page.getByRole("heading", { name: "Rollback Playbook" })).toBeVisible();
  await expect(panel.locator(".diff-line.del")).toHaveText(
    "Page the on-call engineer.",
  );
  await expect(panel.locator(".diff-line.add")).toHaveText(
    "Page the on-call engineer in the payments rotation.",
  );
  // The panel is a diff, two buttons and the document underneath it, and every
  // assertion above passes just as well when they are stacked unreadably. It is
  // also the panel a leaked proposal from another spec doubles up, so the shot
  // is where a second card would be seen rather than only counted.
  await page.screenshot({ path: "test-results/document-parked-write.png", fullPage: true });

  await panel.getByRole("button", { name: "Apply 1 of 1 change" }).click();
  await expect(page.locator(".document-review")).toHaveCount(0);

  // No reload: the handler follows the run's event stream and refetches once the
  // tool reports completion, so the open editor picks the write up on its own.
  // The value can only come from the server, so this also proves the write landed.
  await expect(page.locator(".document-source")).toHaveValue(/payments rotation/, {
    timeout: 20_000,
  });
});

/**
 * The two capabilities below existed server-side and could not be reached from
 * the app: the citation validator reported to an audit row nobody reads, and
 * every chart the sandbox drew was stored, described, counted — and rendered
 * nowhere. Both are the kind of bug a passing test does not see, so these
 * assert on pixels and on rendered text rather than on element presence.
 *
 * They live at the end of this file because the browser suite shares one
 * workspace in file order; everything they create is deleted before they
 * return, and each screenshots what it claims so the claim can be checked.
 */

/**
 * Reopen a thread by name after a reload.
 *
 * Which conversation the shell lands on after a refresh is its own decision and
 * not what these tests are about; asserting through it made a test about
 * artifact durability fail because an empty thread happened to sort first.
 */
async function openThread(page: Page, title: string) {
  await page.getByRole("button", { name: title, exact: false }).first().click();
}

/** True only for an <img> the browser actually decoded. */
async function decoded(image: import("@playwright/test").Locator): Promise<boolean> {
  return image.evaluate(
    (node) =>
      node instanceof HTMLImageElement && node.complete && node.naturalWidth > 0,
  );
}

test("a fabricated citation is flagged under the answer that made it", async ({
  page,
}) => {
  await page.goto("/");
  await openView(page, "Knowledge", /Sources/);
  await page.locator('input[type="file"]').setInputFiles({
    name: "rollout-e2e.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(
      "# Rollout\n\nThe rollout reduces onboarding time by forty percent. " +
        "It is owned by the platform team.",
    ),
  });
  await expect(page.getByText("rollout-e2e.md")).toBeVisible();
  await expect(page.getByText("Indexed").last()).toBeVisible();

  await page.getByRole("button", { name: "Chat", exact: true }).click();
  await page.getByRole("button", { name: "New thread" }).click();
  const composer = chatComposer(page);
  await composer.fill("Check the rollout date for me.");
  await composer.press("Enter");

  // The model cited [42] against a handful of passages. The validator has
  // always caught this; the product has never shown it.
  const flagged = page.locator(".citation-check.fabricated");
  await expect(flagged).toBeVisible({ timeout: 20_000 });
  await expect(flagged).toContainText("[42]");
  await expect(flagged).toContainText("does not match a supplied passage");
  // A correctness warning about the text above it, so it is announced.
  await expect(flagged).toHaveAttribute("role", "alert");
  // Red, not decoration: the warning must not read as the clean variant.
  const fill = await flagged.evaluate((node) => getComputedStyle(node).backgroundColor);
  await page.screenshot({ path: "test-results/citation-fabricated.png", fullPage: true });

  // And the clean verdict is a different thing to look at, so "checked and
  // clean" cannot be confused with "never checked".
  await composer.fill("What does the rollout reduce?");
  await composer.press("Enter");
  const clean = page.locator(".citation-check.clean");
  await expect(clean).toBeVisible({ timeout: 20_000 });
  await expect(clean).toContainText("Citations check out");
  expect(await clean.evaluate((node) => getComputedStyle(node).backgroundColor)).not.toBe(
    fill,
  );

  // It survives a reload, because it is stored on the message rather than only
  // streamed. A warning that vanishes on refresh is barely a warning.
  await page.reload();
  await openThread(page, "Check the rollout date for me.");
  await expect(page.locator(".citation-check.fabricated")).toContainText("[42]");

  await openView(page, "Knowledge", /Sources/);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete rollout-e2e.md" }).click();
  await expect(page.getByText("rollout-e2e.md")).toHaveCount(0);
});

test("a chart the agent draws is visible in the conversation", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New thread" }).click();
  const composer = chatComposer(page);
  await composer.fill("Plot the rollout for me.");
  await composer.press("Enter");

  const card = page.locator(".tool-card", { hasText: "run_python" });
  await expect(card).toBeVisible({ timeout: 30_000 });
  await card.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("Plotted it.")).toBeVisible({ timeout: 30_000 });

  // The whole point. `toBeVisible` passes on a broken image, so the browser is
  // asked whether it actually decoded pixels — the failure this is guarding
  // against rendered an <img> with an empty src and told nobody.
  const figure = card.locator("img.artifact-image");
  await expect(figure).toBeVisible({ timeout: 20_000 });
  await expect
    .poll(() => decoded(figure), { timeout: 15_000 })
    .toBe(true);
  expect(await figure.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBe(
    240,
  );
  // Real alt text, not "" and not the filename.
  await expect(figure).toHaveAttribute("alt", /Figure 1 of 1 produced by run_python/);
  await page.screenshot({ path: "test-results/chat-artifact.png", fullPage: true });

  // Still there after a reload: the descriptors are on the tool call row, not
  // only on the event that streamed past.
  await page.reload();
  await openThread(page, "Plot the rollout for me.");
  const reloaded = page.locator(".tool-card", { hasText: "run_python" }).locator("img");
  await expect(reloaded).toBeVisible({ timeout: 20_000 });
  await expect.poll(() => decoded(reloaded), { timeout: 15_000 }).toBe(true);

  // The figure is also a workspace source, and can be opened from there.
  await openView(page, "Knowledge", /Sources/);
  const row = page.locator(".source-row", { hasText: "sandbox-png-1.png" });
  await expect(row).toBeVisible();
  await expect(row).toContainText("Saved");
  page.once("dialog", (dialog) => dialog.accept());
  await row.getByRole("button", { name: /^Delete sandbox-png-1\.png$/ }).click();
  await expect(page.getByText("sandbox-png-1.png")).toHaveCount(0);
});

/**
 * There is no "the sandbox console shows the figure the code drew" test any
 * more, and its absence is deliberate rather than a gap.
 *
 * The console was a *destination* — a page you visited to operate a machine —
 * and a capability is not a destination. What that test uniquely proved is
 * proved above by the test that precedes it: `persist_artifacts` is the same
 * code path whether the run came from the panel's Run button or from the
 * agent's `run_python`, and the assertion that matters — that a figure reaches
 * a user's screen and survives a reload — is made against the surface a user
 * now actually has.
 *
 * It also took the "stop every live machine" cleanup with it, which is now
 * unnecessary rather than missing: the agent's tools reuse one session per
 * workspace through `ensure_session`, so nothing in this suite creates a second
 * one, and the server reaps idle sessions on its own timer.
 */
