import { expect, test, type Locator, type Page } from "@playwright/test";
import { openView, rail } from "./shell";

/**
 * Collapsing the panes on the left, proved by the width the working area gains.
 *
 * The assertion is deliberately a measurement rather than "the rail is hidden":
 * a rail that is `visibility: hidden` still holds its 252px track, and the point
 * of the feature is the space, not the invisibility. Three things have to hold
 * for each pane — the room is really handed over, the control that undoes it
 * survives and says what it will do next, and the choice outlives a reload.
 */

/** The rendered width of an element, which is the thing under test. */
async function widthOf(locator: Locator): Promise<number> {
  const box = await locator.boundingBox();
  if (!box) throw new Error("the element being measured is not rendered");
  return box.width;
}

/** A toggle addressed the way a screen reader meets it: by what it will do. */
const toggle = (page: Page, name: string) => page.getByRole("button", { name, exact: true });

test("the sidebar collapses, hands its width to the view, and stays collapsed", async ({
  page,
}) => {
  await page.goto("/");
  await expect(rail(page).getByRole("button")).toHaveCount(4);

  const main = page.locator(".main-panel");
  const sidebar = page.locator("#workspace-rail");
  const wide = await widthOf(main);

  // Reached and operated from the keyboard, not the mouse: focus it and press
  // Enter. `aria-expanded` says where you are, the name says where pressing it
  // takes you, and the two must not be the same sentence.
  const collapse = toggle(page, "Hide the sidebar");
  await expect(collapse).toHaveAttribute("aria-expanded", "true");
  await collapse.focus();
  await expect(collapse).toBeFocused();
  await page.keyboard.press("Enter");

  // The CONTEXT sidebar is gone and the view has the room; the icon rail
  // stays, because the four doors are what keep a collapsed shell navigable.
  // 200px is a floor, not a measurement: the sidebar's track is 252px, and
  // anything that merely hid it without releasing the column would move this
  // by zero.
  await expect(sidebar).toBeHidden();
  await expect(rail(page)).toBeVisible();
  const collapsedWidth = await widthOf(main);
  expect(collapsedWidth - wide).toBeGreaterThan(200);

  const expand = toggle(page, "Show the sidebar");
  await expect(expand).toBeVisible();
  await expect(expand).toHaveAttribute("aria-expanded", "false");
  await page.screenshot({ path: "test-results/collapse-rail.png" });

  // Across a reload, which is the whole reason it is in localStorage.
  await page.reload();
  await expect(toggle(page, "Show the sidebar")).toBeVisible();
  await expect(sidebar).toBeHidden();
  await expect(rail(page)).toBeVisible();
  expect(await widthOf(main)).toBeGreaterThan(wide + 200);

  // And back, so the rest of this file — and any spec that shares the storage
  // state — meets the shell it expects.
  await toggle(page, "Show the sidebar").click();
  await expect(sidebar).toBeVisible();
  expect(await widthOf(main)).toBeCloseTo(wide, 0);
});

test("the documents file list collapses to a strip that can still be reopened", async ({
  page,
}) => {
  await page.goto("/");
  // The Library group lands on Documents, whose sidebar is the folder tree.
  await openView(page, "Library", /^Documents/);

  const list = page.locator(".documents-list");
  const editor = page.locator(".documents-layout");
  await expect(list).toBeVisible();
  const openWidth = await widthOf(list);
  expect(openWidth).toBeGreaterThan(180);
  // The tree's own content, which is what disappears — asserted as text so this
  // cannot pass against an empty pane that happens to be the right width.
  await expect(list).toContainText("Documents");

  await toggle(page, "Hide the file list").click();

  // A strip, not nothing: unlike the rail, this pane carries its own toggle, so
  // collapsing it to zero would leave no way back.
  const strip = await widthOf(list);
  expect(strip).toBeLessThan(60);
  expect(strip).toBeGreaterThan(0);
  // The tree is gone rather than clipped — but asserted as visibility, not as
  // `not.toContainText`. The pane empties with `display: none`, which takes its
  // rows out of the picture, out of the accessibility tree and out of the tab
  // order while leaving them in `textContent`, and `textContent` is what
  // `toContainText` reads. So the claim is made the way a user meets it: none
  // of the tree shows, and the single control left standing is the toggle back.
  await expect(list.getByText("Documents", { exact: true })).toBeHidden();
  await expect(list.getByRole("button")).toHaveCount(1);
  const reopen = toggle(page, "Show the file list");
  await expect(reopen).toBeVisible();
  await expect(reopen).toHaveAttribute("aria-expanded", "false");
  await page.screenshot({ path: "test-results/collapse-documents-list.png" });

  // The layout really re-flowed rather than the pane merely being painted over.
  expect(await widthOf(editor)).toBeGreaterThan(0);

  await page.reload();
  await openView(page, "Library", /^Documents/);
  await expect(toggle(page, "Show the file list")).toBeVisible();
  expect(await widthOf(page.locator(".documents-list"))).toBeLessThan(60);

  await toggle(page, "Show the file list").click();
  await expect(page.locator(".documents-list")).toContainText("Documents");
  expect(await widthOf(page.locator(".documents-list"))).toBeCloseTo(openWidth, 0);
});
