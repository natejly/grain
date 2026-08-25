import { expect, test, type Page } from "@playwright/test";
import { openView } from "./shell";

/**
 * The Rules & policies page, driven against the real API: the standing-grant
 * ledger and, under it, the organization panel.
 *
 * The org panel lived inside Admin once, where the view's owner-only fetches
 * 403-walled it for exactly the members it governs; it is reached through the
 * Inbox door now (Inbox → Rules), which is the point this spec pins by
 * navigating there like a user rather than mounting anything directly.
 *
 * What is worth a browser for is the round trip, not the markup: a ceiling set
 * here has to be a row `evaluate_policy` reads, and the only way to know the
 * PUT body and the GET shape agree is to set one and reload. The unit test in
 * `tests/organization-panel.test.ts` covers the `null` / `[]` distinction
 * against a mock; this covers the part a mock cannot, which is that the server
 * agrees.
 *
 * The seeded dev owner is also the admin of the seeded organization — exactly
 * what signup produces — so the write controls are present. The *refusal* path,
 * a workspace owner who is not an org admin, is pinned server-side in
 * `tests/test_org_scope.py`, where it belongs: it is an authorization fact, and
 * proving it through a browser would prove the button was disabled rather than
 * that the request was refused.
 */

const orgPanel = (page: Page) =>
  page.locator(".admin-panel").filter({ hasText: "Organization" });

async function openPolicies(page: Page) {
  await page.goto("/");
  await openView(page, "Inbox", /^Rules/);
  await expect(
    page.getByRole("heading", { name: "Rules & policies" }),
  ).toBeVisible();
}

/** Nothing inside a panel may be wider than the panel — see invites.spec.ts. */
async function fitsInsideItsPanel(panel: import("@playwright/test").Locator) {
  const overflow = await panel.evaluate((el) => el.scrollWidth - el.clientWidth);
  expect(overflow, "content is wider than the panel it is in").toBeLessThanOrEqual(0);
}

/**
 * Write a personal tool grant straight through the API, with the browser's own
 * session cookie and CSRF token — the same arrangement todos-approval.spec.ts
 * uses. The UI's only creation path is the "always allow" checkbox on a live
 * approval card, which needs an agent run this spec does not want to stage.
 */
async function writeToolPolicy(page: Page, tool: string): Promise<void> {
  const failure = await page.evaluate(async (toolName) => {
    // The API this dev server was told to talk to; the page itself is on 3010.
    const base = "http://127.0.0.1:8010";
    const session = await fetch(`${base}/api/auth/me`, { credentials: "include" });
    if (!session.ok) return `auth/me said ${session.status}`;
    const { csrf_token, workspace_id } = await session.json();
    const response = await fetch(`${base}/api/tool-policies`, {
      method: "PUT",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf_token,
        "X-Workspace-Id": workspace_id,
      },
      body: JSON.stringify({ tool_name: toolName, policy: "allow", scope: "chat" }),
    });
    return response.ok ? "" : `${response.status} ${await response.text()}`;
  }, tool);
  expect(failure, `tool policy allow for ${tool}`).toBe("");
}

test("the organization panel states the posture and survives a round trip", async ({
  page,
}) => {
  await openPolicies(page);
  const panel = orgPanel(page);
  await expect(panel).toBeVisible();
  await expect(panel.getByText("admin", { exact: true }).first()).toBeVisible();

  // A tool name unique to this run, so a re-run is not asserting on a leftover.
  const tool = `probe_${Date.now()}`;
  await panel.getByLabel("Tool").fill(tool);
  await panel.getByLabel("Scope").selectOption("workflow");
  await panel.getByLabel("Ceiling").selectOption("deny");
  await panel.getByRole("button", { name: "Set ceiling" }).click();
  await expect(panel.getByText(tool)).toBeVisible();

  await fitsInsideItsPanel(panel);
  // The panel rather than the page: this spec is about one card, and a
  // full-page shot buries it under the ledger above it.
  await panel.screenshot({ path: "test-results/organization-panel.png" });

  // The round trip: come back to a freshly mounted panel and the ceiling is
  // still there, which is only true if the PUT wrote what the GET reads.
  // Re-navigated rather than reloaded because the view is client state and not
  // a URL, so a reload lands on chat and would prove nothing.
  await openPolicies(page);
  await expect(orgPanel(page).getByText(tool)).toBeVisible();

  // And clearing it removes the row rather than leaving a stale one on screen.
  await orgPanel(page)
    .getByLabel(`Clear the workflow ceiling for ${tool}`)
    .click();
  await expect(orgPanel(page).getByText(tool)).toHaveCount(0);
});

test("a standing grant shows in the Rules ledger and revoking removes it", async ({
  page,
}) => {
  await page.goto("/");
  // Unique per run: the ledger lists whatever the workspace holds, and a
  // leftover from a broken earlier run must not satisfy this one.
  const tool = `probe_rule_${Date.now()}`;
  await writeToolPolicy(page, tool);

  await openView(page, "Inbox", /^Rules/);
  const rules = page.locator(".admin-panel").filter({ hasText: "Standing tool grants" });
  const row = rules.locator("tr").filter({ hasText: tool });
  await expect(row).toHaveCount(1);
  // The row states the grant in the ledger's terms, not the enum's.
  await expect(row).toContainText("while chatting");
  await expect(row).toContainText("You");

  // Revoking is the ledger's own affordance — and the list shrinks because the
  // server agreed, not because the row was filtered client-side.
  await row.getByLabel(`Revoke the personal chat rule for ${tool}`).click();
  await expect(rules.getByText(tool)).toHaveCount(0);
});
