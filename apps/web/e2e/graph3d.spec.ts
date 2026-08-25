import { expect, test } from "@playwright/test";
import { openView } from "./shell";

/**
 * Temporary verification that the 3D graph actually paints, not just that the
 * page survives mounting it. Passing e2e elsewhere only proves no crash.
 */
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
  await page.locator('input[type="file"]').setInputFiles({
    name: "graph3d-e2e.md",
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
  page.once("dialog", (dialog) => dialog.accept());
  await page
    .locator(".source-row", { hasText: "graph3d-e2e.md" })
    .getByTitle("Delete source")
    .click();
  await expect(page.getByText("graph3d-e2e.md")).toHaveCount(0);
  expect(failures).toEqual([]);
});
