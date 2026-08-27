import { expect, test, type Page } from "@playwright/test";
import { newThread } from "./shell";

/**
 * The composer's two promises about the words you have typed but not sent.
 *
 * Both were broken, and both failed *quietly*, which is what makes them worth a
 * spec rather than a comment:
 *
 *  - The draft belonged to the shell, not to the thread, so a half-typed
 *    message followed you to whatever thread you clicked next and the very next
 *    Enter sent it into the wrong conversation — on a shared thread, in front
 *    of the wrong people, with nothing on screen saying it had moved.
 *  - A send that never reached the API said nothing at all: the words stayed
 *    put, no message appeared, and the user could not tell a failed send from a
 *    keystroke that had been ignored. (`describeError` returns "" for an
 *    unreachable API on the grounds that the health banner covers it, but that
 *    banner polls on its own 15-second cadence and never fires for a single
 *    blipped request.)
 *
 * The titles are the prompts: the rail names a thread after the first thing
 * asked in it, so two distinctive prompts give two rows that can be told apart
 * without depending on where earlier specs left the rail.
 */

const composer = (page: Page) => page.getByRole("textbox", { name: "Message" });
const ALPHA = "draft owner alpha";
const BETA = "draft owner beta";
const row = (page: Page, title: string) => page.locator(".thread", { hasText: title }).first();

/** Open a fresh thread and ask something, so the rail row has a name to click. */
async function namedThread(page: Page, prompt: string) {
  await newThread(page);
  await composer(page).fill(prompt);
  await composer(page).press("Enter");
  // The prompt and its answer: the turn is over, so the next click is a
  // thread switch rather than a race with a run that is still starting.
  await expect(page.locator(".message")).toHaveCount(2);
  await expect(row(page, prompt)).toBeVisible();
}

test("a draft belongs to its thread, and is still there when you come back", async ({
  page,
}) => {
  await page.goto("/");
  await namedThread(page, ALPHA);
  await namedThread(page, BETA);

  // Standing in beta, type something meant for beta alone.
  const forBeta = "a private note meant only for beta";
  await composer(page).fill(forBeta);

  // Clicking alpha must not carry it along — this is the misdirection itself.
  await row(page, ALPHA).click();
  await expect(page.locator(".message").first()).toContainText(ALPHA);
  await expect(composer(page)).toHaveValue("");

  // Alpha keeps its own draft, side by side with beta's.
  const forAlpha = "a different note meant only for alpha";
  await composer(page).fill(forAlpha);

  await row(page, BETA).click();
  await expect(composer(page)).toHaveValue(forBeta);

  await row(page, ALPHA).click();
  await expect(composer(page)).toHaveValue(forAlpha);

  // Sending clears the thread that sent, and only that thread: a draft is not
  // worth isolating if a neighbour's send throws it away.
  await composer(page).fill("check the rollout date");
  await composer(page).press("Enter");
  await expect(composer(page)).toHaveValue("");
  await expect(page.locator(".message")).toHaveCount(4);

  await row(page, BETA).click();
  await expect(composer(page)).toHaveValue(forBeta);
});

test("a send that never reaches the API says so, and keeps the words", async ({ page }) => {
  await page.goto("/");
  await newThread(page);

  // A request that fails at the network level — the shape a blip, a dropped
  // connection, and a 500 stripped of its header by the CORS middleware all
  // arrive in. `/health` keeps answering throughout, so the banner never fires
  // and this toast is the only thing that can tell the user.
  await page.route("**/api/conversations/*/messages", (route) => route.abort("failed"));

  const words = "a paragraph I do not want to lose";
  await composer(page).fill(words);
  await composer(page).press("Enter");

  const toast = page.locator(".error-toast");
  await expect(toast).toBeVisible();
  await expect(toast).toContainText("Could not send message");
  // The words are still in the box, so the retry below is one keystroke.
  await expect(composer(page)).toHaveValue(words);
  await expect(page.locator(".message")).toHaveCount(0);

  // And the same Enter works once the network is back: the failure parked the
  // turn, it did not poison the thread.
  await page.unroute("**/api/conversations/*/messages");
  await composer(page).press("Enter");
  await expect(page.locator(".message").first()).toContainText(words);
});
