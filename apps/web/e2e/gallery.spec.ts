import { expect, test, type Page } from "@playwright/test";
import { openView } from "./shell";

/**
 * The marketplace loop, from the outside: author a skill, publish it to the
 * gallery, read the whole listing, install a copy, and find that copy standing
 * on the Skills page as an ordinary private skill.
 *
 * The slug is unique per run rather than cleaned up, because listings are
 * append-only on purpose — there is no delete to lean on, and republishing a
 * leftover slug would demand a changelog this spec has no reason to write.
 */

const STAMP = Date.now().toString(36);
const SLUG = `e2e-gallery-${STAMP}`;
const TITLE = `E2E Gallery Skill ${STAMP}`;
const BODY = "Summarize the thread in exactly two sentences.";

async function deleteSkillIfPresent(page: Page, title: string) {
  const remove = page.getByRole("button", { name: `Delete ${title}` });
  while ((await remove.count()) > 0) {
    page.once("dialog", (dialog) => void dialog.accept());
    const before = await remove.count();
    await remove.first().click();
    await expect(remove).toHaveCount(before - 1);
  }
}

test("a skill published to the gallery installs as a private copy", async ({
  page,
}) => {
  // Author the skill.
  await page.goto("/");
  await openView(page, "Chat", /^Skills/);
  await page.getByRole("button", { name: "New skill" }).click();
  await page.getByRole("textbox", { name: "Skill name" }).fill(SLUG);
  await page.getByRole("textbox", { name: "Skill title" }).fill(TITLE);
  await page.getByRole("textbox", { name: "Skill body" }).fill(BODY);
  await page.getByRole("button", { name: "Create skill" }).click();
  await expect(page.getByRole("button", { name: `Publish ${TITLE} to the gallery` })).toBeVisible();

  // Publish it. The drawer pre-fills from the skill; the slug is already
  // unique, so the defaults are the whole form.
  await page.getByRole("button", { name: `Publish ${TITLE} to the gallery` }).click();
  await expect(page.getByRole("textbox", { name: "Listing slug" })).toHaveValue(SLUG);
  await page.getByRole("textbox", { name: "Listing byline" }).fill("E2E");
  await page.getByRole("button", { name: "Publish", exact: true }).click();
  await expect(page.getByRole("status")).toContainText(`Published as ${SLUG}`);
  await page.getByRole("button", { name: "Done" }).click();

  // Browse the gallery and open the listing. The full body is on screen
  // before the install button — that render is the consent gate.
  await openView(page, "Library", /^Gallery/);
  const card = page.locator(".mcp-card", { hasText: SLUG });
  await expect(card).toContainText("by E2E");
  await card.getByRole("button", { name: "View & install" }).click();
  const drawer = page.locator(".agent-drawer");
  await expect(drawer).toContainText(BODY);
  await drawer.getByRole("button", { name: "Install a copy" }).click();
  await expect(drawer.getByRole("status")).toContainText("Installed as");
  // The source skill holds the slug in this workspace, so the copy suffixes.
  await expect(drawer.getByRole("status")).toContainText(`${SLUG}-2`);
  await page.locator(".drawer-scrim").click({ position: { x: 5, y: 5 } });

  // The copy stands on the Skills page: same title, suffixed slug, private.
  await openView(page, "Chat", /^Skills/);
  const copy = page.locator(".mcp-card", { hasText: `/${SLUG}-2` });
  await expect(copy).toContainText(TITLE);
  await expect(copy.locator(".status-pill", { hasText: "shared" })).toHaveCount(0);

  // Tidy the skills page (the listing itself is append-only and stays).
  await deleteSkillIfPresent(page, TITLE);
});

test("a republished listing offers and applies an update to the installed copy", async ({
  page,
}) => {
  const SLUG2 = `e2e-update-${STAMP}`;
  const TITLE2 = `E2E Update Skill ${STAMP}`;

  // Author, publish, and install — the Phase 1 loop, compressed.
  await page.goto("/");
  await openView(page, "Chat", /^Skills/);
  await page.getByRole("button", { name: "New skill" }).click();
  await page.getByRole("textbox", { name: "Skill name" }).fill(SLUG2);
  await page.getByRole("textbox", { name: "Skill title" }).fill(TITLE2);
  await page.getByRole("textbox", { name: "Skill body" }).fill("First body.");
  await page.getByRole("button", { name: "Create skill" }).click();
  await page
    .getByRole("button", { name: `Publish ${TITLE2} to the gallery` })
    .click();
  await page.getByRole("button", { name: "Publish", exact: true }).click();
  await expect(page.getByRole("status")).toContainText(`Published as ${SLUG2}`);
  await page.getByRole("button", { name: "Done" }).click();

  await openView(page, "Library", /^Gallery/);
  const card = page.locator(".mcp-card", { hasText: SLUG2 });
  await card.getByRole("button", { name: "View & install" }).click();
  const drawer = page.locator(".agent-drawer");
  await drawer.getByRole("button", { name: "Install a copy" }).click();
  await expect(drawer.getByRole("status")).toContainText("Installed as");
  await page.locator(".drawer-scrim").click({ position: { x: 5, y: 5 } });
  await expect(
    card.locator(".status-pill", { hasText: "installed" }),
  ).toBeVisible();

  // Sharpen the source and republish it — the changelog is what the
  // republish arm demands.
  await openView(page, "Chat", /^Skills/);
  const source = page
    .locator(".mcp-card", { hasText: `/${SLUG2}` })
    .filter({ hasNotText: `/${SLUG2}-2` });
  await source.getByRole("button", { name: "Edit" }).click();
  await page
    .getByRole("textbox", { name: "Skill body" })
    .fill("Second body, sharper.");
  await page.getByRole("button", { name: "Save changes" }).click();
  await source
    .getByRole("button", { name: `Publish ${TITLE2} to the gallery` })
    .click();
  await page.getByRole("textbox", { name: "Listing changelog" }).fill("Sharper.");
  await page.getByRole("button", { name: "Publish", exact: true }).click();
  await expect(page.getByRole("status")).toContainText(`Published as ${SLUG2}`);
  await page.getByRole("button", { name: "Done" }).click();

  // The gallery offers the update; applying it rewrites the copy in place.
  await openView(page, "Library", /^Gallery/);
  await expect(
    card.locator(".status-pill", { hasText: "update available" }),
  ).toBeVisible();
  await card.getByRole("button", { name: "View & install" }).click();
  await drawer.getByRole("button", { name: "Update to v2" }).click();
  await expect(drawer.getByRole("status")).toContainText("Updated your copy");
  await page.locator(".drawer-scrim").click({ position: { x: 5, y: 5 } });
  await expect(
    card.locator(".status-pill", { hasText: "update available" }),
  ).toHaveCount(0);

  // The copy itself carries the new body.
  await openView(page, "Chat", /^Skills/);
  const copy = page.locator(".mcp-card", { hasText: `/${SLUG2}-2` });
  await copy.getByRole("button", { name: "Edit" }).click();
  await expect(page.getByRole("textbox", { name: "Skill body" })).toHaveValue(
    "Second body, sharper.",
  );
  await page.getByRole("button", { name: "Close" }).click();

  await deleteSkillIfPresent(page, TITLE2);
});
