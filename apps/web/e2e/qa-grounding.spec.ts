import { expect, test, type Page } from "@playwright/test";
import { newThread, openSettings, openThreadActions, openView, sendPrompt } from "./shell";

/**
 * Grounding, from the outside: the per-sentence drawer under an answer, the
 * coverage drawer's honest empty state, and the ledger of what the machine
 * door answered.
 *
 * The verdict these assertions pin is a LEXICAL SUPPORT test and the copy says
 * so — "verified" means the sentence's words are in the passage it cites, not
 * that the sentence is true. That distinction is the whole reason the drawer
 * exists, so it is asserted as rendered text rather than paraphrased here.
 *
 * The defects are PLANTED, not hoped for. `agent-script.json` answers
 * "audit the meridian rollout" with three sentences written against the source
 * this spec uploads:
 *
 *   1. every content word is in the passage, cited [1]  -> verified
 *   2. nothing but "Meridian" is in the passage, cited [1] -> cited, not supported
 *   3. a real claim with no marker at all                 -> uncited
 *
 * so the drawer's counts are arithmetic on a fixture, not a sample of model
 * behaviour. Scripted provider throughout; no live model anywhere.
 *
 * The suite shares one workspace and runs in file order, so every source and
 * thread created here is deleted before the test returns.
 */

/** How long a scripted turn may take to land under a full-suite run. */
const ANSWER_TIMEOUT = 45_000;
// A 45s expect inside Playwright's default 30s test budget fails at 30s, which
// reads as flakiness rather than as the margin it is. Per-file, like
// workspace.spec.ts.
test.describe.configure({ timeout: 180_000 });


/**
 * The document all three tests are graded against.
 *
 * Deliberately distinctive: "Meridian" appears in no other spec's fixture, so
 * the passage this answer cites as `[1]` is the top-ranked one whatever else
 * an earlier spec left indexed.
 */
const SOURCE_BODY =
  "# Meridian\n\nThe Meridian rollout reduces onboarding time by forty percent. " +
  "Meridian is owned by the platform team.";

async function uploadSource(page: Page, filename: string) {
  await openView(page, "Library", /^Sources/);
  await page.locator('input[type="file"]').setInputFiles({
    name: filename,
    mimeType: "text/markdown",
    buffer: Buffer.from(SOURCE_BODY),
  });
  // Scoped to THIS row: sources come back newest-first, so an unscoped
  // "Indexed" resolves against whatever an earlier spec left behind and the
  // wait returns before this upload is ready.
  await expect(
    page.locator(".source-row", { hasText: filename }).getByText("Indexed"),
  ).toBeVisible({ timeout: 30_000 });
}

/** Open the thread whose rail row carries this title. */
async function openThread(page: Page, title: string) {
  await openView(page, "Chat");
  await page
    .locator(".thread", { hasText: title })
    .first()
    .locator("button.thread-open")
    .click();
  await expect(page.locator(".thread.active")).toContainText(title.slice(0, 40));
}

/** Delete the thread whose rail row carries this title. */
async function deleteThread(page: Page, title: string) {
  await openThread(page, title);
  const threads = page.locator(".thread");
  const before = await threads.count();
  page.once("dialog", (dialog) => dialog.accept());
  await openThreadActions(page.locator(".thread.active"));
  await page
    .locator(".thread.active")
    .getByRole("button", { name: /^Delete / })
    .click();
  await expect(threads).toHaveCount(before - 1);
}

async function deleteSource(page: Page, filename: string) {
  await openView(page, "Library", /^Sources/);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: `Delete ${filename}` }).click();
  await expect(page.getByText(filename)).toHaveCount(0);
}

test("the grounding drawer scores an answer sentence by sentence", async ({
  page,
}) => {
  const FILE = "meridian-e2e.md";
  await page.goto("/");
  await uploadSource(page, FILE);

  await newThread(page);
  await sendPrompt(page, "Audit the Meridian rollout.");

  // The plate, collapsed: one headline that carries the score AND the counts
  // behind it, because a bare percentage invites over-reading.
  const plate = page.locator(".grounding-plate");
  await expect(plate).toBeVisible({ timeout: ANSWER_TIMEOUT });
  // "ungrounded" is the tone reserved for a sentence that cites a passage its
  // words are not in — the one defect class this method certifies it catches.
  await expect(plate).toHaveClass(/ungrounded/);
  const summary = plate.getByRole("button");
  // "checked" is load-bearing in that line: a sentence resting on a web result
  // this app never read is left out of the denominator rather than counted, so
  // the percentage is over what was actually checkable here.
  await expect(summary).toHaveText(
    "Grounding 33% — 1 of 3 checked sentences verified, 1 cited but not supported",
  );
  await expect(summary).toHaveAttribute("aria-expanded", "false");

  await summary.click();
  await expect(summary).toHaveAttribute("aria-expanded", "true");
  const detail = plate.locator(".grounding-detail");
  // The sentence that must survive every copy change: it claims support, not
  // truth. If this string ever goes, the drawer is the part to cut.
  await expect(detail.locator(".grounding-meaning")).toContainText(
    "a check of support, not of truth",
  );
  // The rewrite pass has no offline stand-in, and the drawer says that rather
  // than implying the answer was corrected.
  await expect(detail.locator(".grounding-repair")).toHaveText(
    "The rewrite pass is not configured on this deployment, so nothing was corrected.",
  );

  // Three rows, one per scored sentence, each labelled in the reader's words.
  const rows = detail.locator(".grounding-sentence");
  await expect(rows).toHaveCount(3);
  const verified = rows.nth(0);
  await expect(verified).toHaveClass(/verified/);
  await expect(verified.locator(".grounding-badge")).toHaveText("Verified");
  await expect(verified.locator(".grounding-markers")).toHaveText("[1]");
  await expect(verified.locator(".grounding-text")).toContainText(
    "reduces onboarding time by forty percent",
  );

  const unsupported = rows.nth(1);
  await expect(unsupported).toHaveClass(/cited_unsupported/);
  await expect(unsupported.locator(".grounding-badge")).toHaveText(
    "Cited, not supported",
  );
  await expect(unsupported.locator(".grounding-markers")).toHaveText("[1]");
  await expect(unsupported.locator(".grounding-text")).toContainText(
    "fourteen thousand Lisbon customers",
  );

  const uncited = rows.nth(2);
  await expect(uncited).toHaveClass(/uncited/);
  await expect(uncited.locator(".grounding-badge")).toHaveText("Uncited");
  // An uncited row carries no markers at all — not an empty bracket.
  await expect(uncited.locator(".grounding-markers")).toHaveCount(0);

  await page.screenshot({
    path: "test-results/grounding-drawer.png",
    fullPage: true,
  });

  // It is stored on the message, not only streamed: a verdict that vanishes on
  // refresh is barely a verdict. (The drawer reopens closed, which is its
  // resting state.)
  await page.reload();
  await openThread(page, "Audit the Meridian rollout.");
  await expect(page.locator(".grounding-plate").getByRole("button")).toHaveText(
    "Grounding 33% — 1 of 3 checked sentences verified, 1 cited but not supported",
    { timeout: ANSWER_TIMEOUT },
  );

  await deleteThread(page, "Audit the Meridian rollout.");
  await deleteSource(page, FILE);
});

