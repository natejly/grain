import { expect, test, type Page } from "@playwright/test";
import { newThread, openView } from "./shell";

/**
 * Todo lists, and the approval mode that governs a thread.
 *
 * Both features are only worth anything if what they claim is *visible*, so
 * these assert rendered state rather than element presence, and screenshot what
 * they claim so it can be checked by eye:
 *
 *  - a list is checkable in chat, not only on a page of its own, and a tick
 *    survives a reload — a checklist that forgets is not a checklist;
 *  - a write parks under `ask_writes` and does *not* under `auto_writes`, which
 *    is the only way to show the mode changed behaviour rather than a label;
 *  - the indicator is up the whole time the bypass is on, says which thread it
 *    governs, and lists what it let through;
 *  - a denied tool still reads as denied under the bypass — the bypass is
 *    permission to skip asking, not permission to overrule a refusal.
 *
 * The model is scripted (`agent-script.json`); everything else is the real
 * loop, the real policy resolution and the real endpoints. Each test deletes
 * the list it made.
 */

const LIST = "Launch checklist";

/** Fail loudly on console errors: a React crash still leaves the DOM queryable. */
function watchForErrors(page: Page): string[] {
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") failures.push(message.text());
  });
  return failures;
}

const composer = (page: Page) => page.getByRole("textbox", { name: "Message" });

const listNamed = (page: Page, name: string) =>
  page.locator(".todo-list", { hasText: name }).first();

const item = (page: Page, title: string) =>
  page.locator(".todo-item", { hasText: title }).first();

async function createList(page: Page, name: string) {
  await openView(page, "Library", /^Boards & todos/);
  // One create form for both shapes: "List" births the one-column shape.
  await page.getByRole("combobox", { name: "Create as" }).selectOption("list");
  await page.getByRole("textbox", { name: "List name" }).fill(name);
  await page.locator(".board-new").getByRole("button", { name: /Create/ }).click();
  await expect(page.getByRole("heading", { name })).toBeVisible();
}

/**
 * Delete a list. Confirm()-gated — the same as a board: the two shapes are one
 * object, and share one deletion gate.
 */
async function deleteList(page: Page, name: string) {
  await openView(page, "Library", /^Boards & todos/);
  page.once("dialog", (dialog) => dialog.accept());
  await listNamed(page, name).getByRole("button", { name: `Delete ${name}` }).click();
  await expect(page.getByRole("heading", { name })).toHaveCount(0);
}

/**
 * Write or revoke a workspace tool policy straight through the API.
 *
 * There is no UI for standing tool grants — the only one the product offers is
 * the "always allow" checkbox on an approval card, and there is deliberately no
 * "always deny" beside it. So the one precondition this spec cannot arrange by
 * clicking is arranged here, from inside the page, with the browser's own
 * session cookie and CSRF token. It is revoked in the same test that sets it: a
 * workspace-wide deny left behind is a lie every later run inherits.
 */
async function writeToolPolicy(
  page: Page,
  tool: string,
  policy: "deny" | null,
): Promise<void> {
  const failure = await page.evaluate(
    async ([toolName, verdict]) => {
      // The API this dev server was told to talk to; the page itself is on 3010.
      const base = "http://127.0.0.1:8010";
      const session = await fetch(`${base}/api/auth/me`, { credentials: "include" });
      if (!session.ok) return `auth/me said ${session.status}`;
      const { csrf_token, workspace_id } = await session.json();
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf_token,
        "X-Workspace-Id": workspace_id,
      };
      const response = verdict
        ? await fetch(`${base}/api/tool-policies`, {
            method: "PUT",
            credentials: "include",
            headers,
            body: JSON.stringify({ tool_name: toolName, policy: verdict, scope: "chat" }),
          })
        : await fetch(`${base}/api/tool-policies/${toolName}?scope=chat`, {
            method: "DELETE",
            credentials: "include",
            headers,
          });
      return response.ok ? "" : `${response.status} ${await response.text()}`;
    },
    [tool, policy] as const,
  );
  expect(failure, `tool policy ${policy ?? "revoke"} for ${tool}`).toBe("");
}

