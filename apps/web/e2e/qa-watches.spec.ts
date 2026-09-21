import { expect, test, type Page } from "@playwright/test";
import { openThreadActions, openView } from "./shell";

/**
 * The two automations this cycle added: a watch that re-reads a source and
 * writes what changed into a standing brief, and a deliverable run that leaves
 * a manifest behind.
 *
 * ON DRIVING THE SCHEDULE. There is no way to make a cron fire in this harness:
 * `POST /api/workflows/tick` is the only dispatcher and it answers 503
 * "Workflow scheduling is not configured" without a `WORKFLOW_CRON_SECRET`,
 * which `serve_e2e.py` deliberately does not set (schedules.spec.ts whitelists
 * that same 503 as the designed answer). So the scheduled path is out of reach
 * from the browser, and this spec drives the watch through "Run now" instead —
 * which is not a workaround but the other half of the feature: the endpoint is
 * claim-free on purpose, because a person pressing the button is asking for one
 * more check regardless of when the last tick was.
 *
 * Self-cleaning: the watch, its brief document, the source, and the workflow
 * and thread a deliverable run leaves behind are all removed. The manifest row
 * itself has no delete route by design and is the one thing that stays.
 */

test.describe.configure({ timeout: 180_000 });

const FILE = "kestrel-e2e.md";
const WATCH = "Kestrel watch";

function watchForErrors(page: Page): string[] {
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(String(error)));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    // Same designed 503 schedules.spec.ts ignores: the Automations group probes
    // whether anything would fire a schedule, and "nothing will" is the answer
    // on a server with no cron secret. The browser still logs the response.
    if (/503 \(Service Unavailable\)/.test(message.text())) return;
    failures.push(message.text());
  });
  return failures;
}

test("a watch made from a source writes a brief when it is run", async ({ page }) => {
  const errors = watchForErrors(page);
  await page.goto("/");

  await openView(page, "Library", /^Sources/);
  await page.locator('input[type="file"]').setInputFiles({
    name: FILE,
    mimeType: "text/markdown",
    buffer: Buffer.from(
      "# Kestrel\n\nThe Kestrel release ships on the first Tuesday of the month. " +
        "It is owned by the platform team.",
    ),
  });
  await expect(
    page.locator(".source-row", { hasText: FILE }).getByText("Indexed"),
  ).toBeVisible({ timeout: 30_000 });

  // "Watch this file" is an icon on the source row: it carries you to the
  // Watches view with the composer open.
  await page.getByRole("button", { name: `Watch ${FILE}` }).click();
  const form = page.locator(".cron-form");
  await expect(page.getByRole("heading", { name: "New watch" })).toBeVisible();
  // The target is picked by hand here because the seed does not survive the
  // hand-off — see the test below, which pins that defect on its own so this
  // one can go on covering the rest of the watch's life. The target select is
  // the second one (the first picks file-or-space) and is addressed by
  // position, because both selects' accessible names begin "File".
  await form.getByRole("combobox").nth(1).selectOption({ label: FILE });
  await form.getByLabel("Name", { exact: true }).fill(WATCH);
  // The default schedule is already legal (a fixed minute — the server refuses
  // anything that could run more than hourly), so it is left alone.
  await expect(form.getByLabel("Schedule")).toHaveValue("0 9 * * *");
  await form.getByRole("button", { name: "Create watch" }).click();

  await expect(page.getByRole("heading", { name: WATCH })).toBeVisible();
  // Nothing has run yet, and the page says exactly that rather than implying a
  // clean check.
  await expect(page.locator(".page-subtitle").first()).toContainText("Never run");

  // Run it by hand. First run: no fingerprint yet, so the target counts as
  // changed and the brief is written.
  await page.getByRole("button", { name: "Run now" }).click();
  await expect(page.locator(".cron-compiled")).toHaveText(
    "Checked — the target changed, and the brief was rewritten.",
    { timeout: 30_000 },
  );
  await expect(page.locator(".page-citations")).toContainText("passage(s) added");
  await expect(page.getByText("Its standing brief is a document in Library → Documents.")).toBeVisible();
  await page.screenshot({ path: "test-results/watch-ran.png", fullPage: true });

  // The brief really is a document, with the watch's own name on it. A reload
  // first: the shell holds the document list from its bootstrap, and this one
  // was written by a background check rather than by anything this tab did.
  await page.reload();
  await openView(page, "Library", /^Documents/);
  await page.getByText(`Brief: ${WATCH}`).first().click();
  await expect(page.getByRole("heading", { name: `Brief: ${WATCH}` })).toBeVisible({
    timeout: 30_000,
  });
  // It is the watch's standing report, not a log line: the watched target and
  // the observation that produced it are both in the body.
  await expect(page.locator(".document-preview")).toContainText("Watching source");
  await page.screenshot({ path: "test-results/watch-brief.png", fullPage: true });

  // A second run with nothing changed writes nothing, and says so — a watch
  // that reported a change every time would be a watch nobody reads.
  await openView(page, "Automations", "Watches");
  await page.locator(".workflow-item", { hasText: WATCH }).first().click();
  await page.getByRole("button", { name: "Run now" }).click();
  await expect(page.locator(".cron-compiled")).toHaveText(
    "Checked — nothing has changed, so nothing was written.",
    { timeout: 30_000 },
  );

  // Clean up. Deleting a watch deliberately KEEPS its brief — what it learned
  // outlives the watching — so the document has to go by hand.
  await openView(page, "Library", /^Documents/);
  await page.getByText(`Brief: ${WATCH}`).first().click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete document" }).click();
  await expect(page.getByText(`Brief: ${WATCH}`)).toHaveCount(0);

  await openView(page, "Automations", "Watches");
  await page.locator(".workflow-item", { hasText: WATCH }).first().click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.locator(".page-actions").getByRole("button", { name: "Delete" }).click();
  await expect(page.locator(".workflow-item", { hasText: WATCH })).toHaveCount(0);

  await openView(page, "Library", /^Sources/);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: `Delete ${FILE}` }).click();
  await expect(page.getByText(FILE)).toHaveCount(0);

  expect(errors).toEqual([]);
});