test("the coverage drawer says what it has, and says when it has nothing", async ({
  page,
}) => {
  const FILE = "meridian-coverage-e2e.md";
  await page.goto("/");
  await uploadSource(page, FILE);

  await newThread(page);
  await sendPrompt(page, "Audit the Meridian rollout for coverage.");
  await expect(page.locator(".grounding-plate")).toBeVisible({
    timeout: ANSWER_TIMEOUT,
  });

  // Collapsed and unfetched: only a plan-mode or deliverable run records a
  // ledger, so asking eagerly would fire one 404 per message in the transcript.
  const ledger = page.locator(".coverage-ledger").first();
  await expect(ledger.locator("summary")).toHaveText("Coverage");
  await ledger.locator("summary").click();
  // An ordinary chat turn records no ledger, and the drawer says exactly that
  // rather than rendering an empty report that reads like "nothing consulted".
  await expect(ledger.locator("summary")).toHaveText(
    "No coverage recorded for this run.",
  );

  await deleteThread(page, "Audit the Meridian rollout for coverage.");
  await deleteSource(page, FILE);
});

test("the grounded-answer API leaves a receipt an owner can read", async ({
  page,
  playwright,
}) => {
  const FILE = "meridian-receipt-e2e.md";
  const TOKEN_NAME = "E2E grounded receipts";
  const QUESTION = "What does the Meridian rollout change?";

  await page.goto("/");
  await uploadSource(page, FILE);

  // The machine door is bearer-only, so the spec has to hold a real token.
  // Minted the way a person mints one, because the panel's whole claim is that
  // it shows what THESE tokens answered.
  await openSettings(page, "Connections", "API & Webhooks");
  const tokens = page.locator(".mcp-card", { hasText: "API tokens" });
  await tokens.getByLabel("Token name").fill(TOKEN_NAME);
  await tokens.getByRole("button", { name: "Create token" }).click();
  const secret = await tokens.locator(".invite-link code").innerText();
  expect(secret).not.toEqual("");

  // A request context with no cookie jar: the bearer is the whole credential,
  // and a cookie session is refused by this route on purpose.
  const machine = await playwright.request.newContext();
  try {
    const answered = await machine.post(
      "http://127.0.0.1:8010/api/answers/grounded",
      {
        headers: { Authorization: `Bearer ${secret}` },
        data: { question: QUESTION, limit: 5 },
      },
    );
    expect(answered.status()).toBe(200);
    const body = await answered.json();
    // The verdict rides back with the answer — that is what distinguishes this
    // door from every other answer the product produces.
    expect(body.report.grounding.scored).toBeGreaterThan(0);
    expect(body.citations.length).toBeGreaterThan(0);
  } finally {
    await machine.dispose();
  }

  // The panel renders nothing until the API has been used; now it has been.
  await page.reload();
  await openSettings(page, "Connections", "API & Webhooks");
  const panel = page.locator(".admin-panel", { hasText: "Grounded answers" });
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("The grounding score is a check of support, not of truth");

  const row = panel.locator("li", { hasText: QUESTION }).first();
  await expect(row.locator(".share-link-meta")).toContainText("% grounded");
  await expect(row.locator(".share-link-meta")).toContainText("citations resolve");

  // The receipt opens onto the same verdict components the transcript uses, so
  // the ledger and the chat cannot disagree about what "verified" looks like.
  await row.getByRole("button", { name: QUESTION }).click();
  const detail = row.locator(".grounded-receipt-detail");
  await expect(detail).toBeVisible();
  await expect(detail.locator(".grounding-plate")).toBeVisible();
  await expect(detail.locator(".grounding-plate").getByRole("button")).toHaveText(
    /^Grounding \d+% — \d+ of \d+ checked sentences? verified/,
  );
  await page.screenshot({
    path: "test-results/grounded-receipts.png",
    fullPage: true,
  });

  // Self-cleaning as far as the product allows: the token is revoked and the
  // source deleted. A receipt is a ledger row with no delete route by design,
  // so this one stays — the panel is additive and no other spec reads it.
  await tokens.locator("li", { hasText: TOKEN_NAME }).getByRole("button", { name: "Revoke" }).click();
  await deleteSource(page, FILE);
});