test("a todo list is checkable, and the tick survives a reload", async ({ page }) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await createList(page, "Docs checklist");

  const list = listNamed(page, "Docs checklist");
  // An empty list says so, rather than reporting "0 of 0 done".
  await expect(list.getByText("Nothing on this list yet")).toBeVisible();

  await list.getByRole("button", { name: "Add item" }).click();
  await page.getByRole("textbox", { name: "Add item" }).fill("Ship the docs");
  await list.getByRole("button", { name: "Add", exact: true }).click();
  await expect(list.getByText("0 of 1 done")).toBeVisible();

  const shipTheDocs = item(page, "Ship the docs").getByRole("checkbox");
  await expect(shipTheDocs).not.toBeChecked();
  await shipTheDocs.check();
  await expect(list.getByText("1 of 1 done")).toBeVisible();
  // Ticked is a *rendered* state, not only a checkbox attribute: the title is
  // struck through, which is what a reader actually sees.
  await expect(item(page, "Ship the docs").locator(".todo-item-title")).toHaveCSS(
    "text-decoration-line",
    "line-through",
  );
  await list.screenshot({ path: "test-results/todo-list.png" });

  // The tick is on the server, not in this tab.
  await page.reload();
  await openView(page, "Library", /^Boards & todos/);
  await expect(item(page, "Ship the docs").getByRole("checkbox")).toBeChecked();

  // No teleport: grow a second column and the object stays in this one
  // listing — its glyph flips to a board, and the notice toast says so. The
  // page used to split boards and lists across two tabs, which made exactly
  // this action silently move the checklist to the other page.
  const entry = page.locator(".board", { hasText: "Docs checklist" }).first();
  await expect(entry).toHaveAttribute("data-shape", "list");
  await entry.getByRole("button", { name: "Add column" }).click();
  await entry.getByRole("textbox", { name: "Column name" }).fill("Done");
  await entry.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.locator(".notice-toast")).toContainText("Now showing as a board");
  await expect(entry).toHaveAttribute("data-shape", "board");
  // Same place, same items — the tick survived the graduation.
  await expect(entry.getByRole("heading", { name: "Docs checklist" })).toBeVisible();
  await expect(entry.getByText("Ship the docs")).toBeVisible();
  await entry.screenshot({ path: "test-results/todo-graduated.png" });

  // Deleting it is confirm()-gated, as it was while it was still a list —
  // both shapes meet the same gate.
  page.once("dialog", (dialog) => dialog.accept());
  await entry.getByRole("button", { name: "Delete Docs checklist" }).click();
  await expect(page.getByRole("heading", { name: "Docs checklist" })).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("a write parks under ask_writes, runs under the bypass, and a deny survives it", async ({
  page,
}) => {
  const errors = watchForErrors(page);
  await page.goto("/");
  await createList(page, LIST);

  await newThread(page);

  // A fresh thread asks before it writes, and says so.
  const modeTrigger = page.getByRole("button", { name: /^Approval mode:/ });
  await expect(modeTrigger).toHaveAccessibleName("Approval mode: Ask before writes");
  await expect(page.locator(".bypass-banner")).toHaveCount(0);

  // ---- ask_writes: the write parks ----------------------------------------
  await composer(page).fill("Track the launch checklist.");
  await composer(page).press("Enter");

  const firstCard = page.locator(".tool-card", { hasText: "add_todo" }).first();
  await expect(firstCard).toBeVisible({ timeout: 20_000 });
  await expect(firstCard.getByText("Needs approval")).toBeVisible();
  await expect(firstCard.getByText("Freeze the release branch")).toBeVisible();
  await firstCard.getByRole("button", { name: "Approve" }).click();

  const secondCard = page.locator(".tool-card", { hasText: "Run the migrations" });
  await expect(secondCard.getByText("Needs approval")).toBeVisible({ timeout: 20_000 });
  await secondCard.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("Tracking two things on the Launch checklist.")).toBeVisible({
    timeout: 20_000,
  });

  // The list, in the conversation that made it — the point of the inline
  // mount. One checklist for the turn, under the last call that touched the
  // list, rather than one per call showing three states of the same thing.
  const inline = page.locator(".todo-list.compact");
  await expect(inline).toHaveCount(1);
  await expect(inline).toContainText("The assistant added to this list.");
  await expect(inline).toContainText("0 of 2 done");
  await expect(inline.getByText("Freeze the release branch")).toBeVisible();
  await page.screenshot({ path: "test-results/todo-in-chat.png", fullPage: true });

  // ---- auto_writes: the same class of write does not park -----------------
  await modeTrigger.click();
  await page
    .getByRole("group", { name: "Approval mode" })
    .getByRole("button", { name: /^Allow all tool calls/ })
    .click();

  const banner = page.locator(".bypass-banner");
  // Nothing yet. `auto_writes` is the product's DEFAULT mode, so its banner is a
  // trail rather than a standing warning: one rendered on every thread in the
  // product would be furniture, and furniture is not read on the day it finally
  // has something to say. It appears below, once a call has actually gone
  // through unreviewed.
  await expect(banner).toHaveCount(0);
  await expect(modeTrigger).toHaveAccessibleName("Approval mode: Allow all tool calls");

  await composer(page).fill("Tick off the migrations.");
  await composer(page).press("Enter");
  await expect(page.getByText("Ticked off Run the migrations.")).toBeVisible({
    timeout: 20_000,
  });
  // The behavioural difference, asserted as an absence: the identical class of
  // write asked permission a moment ago and did not this time.
  await expect(page.getByText("Needs approval")).toHaveCount(0);

  const checkCard = page.locator(".tool-card", { hasText: "todo_check" });
  await expect(checkCard.getByText("Auto-approved")).toBeVisible({ timeout: 20_000 });
  // The agent ticking something off is the interesting case, so it is shown
  // where it happened, with the item actually ticked.
  const ticked = checkCard.locator(".todo-list.compact");
  await expect(ticked).toContainText("The assistant ticked an item off this list.");
  await expect(ticked).toContainText("1 of 2 done");
  await expect(
    ticked.locator(".todo-item", { hasText: "Run the migrations" }).getByRole("checkbox"),
  ).toBeChecked();

  // And now it says so — the trail arrives with the thing it has to report.
  await expect(banner).toBeVisible();
  await expect(banner).toContainText("Auto-approving writes in");
  await expect(banner).toContainText("1 call went through unreviewed");
  await expect(banner).toContainText("todo_check");
  await banner.getByRole("button", { name: /Show what ran/ }).click();
  await expect(banner.locator(".bypass-trail")).toContainText("todo_check");
  await page.screenshot({ path: "test-results/approval-bypass.png", fullPage: true });

  // Per conversation, and visibly so: a second thread is not bypassed.
  await newThread(page);
  await expect(page.locator(".bypass-banner")).toHaveCount(0);
  await expect(modeTrigger).toHaveAccessibleName("Approval mode: Ask before writes");

  // And it survives a reload of the thread that set it. A bypass that silently
  // lapses is as bad as one that is silently on: neither can be relied on.
  await page.reload();
  await page.getByRole("button", { name: "Track the launch checklist.", exact: false })
    .first()
    .click();
  await expect(page.locator(".bypass-banner")).toBeVisible();

  // ---- a deny is not cleared by the bypass --------------------------------
  await writeToolPolicy(page, "add_todo", "deny");
  await composer(page).fill("Add the smoke test to the checklist.");
  await composer(page).press("Enter");

  const denied = page.locator(".tool-card.denied", { hasText: "add_todo" });
  await expect(denied).toBeVisible({ timeout: 20_000 });
  await expect(denied.getByText("Denied — not run")).toBeVisible();
  // A refusal has to *look* refused in a thread where everything else sails
  // through, so the colour is measured rather than assumed.
  const refusedColour = await denied
    .locator(".tool-status.denied")
    .evaluate((node) => getComputedStyle(node).color);
  const autoColour = await checkCard
    .locator(".auto-approved")
    .evaluate((node) => getComputedStyle(node).color);
  expect(refusedColour).not.toBe(autoColour);
  await page.screenshot({ path: "test-results/approval-denied.png", fullPage: true });
  await writeToolPolicy(page, "add_todo", null);

  // Turning it off is one click from the warning itself.
  await banner.getByRole("button", { name: "Turn off" }).click();
  await expect(page.locator(".bypass-banner")).toHaveCount(0);
  await expect(modeTrigger).toHaveAccessibleName("Approval mode: Ask before writes");

  // The refusal was real: nothing was added, and the agent's tick is on the
  // server rather than only in the transcript that showed it.
  await openView(page, "Library", /^Boards & todos/);
  const list = listNamed(page, LIST);
  await expect(list).toContainText("1 of 2 done");
  await expect(list.getByText("Smoke-test checkout")).toHaveCount(0);
  await expect(item(page, "Run the migrations").getByRole("checkbox")).toBeChecked();

  await deleteList(page, LIST);
  expect(errors).toEqual([]);
});