/**
 * The seed that does not arrive — a defect, pinned rather than worked around.
 *
 * "Watch this file" is supposed to pre-fill the composer with the source's name
 * and id (`workspace.tsx` raises a `watchSeed`, `WatchesView` consumes it). It
 * does not: the effect in `views/watches.tsx` calls `onComposeSeedHandled()`
 * and `setComposing(true)` together, so the parent has already cleared
 * `watchSeed` by the time `WatchComposer` first mounts — and the composer reads
 * `seed` only in its `useState` initialisers. The form therefore opens blank,
 * and a user who clicked a specific file has to find it again in a dropdown.
 *
 * Fixed 2026-09-21: the view now copies the seed to local state before the
 * handled callback lowers the parent's flag (the crons-view pattern), so this
 * runs as a live regression test.
 */
test("the watch composer is seeded from the source that opened it", async ({ page }) => {
  const errors = watchForErrors(page);
  const SEED_FILE = "kestrel-seed-e2e.md";
  await page.goto("/");

  await openView(page, "Library", /^Sources/);
  await page.locator('input[type="file"]').setInputFiles({
    name: SEED_FILE,
    mimeType: "text/markdown",
    buffer: Buffer.from("# Kestrel seed\n\nA file to watch."),
  });
  await expect(
    page.locator(".source-row", { hasText: SEED_FILE }).getByText("Indexed"),
  ).toBeVisible({ timeout: 30_000 });

  try {
    await page.getByRole("button", { name: `Watch ${SEED_FILE}` }).click();
    const form = page.locator(".cron-form");
    await expect(page.getByRole("heading", { name: "New watch" })).toBeVisible();
    // Both halves of the seed: the name it suggests and the target it chose.
    await expect(form.getByLabel("Name", { exact: true })).toHaveValue(SEED_FILE);
    await expect(form.getByRole("combobox").nth(1)).toHaveValue(/.+/);
    expect(errors).toEqual([]);
  } finally {
    // The body above is expected to throw, so the upload is swept from here:
    // a failing test still has to leave the shared workspace as it found it.
    await openView(page, "Library", /^Sources/);
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: `Delete ${SEED_FILE}` }).click();
    await expect(page.getByText(SEED_FILE)).toHaveCount(0);
  }
});

/**
 * A deliverable run, as far as this harness can take it.
 *
 * The preset's four nodes are fixed — plan, research, build, manifest — and the
 * first three are agent nodes whose prompts are baked into `preset_graph()`.
 * The scripted double has no entry for those prompts, so it answers each from
 * the retrieved passages and calls no tools; nothing reaches `sandbox_download`
 * and the manifest therefore names no files. That is asserted below as the
 * honest empty state rather than papered over: what this test proves is that a
 * run started from the browser produces a REAL manifest row, that the write is
 * gated like any other, and that the view reads it back.
 */
