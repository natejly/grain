import { expect, test, type Page } from "@playwright/test";
import { createFromMenu, openView, rail } from "./shell";

/**
 * The whole workflow loop, driven the way a user drives it: describe an
 * automation in English, read the graph before it exists, save it, run it, and
 * answer what it stopped on.
 *
 * The park is real, not staged. Against the scripted provider the compiler
 * emits a fixed two-node graph — `search_sources` then an assistant step — and
 * the script entry matching "summarise these passages" makes that assistant
 * step call `create_document`, which is write-capable. So the run reaches a
 * write with nobody at the keyboard and parks, exactly as ADR 0007 says an
 * unattended run must, and the approval it parks on is an ordinary
 * `AgentToolCall` resolved through the ordinary decision endpoint.
 *
 * A refused compile cannot be provoked here — the scripted compiler always
 * emits a graph that validates — so the rendering of a rejection is pinned in
 * tests/workflow-format.test.ts instead.
 *
 * This spec creates a workflow, a document and a conversation. It deletes all
 * three: the suite shares one workspace and runs in file order.
 */

/** Fail loudly on console errors: a React crash still leaves the DOM queryable. */
function watchForErrors(page: Page): string[] {
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") failures.push(message.text());
  });
  return failures;
}

const WORKFLOW_NAME = "Scripted workflow";
const WORKFLOW_DOCUMENT = "Workflow Run Summary";
const WORKFLOW_CONVERSATION = `Workflow: ${WORKFLOW_NAME}`;

test("workflows: compile a sentence, review the graph, run it, and answer the park", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const errors = watchForErrors(page);
  await page.goto("/");

  // Create makes the thing rather than navigating to a list: a workflow's
  // "thing" is the sentence, so this opens the composer, like a dashboard's.
  await createFromMenu(page, "Workflow");
  await expect(page.locator(".workflow-author")).toBeVisible();
  await expect(rail(page).getByRole("button", { name: /^Workflows/ })).toHaveAttribute(
    "aria-current",
    "page",
  );

  await page
    .getByRole("textbox", { name: "Describe the automation" })
    .fill("Every Monday, pull the open pull requests, summarise them, and post to Slack.");
  await page.getByRole("button", { name: "Compile" }).click();

  // The review step: the graph is on screen and nothing has been saved.
  const preview = page.locator(".workflow-preview");
  await expect(preview).toBeVisible({ timeout: 30_000 });
  const nodes = preview.locator(".workflow-node");
  await expect(nodes).toHaveCount(2);
  // A tool node names its tool and its arguments, which is the whole point of
  // the tool/agent split — a reader can see exactly what it will call.
  await expect(preview.getByText("search_sources")).toBeVisible();
  await expect(preview.locator(".workflow-node.tool dt", { hasText: "query" })).toBeVisible();
  // An agent node cannot, and says so rather than looking like one that can.
  await expect(preview.locator(".workflow-node.agent")).toBeVisible();
  await expect(preview.getByText(/Chooses its own tools when it runs/)).toBeVisible();
  await page.locator(".workflow-author").screenshot({
    path: "test-results/workflow-preview.png",
  });

  await preview.getByRole("button", { name: "Save workflow" }).click();
  await expect(page.getByRole("heading", { name: WORKFLOW_NAME })).toBeVisible();
  // The compiled trigger is manual, so the schedule note must say so rather
  // than implying anything will fire it.
  await expect(page.locator(".workflow-notes")).toContainText("Runs when you start it");
  // And the graph contains an assistant step, so the panel must not claim to
  // know what it will do — the flag the API computes only sees tool nodes.
  await expect(page.locator(".workflow-notes")).toContainText(
    "cannot be read off the graph",
  );

  await page.getByRole("button", { name: "Run now" }).click();

  // The run reaches the write and stops. Nothing has been written yet.
  const approval = page.locator(".workflow-approval");
  await expect(approval).toBeVisible({ timeout: 60_000 });
  await expect(page.locator(".workflow-run-banner")).toContainText(
    "Waiting for approval",
  );
  await expect(
    page.locator(".workflow-node.agent .workflow-node-status"),
  ).toContainText("Parked for approval");
  // The card shows what the call *would* do — a proposal, not a receipt. That
  // preview is also why this cannot assert the document is absent by searching
  // the page: the proposal quotes the document it has not written.
  await expect(approval.locator(".workflow-approval-preview")).toContainText(
    WORKFLOW_DOCUMENT,
  );
  // Two shots: the whole shell, so the "Waiting for you" inbox and the run
  // banner can be looked at in context, and the graph pane close up.
  await page.screenshot({ path: "test-results/workflow-parked.png", fullPage: true });
  await page.locator(".workflow-graph-pane").screenshot({
    path: "test-results/workflow-parked-graph.png",
  });
  // And the same screen in the other theme. Every colour here comes from a
  // token, but "it used a token" and "it is readable" are different claims and
  // only one of them can be asserted by a test.
  await page.getByRole("button", { name: "Switch to dark theme" }).click();
  await page.screenshot({
    path: "test-results/workflow-parked-dark.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Switch to light theme" }).click();

  await approval.getByRole("button", { name: "Approve" }).click();

  // Approved, resumed, finished — from this panel, without going to find the
  // conversation the approval was recorded in.
  await expect(page.locator(".workflow-run-banner")).toContainText("Succeeded", {
    timeout: 60_000,
  });
  await expect(
    page.locator(".workflow-node.tool .workflow-node-status"),
  ).toContainText("Done");
  await expect(page.locator(".workflow-run-item").first()).toContainText("Succeeded");
  await page.screenshot({ path: "test-results/workflow-run.png", fullPage: true });

  // --- Put the shared workspace back -------------------------------------
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: `Delete ${WORKFLOW_NAME}` }).click();
  await expect(page.locator(".workflow-item")).toHaveCount(0);

  // The approved write really happened, and is this spec's to clean up.
  await openView(page, "Documents", /^Documents/);
  await page.getByText(WORKFLOW_DOCUMENT).click();
  // Confirm()-gated like every other destructive action here, so the handler
  // has to be armed before the click. It is consumed by this dialog, which is
  // what keeps it from reaching the conversation delete further down.
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete document" }).click();
  await expect(page.getByText(WORKFLOW_DOCUMENT)).toHaveCount(0);

  // The backing run hangs off a conversation, so the approval card had an
  // inbox to land in. It is still a stray thread once the workflow is gone.
  await page.reload();
  const thread = page.getByRole("button", { name: `Delete ${WORKFLOW_CONVERSATION}` });
  page.once("dialog", (dialog) => dialog.accept());
  await thread.click();
  await expect(thread).toHaveCount(0);

  expect(errors).toEqual([]);
});
