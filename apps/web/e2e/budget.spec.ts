import { expect, test, type Page } from "@playwright/test";
import { STORAGE_STATE } from "./credentials";
import { createFromMenu, openSettings, openView } from "./shell";

/**
 * The spend ceiling, from the wall a user hits to the number that releases them.
 *
 * The park is real, not staged. `budget.exceeds` stops at the boundary — `>=`,
 * not `>` — so a workspace ceiling of $0 stops the very next model call, which
 * is the cheapest way to reach a state that otherwise needs a real invoice to
 * produce. Everything after that is the product: a chat turn that says why it
 * stopped, a graph that does not claim to be waiting for a decision nobody can
 * give, and the raise that releases both.
 *
 * The e2e API runs the scripted provider, which records no `model_usage` rows
 * at all — so the window really does contain zero priced calls, and the panel
 * has to say "Not priced" rather than "$0.00 of $0.00". That is the day-one
 * state of every deployment that has not configured MODEL_PRICES, and it is
 * asserted rather than admired.
 *
 * This spec sets a workspace-wide ceiling, saves a workflow and parks a real
 * write — all of which every later spec in the file order would inherit. Each
 * test undoes its own, and `sweep` in an afterAll undoes them again for the
 * runs where an assertion failed before the test got that far.
 */

const API = "http://127.0.0.1:8010";

type Ceiling = {
  window_hours: number;
  usd_per_window: number | null;
  tokens_per_window: number | null;
};

/** No limit of either kind — the state a fresh workspace is in. */
const UNLIMITED: Ceiling = {
  window_hours: 24,
  usd_per_window: null,
  tokens_per_window: null,
};

/**
 * Set the ceiling through the API rather than the UI.
 *
 * Arranging a precondition is not the thing under test, and driving the Admin
 * form to reach the chat screen would make every chat assertion depend on the
 * form still working. Done from inside the page so the session cookie and the
 * CSRF token are the ones the browser already holds.
 */
async function putCeiling(page: Page, ceiling: Ceiling) {
  const status = await page.evaluate(
    async ({ api, body }) => {
      const me = await fetch(`${api}/api/auth/me`, { credentials: "include" }).then(
        (response) => response.json(),
      );
      const response = await fetch(`${api}/api/admin/budget`, {
        method: "PUT",
        credentials: "include",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": me.csrf_token },
        body: JSON.stringify(body),
      });
      return response.status;
    },
    { api: API, body: ceiling },
  );
  expect(status).toBe(200);
}

/**
 * Fail loudly on console errors: a React crash still leaves the DOM queryable.
 *
 * `ignore` exists for exactly one line below. Showing the not-an-owner state
 * means serving a real 403, and Chrome logs every 403 response as a console
 * error — so that one is the test's own doing and not a defect it is watching
 * for. Anything else still fails the run.
 */
function watchForErrors(page: Page, ignore?: RegExp): string[] {
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(String(error)));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    if (ignore && ignore.test(message.text())) return;
    failures.push(message.text());
  });
  return failures;
}

/**
 * Undo everything this file put in the shared workspace, whatever happened.
 *
 * The ceiling is the obvious one — a stray $0 would park every run in the rest
 * of the suite — but the workflow test below also saves a workflow, parks a
 * *real* write on it and opens a conversation, and a failed assertion anywhere
 * in the middle skips the inline cleanup at the end of that test. None of that
 * residue is inert: a parked `create_document` is workspace-wide, so it comes
 * back from `GET /api/documents-pending` and lands a second card in the
 * Documents view of a spec three files later. That is exactly how one failure
 * here became three, and the reason this sweeps rather than only clears.
 *
 * Driven through the API, and every step tolerates a workspace that is already
 * clean, so the happy path — where each test tidies up after itself — runs this
 * and finds nothing to do.
 */
