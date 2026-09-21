import { expect, test, type Page } from "@playwright/test";
import { newThread, openThreadActions, openView, sendPrompt } from "./shell";

/**
 * The composer's research controls: the preset picker that SEEDS the controls
 * beside it, the live plan a plan-then-execute turn narrates, and the follow-up
 * chips under an answer.
 *
 * The seed doctrine is the thing worth testing here. A preset is not a hidden
 * policy the run path applies behind the user's back — picking one moves the
 * visible controls, and the user is free to move them back before sending. So
 * every assertion below is on a control a person can see, never on a request
 * body.
 *
 * Scripted provider throughout. Threads created are deleted; the suite shares
 * one workspace and runs in file order.
 */

const ANSWER_TIMEOUT = 45_000;
test.describe.configure({ timeout: 180_000 });

const composer = (page: Page) => page.getByRole("textbox", { name: "Message" });
const presetPicker = (page: Page) =>
  page.getByRole("combobox", { name: "Run preset · this thread" });
const effortPicker = (page: Page) =>
  page.getByRole("combobox", { name: "Reasoning effort · this thread" });
const planToggle = (page: Page) => page.getByRole("button", { name: "Plan", exact: true });
const modeTrigger = (page: Page) => page.getByRole("button", { name: /^Approval mode:/ });

function watchForErrors(page: Page): string[] {
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") failures.push(message.text());
  });
  return failures;
}

/** Open the thread whose rail row carries this title. */
async function openThread(page: Page, title: string) {
  await openView(page, "Chat");
  await page.locator(".thread", { hasText: title }).first().locator("button.thread-open").click();
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

test("a preset seeds the controls beside it, and the thread remembers it", async ({
  page,
}) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await newThread(page);

  // The catalogue is served from bootstrap and is not gated on the provider,
  // so the picker is here even on a scripted deployment.
  await expect(presetPicker(page)).toBeVisible();
  await expect(presetPicker(page)).toHaveValue("");
  await expect(planToggle(page)).toHaveAttribute("aria-pressed", "false");

  // Quick lookup: low effort, no planning.
  await presetPicker(page).selectOption({ label: "Quick lookup" });
  await expect(effortPicker(page)).toHaveValue("low");
  await expect(planToggle(page)).toHaveAttribute("aria-pressed", "false");

  // Deep research: the picker moves effort AND turns planning on. Both are
  // controls the user can now see and override — that is the whole doctrine.
  await presetPicker(page).selectOption({ label: "Deep research" });
  await expect(effortPicker(page)).toHaveValue("high");
  await expect(planToggle(page)).toHaveAttribute("aria-pressed", "true");

  // And back: a preset that only ever tightened would be a trap.
  await presetPicker(page).selectOption({ label: "Quick lookup" });
  await expect(planToggle(page)).toHaveAttribute("aria-pressed", "false");

  // What a preset must NOT reach: the thread's approval mode. It is not a
  // composer control — it is a standing policy every later turn reads — and a
  // picker described as "low effort, three short passages, core tools only"
  // quietly moving a thread out of the mode its owner chose is the one way this
  // feature could do harm. Proved against a mode the user set by hand, so the
  // assertion cannot pass merely because the preset happened to agree.
  await modeTrigger(page).click();
  await page
    .getByRole("group", { name: "Approval mode" })
    .getByRole("button", { name: /^Ask before everything/ })
    .click();
  await expect(modeTrigger(page)).toHaveAccessibleName(
    "Approval mode: Ask before everything",
  );
  await presetPicker(page).selectOption({ label: "Deliverable" });
  await expect(effortPicker(page)).toHaveValue("high");
  await expect(planToggle(page)).toHaveAttribute("aria-pressed", "true");
  await expect(modeTrigger(page)).toHaveAccessibleName(
    "Approval mode: Ask before everything",
  );

  // Put the thread back where the rest of this test expects it.
  await presetPicker(page).selectOption({ label: "Quick lookup" });
  await modeTrigger(page).click();
  await page
    .getByRole("group", { name: "Approval mode" })
    .getByRole("button", { name: /^Ask before writes/ })
    .click();
  await expect(modeTrigger(page)).toHaveAccessibleName(
    "Approval mode: Ask before writes",
  );
  await page.screenshot({
    path: "test-results/preset-picker.png",
    fullPage: true,
  });

  // The pick is remembered on the conversation, not in this tab: send, reload,
  // reopen, and the picker still reads it. (The plan toggle deliberately does
  // NOT survive — planning is a choice about this question, not this thread.)
  await sendPrompt(page, "Audit the Meridian rollout with a preset.");
  await expect(page.locator(".message")).toHaveCount(2, { timeout: ANSWER_TIMEOUT });
  await page.reload();
  await openThread(page, "Audit the Meridian rollout with a preset.");
  await expect(presetPicker(page)).toHaveValue("quick-lookup");
  await expect(planToggle(page)).toHaveAttribute("aria-pressed", "false");

  await deleteThread(page, "Audit the Meridian rollout with a preset.");
  expect(errors).toEqual([]);
});

