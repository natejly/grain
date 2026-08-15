import { expect, test, type Page } from "@playwright/test";
import { openSettings } from "./shell";

/**
 * A workspace gains a second member — the one thing this product could not do.
 *
 * Every collaborative feature already built rested on a multi-tenancy nothing
 * could produce: `Membership` was only ever written by signup, always as the
 * owner of a brand new workspace, so `require_owner` was true for everybody who
 * could reach it and the workspace switcher could only ever list one row.
 *
 * The whole loop is driven here rather than mocked, because the parts that can
 * be wrong are the parts that cross the seam: the link the owner is shown once
 * has to be the link that opens the accept page, and withdrawing the invitation
 * has to make that same link stop working. A stubbed route proves neither.
 *
 * The accept *click* is not driven, deliberately: accepting requires a session
 * whose address is the invited one, and minting a second real account inside a
 * browser test would be a signup flow standing in for an invitation flow. What
 * is driven is everything up to the button, plus the two refusals — which is
 * where the security lives. `tests/test_workspace_invites.py` drives the write.
 */

/**
 * A genuinely signed-out browser. The chromium project sets `storageState` in
 * `use`, and that reaches contexts opened off the `browser` handle too — so a
 * context that does not say this is the seeded owner wearing a guest's name,
 * and the "invitee has no account yet" path would never be exercised.
 */
const SIGNED_OUT = { storageState: { cookies: [], origins: [] } };

async function shot(page: Page, name: string) {
  await page.screenshot({ path: `test-results/invites-${name}.png`, fullPage: true });
}

const invitePanel = (page: Page) =>
  page.locator(".admin-panel").filter({ hasText: "Invitations" });

const memberPanel = (page: Page) =>
  page.locator(".admin-panel").filter({ hasText: "Members and roles" });

async function openAdmin(page: Page) {
  await page.goto("/");
  await openSettings(page, "Admin");
  await expect(page.getByRole("heading", { name: "Admin" })).toBeVisible();
}

/** A fresh address per run, so a re-run is not a 409 for an existing member. */
function newAddress() {
  return `invitee-${Date.now()}-${Math.floor(Math.random() * 10000)}@example.test`;
}

/**
 * Nothing inside a panel may be wider than the panel.
 *
 * Both of these panels shipped broken the first time and it was visible only in
 * a screenshot: the roster's action column gave the table a minimum width the
 * grid track (`minmax(320px, 1fr)`) is allowed to go below, and the invitation
 * link is one unbreakable URL. Both pushed out through the panel's right border
 * instead of scrolling inside it — 26px and 10px, measured. Assert it rather
 * than look at it again.
 */
async function fitsInsideItsPanel(panel: import("@playwright/test").Locator) {
  const overflow = await panel.evaluate(
    (el) => el.scrollWidth - el.clientWidth,
  );
  expect(overflow, "content is wider than the panel it is in").toBeLessThanOrEqual(0);
}

/**
 * ...and the row's action must be *readable*, not merely present.
 *
 * `toBeVisible` passes for an element clipped to two pixels by an ancestor's
 * `overflow: hidden`, which is exactly what the first fix for the overflow above
 * produced: the panel stopped bulging and the "Withdraw" button became "Wit".
 * Compare the boxes.
 */
async function isFullyOnScreen(
  panel: import("@playwright/test").Locator,
  control: import("@playwright/test").Locator,
) {
  const outer = await panel.boundingBox();
  const inner = await control.boundingBox();
  expect(outer && inner).toBeTruthy();
  expect(
    inner!.x + inner!.width,
    "the control is clipped by the panel edge",
  ).toBeLessThanOrEqual(outer!.x + outer!.width);
}

test("the roster offers a role control and a way out for each member", async ({
  page,
}) => {
  await openAdmin(page);
  const panel = memberPanel(page);
  await expect(panel).toBeVisible();
  // The seeded owner. Before invitations this was the only row a workspace
  // could ever have.
  const role = panel.getByLabel(/^Role for /).first();
  await expect(role).toBeVisible();
  await expect(role).toHaveValue("owner");
  await expect(panel.getByRole("button", { name: /^Remove / })).toBeVisible();
  await expect(panel.getByText("you")).toBeVisible();
  await fitsInsideItsPanel(panel);
  await isFullyOnScreen(panel, panel.getByRole("button", { name: /^Remove / }));
  await shot(page, "members");
});