async function sweep(page: Page) {
  await page.evaluate(async (api) => {
    const me = await fetch(`${api}/api/auth/me`, { credentials: "include" }).then(
      (response) => response.json(),
    );
    const headers = {
      "Content-Type": "application/json",
      "X-CSRF-Token": me.csrf_token,
    };
    const get = (path: string) =>
      fetch(`${api}${path}`, { credentials: "include" }).then((response) =>
        response.ok ? response.json() : [],
      );

    // Only this file's own proposal, by the title the script gives it — a sweep
    // that denied every parked write in the workspace would silently undo a
    // later spec's arrangement instead of cleaning up after this one.
    //
    // Decided before the workflow is deleted: a denial ends the run that holds
    // the proposal, and a denial writes nothing.
    const parked = await get("/api/documents-pending");
    for (const edit of parked.filter(
      (row: { title: string }) => row.title === "Workflow Run Summary",
    )) {
      await fetch(`${api}/api/agent-tool-calls/${edit.id}/decision`, {
        method: "POST",
        credentials: "include",
        headers: { ...headers, "Idempotency-Key": `sweep-${edit.id}` },
        body: JSON.stringify({ decision: "denied" }),
      });
    }
    for (const workflow of await get("/api/workflows")) {
      if (workflow.name !== "Scripted workflow") continue;
      await fetch(`${api}/api/workflows/${workflow.id}`, {
        method: "DELETE",
        credentials: "include",
        headers,
      });
    }
    for (const conversation of await get("/api/conversations")) {
      if (conversation.title !== "Workflow: Scripted workflow") continue;
      // Idempotency-Key is required by this route and not by the two above,
      // and a missing one is a 422 the sweep would swallow — which is how a
      // half-working sweep leaves exactly one of the three things behind.
      await fetch(`${api}/api/conversations/${conversation.id}`, {
        method: "DELETE",
        credentials: "include",
        headers: { ...headers, "Idempotency-Key": `sweep-${conversation.id}` },
      });
    }
  }, API);
}

test.afterAll(async ({ browser }) => {
  const context = await browser.newContext({ storageState: STORAGE_STATE });
  const page = await context.newPage();
  await page.goto("/");
  await putCeiling(page, UNLIMITED);
  await sweep(page);
  await context.close();
});

test("day one: no ceiling configured reads as a choice, not a broken panel", async ({
  page,
}) => {
  await page.goto("/");
  await openSettings(page, "Admin");
  const panel = page.locator(".budget-panel");
  await expect(panel).toBeVisible();

  // Unset is unlimited and stays unlimited — a deployment upgrading into this
  // release must not discover a cap nobody chose.
  await expect(panel.getByText("No spend ceiling — this workspace is unlimited")).toBeVisible();
  await expect(panel.getByText("No limit").first()).toBeVisible();
  // With no rates configured and nothing spent, there is no money to print.
  await expect(panel).not.toContainText("$0.00");
  await expect(panel.getByRole("spinbutton", { name: "Dollar ceiling" })).toHaveValue("");
  await expect(panel.getByRole("spinbutton", { name: "Token ceiling" })).toHaveValue("");
  await expect(panel.getByRole("spinbutton", { name: "Window (hours)" })).toHaveValue("24");

  await page.screenshot({ path: "test-results/budget-admin-dayone.png", fullPage: true });

  await page.emulateMedia({ colorScheme: "dark" });
  await expect(panel.getByText("No spend ceiling — this workspace is unlimited")).toBeVisible();
  await page.screenshot({
    path: "test-results/budget-admin-dayone-dark.png",
    fullPage: true,
  });
  await page.emulateMedia({ colorScheme: "light" });
});

