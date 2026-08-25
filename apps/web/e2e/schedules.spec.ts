import { expect, test, type Page } from "@playwright/test";
import { openView } from "./shell";

/**
 * The Schedules view, driven the way a user drives it: describe the schedule
 * in English, read the cron it compiled to and when it would actually fire,
 * and only then save.
 *
 * "every monday at 9am" is a shape the scripted (offline) compiler resolves
 * deterministically, so the compile is real — a round trip through
 * `POST /api/crons/compile-schedule` — and not a stub in the page. What the
 * chip shows is exactly what submit posts; the assertion pins the five-field
 * shape rather than one expression so the spec does not re-litigate the
 * compiler's output, which tests on the API side already pin.
 *
 * This spec creates one automation and deletes it: the suite shares one
 * workspace and runs in file order.
 */

/** Fail loudly on console errors: a React crash still leaves the DOM queryable.
 *
 * One expected entry is ignored: the ticker probe (`workflowSchedulingEnabled`)
 * POSTs /api/workflows/tick and reads a 503 as "scheduling is not configured"
 * — the DESIGNED answer on a dev/e2e server with no WORKFLOW_CRON_SECRET, and
 * exactly what lets this page say "nothing will fire it" honestly. The browser
 * still logs the failed resource; that log is the probe working. */
function watchForErrors(page: Page): string[] {
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(String(error)));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    if (/503 \(Service Unavailable\)/.test(message.text())) return;
    failures.push(message.text());
  });
  return failures;
}

const NAME = "English Monday Digest";

test("an automation is described in English, compiled, saved, and deleted", async ({
  page,
}) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await openView(page, "Automations", "Schedules");

  await page.getByRole("button", { name: "New automation" }).click();
  const form = page.locator(".cron-form");
  await form.getByPlaceholder("Morning digest").fill(NAME);

  // The schedule row takes a sentence, not an expression.
  await form.getByLabel("Describe the schedule").fill("every monday at 9am");
  await form.getByRole("button", { name: "Compile" }).click();

  // The compiled chip is the receipt: a five-field cron expression, verbatim
  // what submit will post, with the humane check — when it next fires — beside.
  const chip = form.locator('[aria-label="Compiled schedule"]');
  await expect(chip).toBeVisible();
  await expect(chip).toHaveText(/^\S+ \S+ \S+ \S+ \S+$/);
  await expect(form.locator(".cron-next-fires")).toContainText("Next:");

  // Role-scoped, not getByLabel: the Kind select's accessible name contains
  // the word "prompt" too, and a label lookup resolves to both.
  await form.getByRole("textbox", { name: "Prompt" }).fill("Summarise the week ahead.");
  await form.getByRole("button", { name: "Create automation" }).click();

  // Saving lands on the detail view and puts the row in the sidebar list.
  await expect(page.getByRole("heading", { name: NAME })).toBeVisible();
  await expect(page.locator(".workflow-item").filter({ hasText: NAME })).toHaveCount(1);

  // Deleting asks through window.confirm; arm the answer before the click.
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: `Delete ${NAME}` }).click();
  await expect(page.locator(".workflow-item").filter({ hasText: NAME })).toHaveCount(0);

  expect(errors).toEqual([]);
});