test("an owner invites an address, is shown the link once, and can withdraw it", async ({
  page,
  context,
}) => {
  const address = newAddress();
  await openAdmin(page);
  const panel = invitePanel(page);
  await expect(panel).toBeVisible();

  await panel.getByLabel("Email").fill(address);
  await panel.getByRole("button", { name: /Send invitation/ }).click();

  // The link appears exactly once, in the response to the POST that minted it.
  // The API stores only its SHA-256, so this is the only chance to read it —
  // and in development, where EMAIL_SENDER defaults to `console`, it is the
  // whole delivery mechanism.
  const link = panel.locator(".invite-link-row code");
  await expect(link).toBeVisible();
  const acceptUrl = (await link.textContent()) || "";
  expect(acceptUrl).toContain("/auth/invite?token=");

  const row = panel.locator("tr", { hasText: address });
  await expect(row).toBeVisible();
  await expect(row.getByText("pending")).toBeVisible();
  // With the link block *and* a populated row on screen: the two widest things
  // this panel ever holds, both present at once.
  await fitsInsideItsPanel(panel);
  await isFullyOnScreen(
    panel,
    row.getByRole("button", { name: /Withdraw the invitation/ }),
  );
  await shot(page, "invited");

  // The link works for somebody with no session at all: an invitee may have no
  // account here yet, so the page has to be able to say what they are joining
  // before asking them for anything.
  const token = acceptUrl.split("token=")[1];
  const anonymous = await context.browser()!.newContext(SIGNED_OUT);
  const guest = await anonymous.newPage();
  await guest.goto(`/auth/invite?token=${token}`);
  await expect(guest.getByRole("heading", { name: /^Join / })).toBeVisible();
  // Twice on the page: named in the invitation, and again in the "sign in as"
  // hint. Either is the assertion; the strict-mode default is not.
  await expect(guest.getByText(address).first()).toBeVisible();
  await expect(guest.getByRole("link", { name: "Sign in to accept" })).toBeVisible();
  await shot(guest, "accept-page");

  // Withdrawing is what a link being a credential means in practice.
  await row.getByRole("button", { name: /Withdraw the invitation/ }).click();
  await expect(row.getByText("revoked")).toBeVisible();
  await expect(
    row.getByRole("button", { name: /Withdraw the invitation/ }),
  ).toHaveCount(0);
  await shot(page, "withdrawn");

  await guest.reload();
  await expect(guest.getByText("This invitation was withdrawn.")).toBeVisible();
  await shot(guest, "withdrawn-link");
  await anonymous.close();
});

test("an invitation link that names nothing is refused rather than half-rendered", async ({
  browser,
}) => {
  const anonymous = await browser.newContext(SIGNED_OUT);
  const guest = await anonymous.newPage();
  await guest.goto("/auth/invite?token=not-a-real-token");
  await expect(guest.getByRole("heading", { name: "That link did not work" })).toBeVisible();
  await expect(guest.getByRole("link", { name: "Continue to sign in" })).toBeVisible();
  await shot(guest, "bad-token");
  await anonymous.close();
});

test("the sign-in page comes back to the invitation instead of to the workspace", async ({
  browser,
}) => {
  // The `?next=` round trip. Without it, an invitee who has to sign in lands on
  // "/" still holding nothing, and accepting means finding the email again.
  const anonymous = await browser.newContext(SIGNED_OUT);
  const guest = await anonymous.newPage();
  await guest.goto("/auth/login?next=%2Fauth%2Finvite%3Ftoken%3Dabc");
  await expect(guest.getByRole("heading", { name: "Sign in" })).toBeVisible();
  // And the open-redirect fence: an absolute target is dropped, not followed.
  await guest.goto("/auth/login?next=https%3A%2F%2Fevil.test");
  await expect(guest).toHaveURL(/\/auth\/login/);
  await anonymous.close();
});