test("a chat turn stopped by the ceiling explains itself, and the raise releases it", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const errors = watchForErrors(page, /403 \(Forbidden\)/);
  await page.goto("/");
  await putCeiling(page, { ...UNLIMITED, usd_per_window: 0 });

  await page.getByRole("button", { name: "New thread" }).click();
  await page.locator(".composer textarea").fill("What do these sources say?");
  await page.getByRole("button", { name: "Send message" }).click();

  const hold = page.locator(".budget-hold");
  await expect(hold).toBeVisible({ timeout: 60_000 });
  await expect(hold).toContainText("Paused — this workspace reached its spend limit");
  // Parked, not failed, and it says which limit over which window.
  await expect(hold).toContainText("picks up exactly where it stopped");
  // The figures panel: a term list, so the label is a <dt> rather than prose.
  await expect(hold.locator("dt", { hasText: /^Ceiling$/ })).toBeVisible();
  await expect(hold.locator("dt", { hasText: /^Window$/ })).toBeVisible();
  // Nothing here is decidable: a budget park writes no AgentToolCall, so an
  // approve/deny pair would be two buttons for a record that does not exist.
  await expect(hold.getByRole("button", { name: "Approve" })).toHaveCount(0);
  await expect(hold.getByRole("button", { name: "Deny" })).toHaveCount(0);
  // The scripted provider prices nothing, so the spend must not be a figure.
  await expect(hold.getByText("Not priced")).toBeVisible();
  await expect(hold).not.toContainText("$0.00 spent");

  await page.screenshot({ path: "test-results/budget-chat-parked.png", fullPage: true });
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(hold).toBeVisible();
  await page.screenshot({
    path: "test-results/budget-chat-parked-dark.png",
    fullPage: true,
  });
  await page.emulateMedia({ colorScheme: "light" });

  // --- The same park, seen by an owner on the Admin screen ----------------
  // Raising a limit that releases nothing is indistinguishable from raising one
  // that releases six automations, so the ceiling names what it is holding.
  await openSettings(page, "Admin");
  const panel = page.locator(".budget-panel");
  // Scoped to the headline: the form's lead says the same numbers back, which
  // is the point of putting them next to each other and makes a bare text
  // match ambiguous.
  await expect(panel.locator(".usage-notice strong")).toHaveText("$0.00 per 24 hours");
  await expect(panel.getByText("1 parked")).toBeVisible();
  await expect(panel.locator(".budget-parked")).toContainText("raising the ceiling");
  await page.screenshot({ path: "test-results/budget-admin-parked.png", fullPage: true });

  // And the park survives leaving the view: it is run state, not view state.
  await openView(page, "Chat");
  await expect(hold).toBeVisible();

  // --- What a member who is not an owner meets ----------------------------
  // `PUT /api/admin/budget` is require_owner, which answers 403 "Owner role
  // required". The control asks the route rather than guessing from a role
  // string, so serving that answer is enough to see the state.
  const forbid = async (route: import("@playwright/test").Route) => {
    if (route.request().method() !== "GET") return route.continue();
    return route.fulfill({ status: 403, json: { detail: "Owner role required" } });
  };
  await page.route("**/api/admin/budget", forbid);
  await hold.getByRole("button", { name: "Raise the spend limit" }).click();
  const menu = page.getByRole("group", { name: "Spend limit" });
  await expect(menu).toContainText("Only an owner of this workspace can change the spend limit");
  await expect(menu.getByRole("spinbutton", { name: "Dollar ceiling" })).toHaveCount(0);
  await page.screenshot({
    path: "test-results/budget-chat-not-owner.png",
    fullPage: true,
  });
  await page.keyboard.press("Escape");
  await expect(menu).toHaveCount(0);
  await page.unroute("**/api/admin/budget", forbid);

  // --- The way forward, from where the wall is ----------------------------
  await hold.getByRole("button", { name: "Raise the spend limit" }).click();
  const owned = page.getByRole("group", { name: "Spend limit" });
  await expect(owned.getByRole("spinbutton", { name: "Dollar ceiling" })).toHaveValue("0");
  await page.screenshot({ path: "test-results/budget-chat-raise.png", fullPage: true });
  await owned.getByRole("spinbutton", { name: "Dollar ceiling" }).fill("25");
  await owned.getByRole("button", { name: "Save limit" }).click();

  // The raise *is* the release: the run resumes on the same open stream and
  // finishes, with nobody reloading anything.
  await expect(hold).toHaveCount(0, { timeout: 60_000 });
  await expect(page.locator(".message.assistant").first()).toBeVisible({
    timeout: 60_000,
  });
  await page.screenshot({ path: "test-results/budget-chat-released.png", fullPage: true });

  // --- Put the shared workspace back --------------------------------------
  await putCeiling(page, UNLIMITED);
  // The thread this test opened is the most recently touched one, so it is the
  // first in the list — named by its own prompt once the run finished.
  const threads = page.locator(".thread");
  const before = await threads.count();
  page.once("dialog", (dialog) => dialog.accept());
  await threads.first().getByRole("button", { name: /^Delete / }).click();
  await expect(threads).toHaveCount(before - 1);

  expect(errors).toEqual([]);
});

