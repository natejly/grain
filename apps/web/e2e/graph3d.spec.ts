import { expect, test, type Page } from "@playwright/test";
import { openView } from "./shell";

/**
 * Temporary verification that the 3D graph actually paints, not just that the
 * page survives mounting it. Passing e2e elsewhere only proves no crash.
 */

const SOURCE = "graph3d-e2e.md";

/**
 * Remove every source of this name, leaving none.
 *
 * Every rather than the first: the case worth handling is the one where a
 * previous attempt left one behind and this one has just added another — which
 * is also the case a `.first()` would quietly half-fix.
 */
async function removeSource(page: Page) {
  const remove = page
    .locator(".source-row", { hasText: SOURCE })
    .getByTitle("Delete source");
  // Re-counted each pass, since a deletion re-renders the list.
  for (let left = await remove.count(); left > 0; left = await remove.count()) {
    page.once("dialog", (dialog) => dialog.accept());
    await remove.first().click();
    await expect(remove).toHaveCount(left - 1);
  }
}
test("the 3d graph paints a non-empty canvas", async ({ page }) => {
  const failures: string[] = [];
  page.on("pageerror", (error) => failures.push(String(error)));
  page.on("console", (message) => {
    if (message.type() === "error") failures.push(message.text());
  });

  await page.goto("/");
  // The graph only has a canvas once there is something to draw, so seed an
  // entity-rich source first — the same flow the main spec uses.
  await openView(page, "Library", /Sources/);
  // Cleared first, because the teardown at the end of this test is exactly what
  // a failure skips. A leaked source is not merely untidy: the next attempt
  // uploads a second copy of the same name, so the delete below — and the main
  // spec's — matches two buttons and dies on strict mode. Every retry then
  // reports a different error than the one that started it, and the run says
  // "resolved to 2 elements" when the real failure was the canvas not painting.
  await removeSource(page);
  await page.locator('input[type="file"]').setInputFiles({
    name: SOURCE,
    mimeType: "text/markdown",
    buffer: Buffer.from(
      "# Northstar\n\nProject Northstar is owned by Maya Chen at Atlas Labs. " +
        "Maya Chen works with Devi Rao on the Juniper rollout at Atlas Labs.",
    ),
  });
  await expect(page.getByText("Indexed").last()).toBeVisible();

  await openView(page, "Library", /Graph/);
  await expect(
    page.getByText("Project Northstar", { exact: true }).first(),
  ).toBeVisible();

  const canvas = page.locator(".graph-3d-canvas canvas");
  await expect(canvas).toBeVisible({ timeout: 20000 });

  // Give the simulation and a few frames time to settle.
  await page.waitForTimeout(2500);

  // A WebGL canvas is not readable through drawImage once composited unless the
  // context was created with preserveDrawingBuffer, so the screenshot — which
  // captures what was actually presented — is the honest check.
  await page.locator(".graph-3d").screenshot({ path: "test-results/graph3d.png" });

  // ...and that it survives the page re-rendering around it. Selecting a row
  // hands Graph3D a freshly built `edges` array — the page has no way not to —
  // and a build effect keyed on that identity tore the whole scene down for it:
  // new WebGL context, new simulation, camera back to its framing shot, the
  // layout you were reading replaced by an expanding ball of noise. The canvas
  // element is the tell, because a rebuild removes and re-appends it.
  await canvas.evaluate((element) => element.setAttribute("data-kept", "yes"));
  // The name, not the row: in a 280px panel the row's own centre lands on the
  // Passage button beside it, which deliberately does not select.
  await page
    .locator(".entity-row", { hasText: "Atlas Labs" })
    .first()
    .locator("button.entity-select")
    .click();
  await expect(page.locator(".entity-row.selected")).toHaveCount(1);
  await expect(page.locator(".graph-3d-canvas canvas")).toHaveCount(1);
  await expect(canvas).toHaveAttribute("data-kept", "yes");
  console.log("page errors:", JSON.stringify(failures));

  // The corner legend names the node colours, one entry per entity type.
  const legend = page.locator(".graph-3d-legend");
  for (const label of ["organization", "project", "entity", "concept"]) {
    await expect(legend).toContainText(label);
  }

  // List → canvas: clicking a row selects it. The canvas half of that is
  // WebGL, so the row class is the honest DOM-level check. The click lands on
  // the row's own title button rather than the row's geometric center — the
  // center can fall on a nested provenance chip, which stops propagation and
  // navigates away instead of selecting.
  const row = page.locator(".entity-row", { hasText: "Project Northstar" }).first();
  await row.locator(".entity-select").click();
  await expect(row).toHaveClass(/selected/);

  // Leave the shared workspace as we found it. Without this the source lingers
  // and the main spec's "Delete source" lookup matches two buttons.
  await openView(page, "Library", /Sources/);
  await removeSource(page);
  await expect(page.getByText(SOURCE)).toHaveCount(0);
  expect(failures).toEqual([]);
});
