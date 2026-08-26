import { expect, test, type Locator, type Page } from "@playwright/test";
import { createFromMenu, openThreadActions, openView, rail } from "./shell";

const API = "http://127.0.0.1:8010";

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

/**
 * A graph with declared parameters, stored through the API.
 *
 * The scripted compiler always emits the same two-node graph and it takes no
 * inputs, so a parameterised workflow cannot be reached by describing one — and
 * arranging a precondition is not the thing under test. `POST /api/workflows`
 * puts a hand-written graph through `compile_document`, the same validator the
 * model's output goes through, so this is a graph the compiler would accept and
 * not a row smuggled past it.
 */
const PARAMETERISED = {
  name: "Parameterised search",
  description: "Looks something up, with the something supplied at run time.",
  trigger: { kind: "manual", cron: "", timezone: "UTC" },
  inputs: [
    {
      name: "query",
      type: "string",
      label: "Search for",
      description: "The words to look for in this workspace's sources.",
      required: true,
      default: null,
      choices: [],
    },
    {
      name: "scope",
      type: "string",
      label: "Scope",
      description: "",
      required: false,
      default: "everything",
      choices: ["everything", "recent"],
    },
  ],
  nodes: [
    {
      id: "find",
      kind: "tool",
      description: "",
      tool: "search_sources",
      arguments: { query: "{{ input.query }} ({{ input.scope }})" },
      prompt: "",
    },
  ],
  edges: [],
};

const PARAMETERISED_NAME = PARAMETERISED.name;
const PARAMETERISED_CONVERSATION = `Workflow: ${PARAMETERISED_NAME}`;

/** Store a graph the way the compiler would, from inside the signed-in page. */
async function storeWorkflow(page: Page, graph: unknown) {
  const status = await page.evaluate(
    async ({ api, body }) => {
      const me = await fetch(`${api}/api/auth/me`, { credentials: "include" }).then(
        (response) => response.json(),
      );
      const response = await fetch(`${api}/api/workflows`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": me.csrf_token },
        body: JSON.stringify(body),
      });
      return { code: response.status, detail: await response.text() };
    },
    { api: API, body: { graph, status: "active", source_prompt: "" } },
  );
  // Named rather than asserted bare: a 422 here is the graph being wrong, and
  // the report says which field, which is the only useful thing to read.
  expect(status.detail).toContain(PARAMETERISED_NAME);
  expect(status.code).toBe(201);
}

/**
 * Fails when `target` never comes to rest inside the canvas that clips it.
 *
 * Retried, because the canvas brings a stopped step into frame with a short
 * animation and re-runs it when the approval card finishes loading and grows.
 * The claim being made is that the decision ends up reachable, not that it is
 * reachable on the first frame — asserting the latter measures a tween.
 */