test("a deliverable run parks on its manifest, and the manifest reads back", async ({
  page,
}) => {
  const errors = watchForErrors(page);
  const TITLE = "Kestrel deliverable";
  // The manifest is titled from the QUESTION, not from the form's Title field
  // — `preset_graph()` templates the manifest node's `title` off
  // `{{ input.question }}`, and the form's title names the workflow instead. So
  // this is what the Deliverables list will show.
  const QUESTION = "What did the Kestrel release change?";
  await page.goto("/");

  await openView(page, "Automations", "Deliverables");
  await page.getByRole("button", { name: "Start a deliverable run" }).click();
  const form = page.locator(".cron-form");
  await form.getByLabel("Title").fill(TITLE);
  await form.getByLabel("What should it answer?").fill(QUESTION);
  await form.getByRole("button", { name: "Start run" }).click();
  await expect(
    page.locator(".workflow-empty", { hasText: "Watch it on the Workflows page" }),
  ).toBeVisible({ timeout: 30_000 });

  // The manifest node is a write, run at workflow scope, so it parks — the same
  // gate every other write meets. It surfaces in the approval queue with the
  // workflow that raised it named on the card.
  //
  // Polled through the queue's own Refresh button rather than waited on: a
  // workflow run leaves no event stream in this tab (the SSE a chat turn opens
  // belongs to that turn), so the feed this page loaded on mount would stay
  // empty however long the assertion waited.
  await openView(page, "Inbox");
  const queued = page.locator(".approval-card", { hasText: "record_manifest" });
  await expect(async () => {
    await page.getByRole("button", { name: "Refresh" }).click();
    await expect(queued).toBeVisible({ timeout: 3_000 });
  }).toPass({ timeout: 90_000 });
  await expect(queued).toContainText("workflow");
  await page.screenshot({ path: "test-results/deliverable-parked.png", fullPage: true });
  await queued.getByRole("button", { name: /^Approve/ }).click();
  await expect(queued).toHaveCount(0, { timeout: 60_000 });

  // The view loads its list on mount and has no refresh button, so come back
  // to it rather than waiting in place.
  await openView(page, "Automations", "Workflows");
  await openView(page, "Automations", "Deliverables");
  const row = page.locator(".workflow-item", { hasText: QUESTION }).first();
  await expect(row).toBeVisible({ timeout: 60_000 });
  await row.click();

  await expect(page.getByRole("heading", { name: QUESTION })).toBeVisible();
  // The manifest's own accounting: which library it ran against, whether the
  // budget held, and what it spent.
  await expect(page.locator(".page-subtitle").first()).toContainText("Workspace library");
  await expect(page.locator(".page-subtitle").first()).toContainText("tool calls");
  await expect(page.getByRole("heading", { name: "Files" })).toBeVisible();
  // No sandbox download happened, and the manifest says so rather than
  // rendering an empty list that reads like a rendering bug.
  await expect(page.locator(".page-citations")).toContainText(
    "This run produced no files.",
  );
  // Coverage is a separate record and a missing one is not a failure.
  await expect(page.locator(".coverage-ledger-section")).toContainText(
    "No coverage recorded for this run.",
  );
  await page.screenshot({ path: "test-results/deliverable-manifest.png", fullPage: true });

  // --- Put the shared workspace back -------------------------------------
  // A deliverable is an ordinary workflow underneath, so it leaves the same two
  // rows an ordinary run leaves: the workflow itself and the conversation its
  // approval card landed in. Both are this spec's to clear — workflows.spec.ts
  // runs later and asserts the workflow list is EMPTY. The manifest row itself
  // has no delete route by design and stays; nothing else reads it.
  await openView(page, "Automations", "Workflows");
  // The delete lives on the detail pane, so the row is opened first.
  await page.locator(".workflow-item", { hasText: "Deliverable run" }).first().click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete Deliverable run" }).click();
  await expect(page.locator(".workflow-item")).toHaveCount(0);

  await page.reload();
  await openView(page, "Chat");
  const thread = page.getByRole("button", { name: "Delete Workflow: Deliverable run" });
  page.once("dialog", (dialog) => dialog.accept());
  await openThreadActions(
    page.locator(".thread").filter({ hasText: "Workflow: Deliverable run" }).first(),
  );
  await thread.click();
  await expect(thread).toHaveCount(0);

  expect(errors).toEqual([]);
});