test("a workflow held by the ceiling does not claim to be waiting for a decision", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const errors = watchForErrors(page);
  await page.goto("/");
  await putCeiling(page, { ...UNLIMITED, usd_per_window: 0 });

  await createFromMenu(page, "Workflow");
  await page
    .getByRole("textbox", { name: "Describe the automation" })
    .fill("Every Monday, pull the open pull requests, summarise them, and post to Slack.");
  await page.getByRole("button", { name: "Compile" }).click();
  const preview = page.locator(".workflow-preview");
  await expect(preview).toBeVisible({ timeout: 30_000 });
  await preview.getByRole("button", { name: "Save workflow" }).click();
  await expect(page.getByRole("heading", { name: "Scripted workflow" })).toBeVisible();
  await page.getByRole("button", { name: "Run now" }).click();

  // The tool node runs; the assistant node needs a model call, and that is the
  // statement the ceiling is checked before.
  const banner = page.locator(".workflow-run-banner");
  await expect(banner).toContainText("Held by the spend limit", { timeout: 60_000 });
  await expect(banner).not.toContainText("Waiting for approval");
  await expect(page.locator(".workflow-node.agent .workflow-node-status")).toContainText(
    "Held by the spend limit",
  );
  // The inbox is the other place this could lie: two different stops land in
  // it, and only one of them is waiting for a person.
  await expect(page.locator(".workflow-inbox-item")).toContainText(
    "Held by the spend limit",
  );
  // No approval card, because there is no approval: the node parked before it
  // proposed anything, so the card that would offer Approve/Deny is absent and
  // the hold panel is in its place.
  await expect(page.locator(".workflow-approval")).toHaveCount(0);
  await expect(page.locator(".budget-hold")).toBeVisible();

  await page.screenshot({
    path: "test-results/budget-workflow-held.png",
    fullPage: true,
  });
  await page.locator(".workflow-graph-pane").screenshot({
    path: "test-results/budget-workflow-held-graph.png",
  });
  // The pane scrolls, so the card inside the parked node gets its own shot:
  // the panel is only useful if its way forward is reachable where it sits.
  await page.locator(".budget-hold").screenshot({
    path: "test-results/budget-workflow-held-card.png",
  });

  // Raising it from here releases the graph, which then carries on to the write
  // it was always going to park on — a real approval, with a card.
  await page.locator(".budget-hold").getByRole("button", { name: "Raise the spend limit" }).click();
  const menu = page.getByRole("group", { name: "Spend limit" });
  await menu.getByRole("spinbutton", { name: "Dollar ceiling" }).fill("25");
  await menu.getByRole("button", { name: "Save limit" }).click();
  await expect(page.locator(".workflow-approval")).toBeVisible({ timeout: 60_000 });
  await expect(banner).toContainText("Waiting for approval");
  await page.screenshot({
    path: "test-results/budget-workflow-released.png",
    fullPage: true,
  });

  // Denied, so no document is written and this spec has less to clean up.
  await page.locator(".workflow-approval").getByRole("button", { name: "Deny" }).click();
  await expect(page.locator(".workflow-approval")).toHaveCount(0, { timeout: 60_000 });

  // --- Put the shared workspace back --------------------------------------
  await putCeiling(page, UNLIMITED);
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: `Delete Scripted workflow` }).click();
  await expect(page.locator(".workflow-item")).toHaveCount(0);

  await page.reload();
  const thread = page.getByRole("button", { name: "Delete Workflow: Scripted workflow" });
  page.once("dialog", (dialog) => dialog.accept());
  await thread.click();
  await expect(thread).toHaveCount(0);

  expect(errors).toEqual([]);
});
