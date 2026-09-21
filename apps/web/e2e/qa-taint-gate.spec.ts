import { expect, test, type Page } from "@playwright/test";
import { existsSync } from "node:fs";
import path from "node:path";
import {
  newThread,
  openSettings,
  openThreadActions,
  openView,
  sendPrompt,
} from "./shell";

/**
 * The provenance gate, end to end: once a turn has read outside content, the
 * next call that CHANGES something waits for a person — and the card says why.
 *
 * What makes this testable at all. The gate arms on two classes by default,
 * `web_fetch` and `mcp_result` (`config.taint_gating_classes`), and neither is
 * reachable from the e2e server as shipped: the web-fetch allowlist is empty so
 * that family is not registered, and nothing seeds an MCP server. Fetching a
 * real page would also mean a real outbound request, which this suite does not
 * make. So the spec brings its own source — `mcp-taint-stub.py`, a genuine MCP
 * server the API talks to over a real subprocess, which answers offline with a
 * payload shaped like an instruction.
 *
 * The instruction-shaped payload is the scenario, not the mechanism. The gate
 * deliberately does not read it: it acts on WHERE the content came from, which
 * is why the same escalation happens for a perfectly innocent MCP result. What
 * is asserted here is the escalation and the reason on the card, never that
 * something classified the text.
 *
 * The thread is switched to "Act on its own" first, and that is load-bearing
 * rather than convenience. Under the harness default (ask before writes) the
 * write parks on the tool's OWN policy and carries no gate reason — the server
 * refuses to claim the gate stopped something the default would have stopped
 * anyway. The gate is only visible where it makes a difference.
 *
 * Self-cleaning: the write is DENIED (so no todo is left behind), the MCP
 * server is removed, and the thread is deleted.
 */

const ANSWER_TIMEOUT = 45_000;
test.describe.configure({ timeout: 180_000 });

const modeTrigger = (page: Page) => page.getByRole("button", { name: /^Approval mode:/ });

const SERVER = "e2e-taint";
const TOOL = "mcp__e2e-taint__briefing";

/**
 * The interpreter that runs the stub, resolved exactly the way
 * `playwright.config.ts` resolves the one that runs the API — the stub imports
 * the same `mcp` package the API does, so it has to be the same environment.
 * Absolute, because the API spawns it and only its own cwd would otherwise
 * decide what a relative path means.
 */
const PYTHON =
  process.env.E2E_PYTHON ??
  (existsSync(".venv/bin/python") ? path.resolve(".venv/bin/python") : "python3");
const STUB = path.join(__dirname, "mcp-taint-stub.py");

function watchForErrors(page: Page): string[] {
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") failures.push(message.text());
  });
  return failures;
}

test("outside content escalates the next write, and the card names why", async ({
  page,
}) => {
  const errors = watchForErrors(page);
  await page.goto("/");

  // Register the stub the way a person registers a local MCP server.
  await openSettings(page, "Connections", /MCP/);
  await expect(page.getByRole("heading", { name: "MCP servers" })).toBeVisible();
  await page.getByRole("button", { name: "Add server" }).click();
  const form = page.locator(".mcp-form");
  await form.getByLabel("Name").fill(SERVER);
  await form.getByLabel("Command").fill(PYTHON);
  await form.getByLabel("Arguments (one per line)").fill(STUB);
  await form.getByRole("button", { name: "Add server" }).click();

  const card = page.locator(".mcp-card", { hasText: SERVER });
  await expect(card).toBeVisible();
  // Discovery is a real subprocess round trip, so it gets a real margin.
  await card.getByRole("button", { name: "Refresh tools" }).click();
  await expect(card.locator(".mcp-tools")).toContainText("briefing", {
    timeout: 30_000,
  });
  await expect(card.locator(".mcp-error")).toHaveCount(0);

  // A thread that has been told to act on its own: without this the write
  // parks on its own default and the gate has nothing to add.
  await newThread(page);
  await expect(modeTrigger(page)).toHaveAccessibleName(
    "Approval mode: Ask before writes",
  );
  await modeTrigger(page).click();
  await page
    .getByRole("group", { name: "Approval mode" })
    .getByRole("button", { name: /^Act on its own/ })
    .click();
  await expect(modeTrigger(page)).toHaveAccessibleName("Approval mode: Act on its own");

  await sendPrompt(page, "Read the partner briefing.");

  // The read itself is NOT gated — nothing untrusted had entered the turn when
  // it was proposed, so "act on its own" meant exactly that. This is what makes
  // the next assertion about the gate rather than about the mode.
  const readCard = page.locator(".tool-card", { hasText: TOOL }).first();
  await expect(readCard).toBeVisible({ timeout: ANSWER_TIMEOUT });
  await expect(readCard.getByText("Needs approval")).toHaveCount(0);

  // The write after it stops, in a thread that was told not to stop.
  const writeCard = page.locator(".tool-card", { hasText: "add_todo" }).first();
  await expect(writeCard).toBeVisible({ timeout: ANSWER_TIMEOUT });
  await expect(writeCard.getByText("Needs approval")).toBeVisible();
  // The reason, above the diff it is waiting on, in the reader's words. This is
  // the provenance named on the card — not a guess about the payload's text.
  await expect(writeCard.locator(".tool-gate-note")).toHaveText(
    "This turn read a result from a connected MCP server. Because this call " +
      "changes something, it is waiting for you even though the thread is set " +
      "to act on its own.",
  );
  await page.screenshot({
    path: "test-results/taint-gate-card.png",
    fullPage: true,
  });

  // Deny it: the injected instruction gets the answer it deserves, and the
  // suite is left without a stray todo.
  await writeCard.getByRole("button", { name: "Deny" }).click();
  await expect(page.getByText("Understood — I added nothing.")).toBeVisible({
    timeout: ANSWER_TIMEOUT,
  });
  // A decided call carries no reason: "it is waiting for you" is past tense.
  await expect(page.locator(".tool-card", { hasText: "add_todo" }).first()
    .locator(".tool-gate-note")).toHaveCount(0);

  // Clean up: the server row first, then the thread. Removing a server asks
  // nothing — it is a registration, not data.
  await openSettings(page, "Connections", /MCP/);
  await page.getByRole("button", { name: `Remove ${SERVER}` }).click();
  await expect(page.locator(".mcp-card", { hasText: SERVER })).toHaveCount(0);

  await openView(page, "Chat");
  const row = page.locator(".thread", { hasText: "Read the partner briefing." }).first();
  await row.locator("button.thread-open").click();
  page.once("dialog", (dialog) => dialog.accept());
  await openThreadActions(page.locator(".thread.active"));
  await page
    .locator(".thread.active")
    .getByRole("button", { name: /^Delete / })
    .click();
  await expect(
    page.locator(".thread", { hasText: "Read the partner briefing." }),
  ).toHaveCount(0);

  expect(errors).toEqual([]);
});
