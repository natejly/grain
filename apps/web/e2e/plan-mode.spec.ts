import { expect, test, type Page } from "@playwright/test";
import { newThread } from "./shell";

/**
 * Plan mode and the composer's built-in slash commands (/plan, /btw).
 *
 * What has to be *visible* here, not merely wired:
 *
 *  - "/" summons the built-in commands beside the skills, and picking /plan
 *    flips the thread's mode control in place — no message is sent;
 *  - a planning turn ends on an approval card whose body IS the plan, with no
 *    "always allow" escape hatch (the server ignores it for this card, so
 *    offering it would promise a skip that cannot happen);
 *  - approving the plan lifts the mode — the control reads "Ask before writes"
 *    again once the turn settles — while denying it leaves the thread planning;
 *  - "/btw" records an aside: it renders as a note rather than a turn, starts
 *    no run, and survives a reload because it lives on the server.
 *
 * The model is scripted (`agent-script.json`, "plan the atlas rollout");
 * everything else — the mode's policy, the park, the decision endpoint's mode
 * restore — is the real loop.
 */

const composer = (page: Page) => page.getByRole("textbox", { name: "Message" });
const modeTrigger = (page: Page) => page.getByRole("button", { name: /^Approval mode:/ });

function watchForErrors(page: Page): string[] {
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") failures.push(message.text());
  });
  return failures;
}

/** Type "/plan" and pick it from the command picker. */
async function togglePlanMode(page: Page) {
  await composer(page).fill("/plan");
  const command = page
    .locator(".skill-picker")
    .getByRole("option", { name: /\/plan/ });
  await expect(command).toBeVisible();
  await command.click();
}

test("/plan gates the thread until the plan card is approved", async ({ page }) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await newThread(page);

  // Picking /plan flips the mode in place: no message sent, draft cleared.
  await expect(modeTrigger(page)).toHaveAccessibleName(
    "Approval mode: Ask before writes",
  );
  await togglePlanMode(page);
  await expect(modeTrigger(page)).toHaveAccessibleName("Approval mode: Plan first");
  await expect(composer(page)).toHaveValue("");

  // The planning turn parks on the plan itself, readable as prose.
  await composer(page).fill("Plan the atlas rollout.");
  await composer(page).press("Enter");
  const card = page.locator(".tool-card", { hasText: "exit_plan_mode" }).first();
  await expect(card).toBeVisible({ timeout: 20_000 });
  await expect(card.getByText("Needs approval")).toBeVisible();
  await expect(card.locator(".plan-proposal")).toContainText(
    "Freeze the release branch",
  );
  // No "always allow" on a plan review: approving THIS plan is the decision.
  await expect(card.getByText(/Always allow/)).toHaveCount(0);
  await page.screenshot({ path: "test-results/plan-review-card.png", fullPage: true });

  // Approval lifts the mode and the same turn carries on.
  await card.getByRole("button", { name: "Approve" }).click();
  await expect(
    page.getByText("Plan approved - starting with the release freeze."),
  ).toBeVisible({ timeout: 20_000 });
  await expect(modeTrigger(page)).toHaveAccessibleName(
    "Approval mode: Ask before writes",
  );

  expect(errors).toEqual([]);
});

test("denying the plan keeps the thread planning", async ({ page }) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await newThread(page);
  await togglePlanMode(page);

  await composer(page).fill("Plan the atlas rollout.");
  await composer(page).press("Enter");
  const card = page.locator(".tool-card", { hasText: "exit_plan_mode" }).first();
  await expect(card.getByText("Needs approval")).toBeVisible({ timeout: 20_000 });
  await card.getByRole("button", { name: "Deny" }).click();

  await expect(
    page.getByText("Understood - I'll revise the plan and propose again."),
  ).toBeVisible({ timeout: 20_000 });
  // Still planning: the denial changed the plan's fate, not the thread's mode.
  await expect(modeTrigger(page)).toHaveAccessibleName("Approval mode: Plan first");

  // And /plan is a toggle: picking it again turns plan mode off by hand.
  await togglePlanMode(page);
  await expect(modeTrigger(page)).toHaveAccessibleName(
    "Approval mode: Ask before writes",
  );

  expect(errors).toEqual([]);
});

test("/btw records an aside: a note, not a turn", async ({ page }) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await newThread(page);

  // The picker offers the command; picking it completes the token for typing.
  await composer(page).fill("/bt");
  await page
    .locator(".skill-picker")
    .getByRole("option", { name: /\/btw/ })
    .click();
  await expect(composer(page)).toHaveValue("/btw ");

  await composer(page).fill("/btw the deadline moved to Friday");
  await composer(page).press("Enter");

  // The note renders as an aside and nothing runs: no status, no cards, and
  // the composer is immediately usable rather than locked behind an active run.
  const aside = page.locator(".message.user.aside");
  await expect(aside).toBeVisible();
  await expect(aside).toContainText("the deadline moved to Friday");
  await expect(page.locator(".run-status")).toHaveCount(0);
  await expect(page.locator(".tool-card")).toHaveCount(0);
  await expect(composer(page)).toBeEnabled();
  await page.screenshot({ path: "test-results/btw-aside.png", fullPage: true });

  // A bare "/btw" is refused rather than recorded as an empty note. The first
  // Enter merely completes the token (the picker is open over "/btw"); the
  // second meets the empty-aside guard and leaves the draft standing.
  await composer(page).fill("/btw");
  await composer(page).press("Enter");
  await expect(composer(page)).toHaveValue("/btw ");
  await composer(page).press("Enter");
  await expect(composer(page)).toHaveValue("/btw ");
  await expect(page.locator(".message.user.aside")).toHaveCount(1);

  // The aside is on the server, not in this tab. An aside deliberately leaves
  // the thread untitled — it asked nothing — so the rail still says
  // "New conversation"; ours is the most recently touched one.
  await page.reload();
  await page.getByRole("button", { name: "New conversation" }).first().click();
  await expect(page.locator(".message.user.aside")).toContainText(
    "the deadline moved to Friday",
  );

  expect(errors).toEqual([]);
});