test("a plan-mode run renders the plan it is working through", async ({ page }) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await newThread(page);

  // The Plan toggle is what puts `submit_plan` / `complete_step` in the turn's
  // registry at all; without it the scripted call would name a tool that does
  // not exist for this run.
  await planToggle(page).click();
  await expect(planToggle(page)).toHaveAttribute("aria-pressed", "true");

  await sendPrompt(page, "Map the Meridian dependencies.");

  // The trail is live narration, so it exists only while the run does. The run
  // parks on a question at the end of the plan, which is what makes this
  // assertable without racing a scripted turn that would otherwise be over in
  // milliseconds — a parked run is not terminal, so the stream stays open.
  const question = page.locator(".tool-card", { hasText: "ask_user" }).first();
  await expect(question).toBeVisible({ timeout: ANSWER_TIMEOUT });

  const trail = page.getByRole("list", { name: "Plan progress" });
  await expect(trail).toBeVisible();
  const steps = trail.locator(".plan-trail-step");
  await expect(steps).toHaveCount(2);
  await expect(steps.nth(0)).toContainText("What does the Meridian rollout change?");
  await expect(steps.nth(1)).toContainText("Who owns Meridian?");
  // The queries the plan committed to, beside the question they answer.
  await expect(steps.nth(0).locator(".plan-trail-query")).toHaveText(
    "meridian rollout onboarding",
  );
  // Both steps closed before the turn parked, each carrying what it found.
  await expect(steps.nth(0)).toHaveClass(/done/);
  await expect(steps.nth(1)).toHaveClass(/done/);
  await expect(steps.nth(0).locator(".plan-trail-summary")).toHaveText(
    "The rollout reduces onboarding time by forty percent.",
  );
  await page.screenshot({ path: "test-results/plan-trail.png", fullPage: true });

  // Answer the question and let the turn finish: the trail is not transcript,
  // so it goes when the run does.
  await question.getByRole("button", { name: /^(Approve|Answer)$/ }).click();
  await expect(page.getByText("Mapped the Meridian dependencies.")).toBeVisible({
    timeout: ANSWER_TIMEOUT,
  });
  await expect(page.getByRole("list", { name: "Plan progress" })).toHaveCount(0);

  await deleteThread(page, "Map the Meridian dependencies.");
  expect(errors).toEqual([]);
});

/**
 * Follow-up chips, against a stubbed message payload — and that stub is the
 * honest part of this test, not a shortcut.
 *
 * The server only admits a suggestion that survives a DENSE retrieval probe
 * (`services/followups.probe` -> `retrieval.dense_ranking`), and
 * `embeddings.embed_batch` returns None for every provider but OpenAI. On the
 * scripted e2e server the probe can therefore never clear, and the column is
 * always `[]` — there is no prompt, in this harness, that produces a chip. The
 * suggester itself is covered by `apps/api/tests/test_followups.py`, which
 * monkeypatches the embedder.
 *
 * What is left over, and what is asserted here, is the half that lives in the
 * browser and has no other coverage in a real page: chips render in a stable
 * order, and clicking one SEEDS the composer rather than sending it — the rule
 * that stops a stray click from spending a turn.
 */
test("a follow-up chip seeds the composer and never sends", async ({ page }) => {
  const errors = watchForErrors(page);

  // Rewrite the transcript fetch on its way back, leaving everything else —
  // the render, the ordering, the click handler, the draft store — real.
  await page.route("**/api/conversations/*/messages*", async (route) => {
    // Only the transcript READ is rewritten; a send goes through untouched.
    if (route.request().method() !== "GET") return route.continue();
    const response = await route.fetch();
    const body = await response.json();
    if (Array.isArray(body) && body.length > 0) {
      const last = body[body.length - 1];
      if (last.role === "assistant") {
        last.followups = [
          {
            text: "What does the workspace say about Rollout?",
            origin: "heading",
            probe_score: 0.4,
            chunk_ids: ["chunk-a"],
          },
          {
            text: "How does Meridian relate to the platform team?",
            origin: "kg",
            probe_score: 0.9,
            chunk_ids: ["chunk-a", "chunk-b"],
          },
        ];
      }
    }
    await route.fulfill({ response, json: body });
  });

  await page.goto("/");
  await newThread(page);
  await sendPrompt(page, "Audit the Meridian rollout for follow-ups.");
  await expect(page.locator(".message")).toHaveCount(2, { timeout: ANSWER_TIMEOUT });

  // The chips arrive with the transcript on a reopen, which is also the path a
  // reader takes back to an answer they left.
  await page.reload();
  await openThread(page, "Audit the Meridian rollout for follow-ups.");

  const chips = page.locator(".followups .followup-chip");
  await expect(chips).toHaveCount(2, { timeout: ANSWER_TIMEOUT });
  // Knowledge-graph chips first: a graph neighbour is a claim about the corpus,
  // a heading is a claim about the answer, and the order has to be total so two
  // renders of one payload cannot differ.
  await expect(chips.nth(0)).toHaveText("How does Meridian relate to the platform team?");
  await expect(chips.nth(1)).toHaveText("What does the workspace say about Rollout?");
  // The hover text says where it came from and that it was checked.
  await expect(chips.nth(0)).toHaveAttribute(
    "title",
    "From the knowledge graph — 2 passages in this workspace can answer it.",
  );

  // Seeding, not sending: the draft fills and the transcript does not grow.
  await chips.nth(0).click();
  await expect(composer(page)).toHaveValue(
    "How does Meridian relate to the platform team?",
  );
  await expect(page.locator(".message")).toHaveCount(2);

  // And APPENDING rather than clobbering — losing typed work to a stray chip
  // click is the failure mode that would get the feature turned off.
  await chips.nth(1).click();
  await expect(composer(page)).toHaveValue(
    "How does Meridian relate to the platform team?\nWhat does the workspace say about Rollout?",
  );
  await page.screenshot({ path: "test-results/followup-chips.png", fullPage: true });

  await composer(page).fill("");
  await page.unroute("**/api/conversations/*/messages*");
  await deleteThread(page, "Audit the Meridian rollout for follow-ups.");
  expect(errors).toEqual([]);
});