async function expectInsideCanvas(page: Page, target: Locator) {
  await expect(async () => {
    const frame = await page.locator(".workflow-canvas").first().boundingBox();
    const box = await target.boundingBox();
    expect(frame, "the canvas has no box").not.toBeNull();
    expect(box, "the target has no box").not.toBeNull();
    expect(box!.y).toBeGreaterThanOrEqual(frame!.y);
    expect(box!.y + box!.height).toBeLessThanOrEqual(frame!.y + frame!.height);
    expect(box!.x).toBeGreaterThanOrEqual(frame!.x);
    expect(box!.x + box!.width).toBeLessThanOrEqual(frame!.x + frame!.width);
  }).toPass({ timeout: 10_000 });
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
  await expect(rail(page).getByRole("button", { name: /^Automations/ })).toHaveAttribute(
    "aria-current",
    "page",
  );

  await page
    .getByRole("textbox", { name: "Workflow prompt" })
    .fill("Every Monday, pull the open pull requests, summarise them, and post to Slack.");
  await page.getByRole("button", { name: "Compile" }).click();

  // The review step: the graph is on screen and nothing has been saved.
  const preview = page.locator(".workflow-preview");
  await expect(preview).toBeVisible({ timeout: 30_000 });
  const nodes = preview.locator(".workflow-node");
  await expect(nodes).toHaveCount(2);
  // The canvas really is a canvas: a dot grid and a viewport, not a list.
  await expect(
    preview.getByRole("group", { name: `${WORKFLOW_NAME} steps` }),
  ).toBeVisible();
  await expect(preview.locator(".react-flow__background")).toBeVisible();
  await expect(preview.locator(".react-flow__edge")).toHaveCount(1);
  await expect(preview.getByRole("button", { name: "Fit the whole graph" })).toBeVisible();
  // A tool node names its tool on the chip, which is the whole point of the
  // tool/agent split — a reader can see what it will call without opening it.
  await expect(preview.getByText("search_sources")).toBeVisible();
  // And everything else it carries is one hover away, on the node itself.
  const toolNode = preview.locator(".workflow-node.tool");
  // Nothing is hovered yet, and this asserts a closed chip — so park the
  // pointer somewhere harmless rather than wherever the last click left it.
  await page.mouse.move(0, 0);
  await expect(toolNode.locator("dt")).toHaveCount(0);
  await toolNode.hover();
  await expect(toolNode.locator("dt", { hasText: "query" })).toBeVisible();
  await expect(toolNode.getByRole("button")).toHaveAttribute("aria-expanded", "true");
  await page.locator(".workflow-author").screenshot({
    path: "test-results/workflow-preview.png",
  });
  // An agent node cannot list its calls, and says so rather than looking like
  // one that can — briefly on the chip, and in full when it is opened.
  const agentNode = preview.locator(".workflow-node.agent");
  await expect(agentNode).toBeVisible();
  await agentNode.hover();
  await expect(preview.getByText(/Chooses its own tools when it runs/)).toBeVisible();
  await page.locator(".workflow-canvas").screenshot({
    path: "test-results/workflow-preview-open.png",
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
  // The decision must be *decidable*, which `toBeVisible` does not check: the
  // canvas clips its overflow, so a parked chip in the bottom row can render an
  // Approve button with a real bounding box that is nonetheless painted outside
  // the frame and can never be clicked. This asserts where it actually landed.
  await expectInsideCanvas(page, approval.getByRole("button", { name: "Approve" }));
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
  await openView(page, "Library", /^Documents/);
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
  // `?view=` survives a reload now, so this comes back on the door the test was
  // last standing on, not the default one -- and the thread rail only renders
  // behind Chat (workspace.tsx gates the whole list on activeGroup.id).
  await openView(page, "Chat");
  const thread = page.getByRole("button", { name: `Delete ${WORKFLOW_CONVERSATION}` });
  page.once("dialog", (dialog) => dialog.accept());
  // The row's actions only become hittable once the row is hovered.
  await openThreadActions(
    page.locator(".thread").filter({ hasText: WORKFLOW_CONVERSATION }).first(),
  );
  await thread.click();
  await expect(thread).toHaveCount(0);

  expect(errors).toEqual([]);
});

test("workflows: a declared input is a form, and a refusal names the field", async ({
  page,
}) => {
  test.setTimeout(120_000);
  const errors = watchForErrors(page);
  await page.goto("/");
  await storeWorkflow(page, PARAMETERISED);

  await openView(page, "Automations");
  await page.getByRole("button", { name: PARAMETERISED_NAME }).first().click();
  await expect(page.getByRole("heading", { name: PARAMETERISED_NAME })).toBeVisible();

  // The declaration is a form: labelled by what the author wrote, not by the
  // slug, and a `choices` input is a select rather than a box to mistype into.
  const search = page.getByRole("textbox", { name: "Search for" });
  await expect(search).toBeVisible();
  const scope = page.getByRole("combobox", { name: "Scope" });
  await expect(scope).toHaveValue("everything");
  await expect(scope.getByRole("option")).toHaveCount(3);

  // Refused, and the refusal is next to the box rather than in a banner about
  // the run. Nothing was sent: the form could not produce a payload.
  await page.getByRole("button", { name: "Run now" }).click();
  await expect(page.locator(".workflow-inputs-alert")).toContainText("Search for");
  await expect(page.locator(".workflow-input-error")).toContainText("required");
  await expect(search).toHaveAttribute("aria-invalid", "true");
  // And the box *looks* refused. `aria-invalid` is what a screen reader hears;
  // a border colour is what everyone else gets, and the two are set in
  // different places — the first version of that rule was outranked by the one
  // giving every box its resting border, and painted nothing.
  const invalidBorder = await search.evaluate(
    (element) => getComputedStyle(element).borderColor,
  );
  const restingBorder = await page
    .getByRole("combobox", { name: "Scope" })
    .evaluate((element) => getComputedStyle(element).borderColor);
  expect(invalidBorder).not.toBe(restingBorder);
  await expect(page.locator(".workflow-run-item")).toHaveCount(0);
  await page.locator(".workflow-inputs").screenshot({
    path: "test-results/workflow-inputs-refused.png",
  });

  // Answered, and the run starts — with the value that was typed, resolved
  // into the argument the tool is actually called with.
  await search.fill("quarterly revenue");
  await scope.selectOption("recent");
  await page.getByRole("button", { name: "Run now" }).click();
  await expect(page.locator(".workflow-run-banner")).toContainText("Succeeded", {
    timeout: 60_000,
  });
  await expect(page.locator(".workflow-inputs-alert")).toHaveCount(0);
  // The list beside the run agrees with the banner. It is kept current by a
  // poll that this very status change switches off, so the last thing it wrote
  // was the state before this one — and nothing else was going to correct it.
  await expect(page.locator(".workflow-run-item").first()).toContainText("Succeeded");
  const node = page.locator(".workflow-node.tool");
  await node.hover();
  await expect(node.locator("dd")).toContainText("quarterly revenue (recent)");
  await page.screenshot({ path: "test-results/workflow-inputs-ran.png", fullPage: true });

  // --- Put the shared workspace back -------------------------------------
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: `Delete ${PARAMETERISED_NAME}` }).click();
  await expect(page.locator(".workflow-item")).toHaveCount(0);

  await page.reload();
  // `?view=` survives a reload now, so this comes back on the door the test was
  // last standing on, not the default one -- and the thread rail only renders
  // behind Chat (workspace.tsx gates the whole list on activeGroup.id).
  await openView(page, "Chat");
  const thread = page.getByRole("button", { name: `Delete ${PARAMETERISED_CONVERSATION}` });
  page.once("dialog", (dialog) => dialog.accept());
  // The row's actions only become hittable once the row is hovered.
  await openThreadActions(
    page.locator(".thread").filter({ hasText: PARAMETERISED_CONVERSATION }).first(),
  );
  await thread.click();
  await expect(thread).toHaveCount(0);

  expect(errors).toEqual([]);
});
