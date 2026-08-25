import { expect, test, type Page } from "@playwright/test";
import { newThread } from "./shell";

/**
 * The harness features a user can reach from the composer: a blocking
 * question (`ask_user`), a delegated sub-agent, and the guardian approval
 * mode's fail-closed posture.
 *
 * Everything but the model is real — the scripted provider proposes the
 * calls, and the loop, the policy engine, the delegation child loop, the
 * decision endpoint's answer amendment, and the rendering are the product's
 * own. Each assertion is on rendered *outcome* text that only a working
 * end-to-end chain can produce: the answer typed into the card coming back
 * out of the executor, and the child agent's words surfacing in the parent's
 * card.
 */

function watchForErrors(page: Page): string[] {
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") failures.push(message.text());
  });
  return failures;
}

const composer = (page: Page) => page.getByRole("textbox", { name: "Message" });

test("an ask_user question renders its options, takes a typed answer, and hands it back", async ({
  page,
}) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await newThread(page);

  await composer(page).fill("Which region should we deploy to today?");
  await composer(page).press("Enter");

  const card = page.locator(".tool-card", { hasText: "ask_user" }).first();
  await expect(card).toBeVisible({ timeout: 20_000 });
  await expect(card.getByText("Needs approval")).toBeVisible();
  // The question and its enumerated options, as separate lines — the
  // pre-wrap fix is what keeps "- us-east" from collapsing into the question.
  await expect(card).toContainText("Which region should we deploy to?");
  await expect(card).toContainText("- us-east");
  await expect(card).toContainText("- eu-west");
  // No standing grant for a question addressed to a person.
  await expect(card.getByText(/Always allow/)).toHaveCount(0);

  const answerBox = card.getByRole("textbox", {
    name: "Answer the assistant's question",
  });
  await answerBox.fill("us-east");
  // Typing an answer relabels the primary action: this is an answer, not a
  // rubber stamp.
  await card.getByRole("button", { name: "Answer" }).click();

  await expect(page.getByText("Deploying as instructed.")).toBeVisible({
    timeout: 20_000,
  });
  // The proof of the whole round trip: the typed words came back OUT of the
  // executor, through the amendment channel, into the call's recorded result.
  await card.locator(".tool-card-head").click();
  await expect(card).toContainText("The user answered");
  await expect(card).toContainText("us-east");
  await page.screenshot({ path: "test-results/ask-user-answered.png", fullPage: true });
  expect(errors).toEqual([]);
});

test("a delegate call runs a read-only child agent and surfaces its findings", async ({
  page,
}) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await newThread(page);

  await composer(page).fill("Delegate the Atlas research to a sub-agent.");
  await composer(page).press("Enter");

  // delegate is read-only, so no approval card — the call runs unattended
  // under the default mode and completes with the child's answer inside.
  const card = page.locator(".tool-card", { hasText: "delegate" }).first();
  await expect(card).toBeVisible({ timeout: 20_000 });
  await expect(
    page.getByText("Per the sub-agent, Atlas is fully covered."),
  ).toBeVisible({ timeout: 20_000 });

  await card.locator(".tool-card-head").click();
  // The child's own words, proving the nested loop actually ran a second
  // scripted turn and its answer crossed back through the tool result.
  await expect(card).toContainText("Atlas is the demo corpus");
  await expect(card).toContainText("Research partner");
  await page.screenshot({ path: "test-results/delegate-answered.png", fullPage: true });
  expect(errors).toEqual([]);
});

test("guardian mode is selectable and fails closed to a human park without a reviewer", async ({
  page,
}) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await newThread(page);

  const modeTrigger = page.getByRole("button", { name: /^Approval mode:/ });
  await modeTrigger.click();
  await page
    .getByRole("group", { name: "Approval mode" })
    .getByRole("button", { name: /^Guardian auto-approve/ })
    .click();
  await expect(modeTrigger).toHaveAccessibleName(
    "Approval mode: Guardian auto-approve",
  );
  // Guardian is a bypass in the honest sense the banner tracks: a write CAN
  // run without a person seeing it first, so the indicator must be up.
  await expect(page.locator(".bypass-banner")).toBeVisible();

  // The scripted deployment has no reviewer model, and the guardian's whole
  // contract is fail-closed: the write parks for a person, exactly as
  // ask_writes would have parked it.
  await composer(page).fill("Track the guarded checklist for me.");
  await composer(page).press("Enter");

  const card = page.locator(".tool-card", { hasText: "add_todo" }).first();
  await expect(card.getByText("Needs approval")).toBeVisible({ timeout: 20_000 });
  await card.getByRole("button", { name: "Deny" }).click();
  await expect(page.getByText("Understood — not tracking that.")).toBeVisible({
    timeout: 20_000,
  });

  // Leave the thread in the default mode so no later spec inherits a bypass.
  await modeTrigger.click();
  await page
    .getByRole("group", { name: "Approval mode" })
    .getByRole("button", { name: /^Ask before writes/ })
    .click();
  await expect(modeTrigger).toHaveAccessibleName(
    "Approval mode: Ask before writes",
  );
  expect(errors).toEqual([]);
});
