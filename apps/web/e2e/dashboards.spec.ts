import { expect, test, type Page } from "@playwright/test";
import { openView } from "./shell";

/**
 * The home screen, end to end: pin, arrange, bind, and be refused.
 *
 * Nothing here builds a dashboard through the UI, because nothing in the
 * product does — the agent authors them in chat and this page curates them. So
 * the fixtures are written straight to the API and everything after that is
 * driven the way a person drives it.
 *
 * The assertions are about *rendered* state rather than about elements being
 * present. A tile whose query failed still has a heading, a grid that saves
 * nothing still shows tiles where you dropped them until you reload, and a
 * refusal rendered as "invalid" still puts a red box on the screen. Each of
 * those is the bug this feature exists to avoid, and only the stronger
 * assertion can see it.
 */

const API = "http://127.0.0.1:8010";

const DASHBOARDS = ["E2E revenue by region", "E2E orders by region"];
const TEMPLATE = "E2E totals template";
const SOURCE = "dashboards-e2e.csv";

type Placement = { name: string; x: number; y: number; w: number; h: number };

/** Every write below goes through the browser's own session and CSRF token. */
async function callApi(
  page: Page,
  method: string,
  path: string,
  body?: unknown,
  extra: Record<string, string> = {},
): Promise<{ status: number; json: unknown }> {
  return page.evaluate(
    async ({ api, method, path, body, extra }) => {
      const me = await fetch(`${api}/api/auth/me`, { credentials: "include" });
      const csrf = (await me.json()).csrf_token as string;
      const res = await fetch(`${api}${path}`, {
        method,
        credentials: "include",
        headers: {
          "x-csrf-token": csrf,
          "content-type": "application/json",
          ...extra,
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      const text = await res.text();
      return { status: res.status, json: text ? JSON.parse(text) : null };
    },
    { api: API, method, path, body, extra } as const,
  );
}

async function pinPlacements(page: Page): Promise<Placement[]> {
  const { json } = await callApi(page, "GET", "/api/dashboard-pins");
  return (json as { dashboard: { name: string }; grid_x: number; grid_y: number; grid_w: number; grid_h: number }[]).map(
    (pin) => ({
      name: pin.dashboard.name,
      x: pin.grid_x,
      y: pin.grid_y,
      w: pin.grid_w,
      h: pin.grid_h,
    }),
  );
}

const placementOf = (pins: Placement[], name: string) =>
  pins.find((pin) => pin.name === name)!;

test.describe("dashboards", () => {
  test.beforeAll(async ({ browser }) => {
    const page = await browser.newPage();
    await page.goto("/");

    // A dataset only exists downstream of a source, so upload one and let the
    // indexer finish before anything asks for its columns.
    await openView(page, "Library", /Sources/);
    await page.locator('input[type="file"]').setInputFiles({
      name: SOURCE,
      mimeType: "text/csv",
      buffer: Buffer.from(
        "region,amount,sold_on\nNorth,120,2026-01-01\nSouth,80,2026-01-02\nEast,200,2026-01-03\nNorth,45,2026-01-04\n",
      ),
    });
    await expect(page.getByText("Indexed").last()).toBeVisible({ timeout: 30_000 });

    const { json: datasets } = await callApi(page, "GET", "/api/datasets");
    const dataset = (datasets as { id: string; name: string }[]).find((item) =>
      item.name.includes("dashboards-e2e"),
    );
    expect(dataset, "the uploaded CSV did not become a dataset").toBeTruthy();

    await callApi(
      page,
      "POST",
      "/api/dashboards",
      {
        name: DASHBOARDS[0],
        description: "",
        dataset_id: dataset!.id,
        spec: {
          visualization: "bar",
          query: {
            group_by: "region",
            metrics: [{ label: "revenue", operation: "sum", field: "amount" }],
          },
          x_field: "region",
          y_fields: ["revenue"],
        },
      },
      { "idempotency-key": `e2e-dash-a-${Date.now()}` },
    );
    await callApi(
      page,
      "POST",
      "/api/dashboards",
      {
        name: DASHBOARDS[1],
        description: "",
        dataset_id: dataset!.id,
        spec: {
          visualization: "donut",
          query: { group_by: "region", metrics: [] },
          x_field: "region",
          y_fields: ["count"],
        },
      },
      { "idempotency-key": `e2e-dash-b-${Date.now()}` },
    );
    await callApi(page, "POST", "/api/dashboard-templates", {
      name: TEMPLATE,
      description: "Totals by a label",
      required_columns: [
        { name: "label", type: "string", description: "" },
        { name: "value", type: "number", description: "" },
      ],
      spec: {
        visualization: "bar",
        query: {
          group_by: "label",
          metrics: [{ label: "total", operation: "sum", field: "value" }],
        },
        x_field: "label",
        y_fields: ["total"],
      },
    });
    await page.close();
  });

  test.afterAll(async ({ browser }) => {
    // The specs share one workspace, so everything created above is removed
    // here — including the dashboard the bind test creates on its way past.
    const page = await browser.newPage();
    await page.goto("/");

    const { json: dashboards } = await callApi(page, "GET", "/api/dashboards");
    for (const dashboard of dashboards as { id: string; name: string }[]) {
      if (dashboard.name.startsWith("E2E ")) {
        await callApi(page, "DELETE", `/api/dashboards/${dashboard.id}`);
      }
    }
    const { json: templates } = await callApi(page, "GET", "/api/dashboard-templates");
    for (const template of templates as { id: string; name: string }[]) {
      if (template.name === TEMPLATE) {
        await callApi(page, "DELETE", `/api/dashboard-templates/${template.id}`);
      }
    }
    // Matched on a substring rather than on equality: the upload is stored
    // under a title the server derives from the filename, and a source left
    // behind becomes a dataset every later spec's dataset picker can see.
    const { json: sources } = await callApi(page, "GET", "/api/sources");
    for (const source of sources as { id: string; title?: string; filename?: string }[]) {
      const label = `${source.title ?? ""} ${source.filename ?? ""}`;
      if (label.includes("dashboards-e2e")) {
        // Unlike the dashboard and template deletes above, this one is behind an
        // idempotency key. Without it the request is rejected before it reaches
        // the row, and `callApi` returns that status to nobody — so the source
        // survived every run of this cleanup, and the *next* file's
        // getByTitle("Delete source") found two buttons instead of one.
        await callApi(page, "DELETE", `/api/sources/${source.id}`, undefined, {
          "Idempotency-Key": `dashboards-e2e-source-${source.id}`,
        });
      }
    }

    // Prove the cleanup rather than assume it: anything still named for this
    // spec would surface as a phantom in whichever file happens to run next.
    const { json: left } = await callApi(page, "GET", "/api/dashboards");
    expect(
      (left as { name: string }[]).filter((item) => item.name.startsWith("E2E ")),
    ).toEqual([]);
    // The source was asserted on by nothing until it leaked, which is exactly
    // how it leaked. A survivor here is a second row in every later spec's
    // source list and dataset picker.
    const { json: sourcesLeft } = await callApi(page, "GET", "/api/sources");
    expect(
      (sourcesLeft as { title?: string; filename?: string }[]).filter((item) =>
        `${item.title ?? ""} ${item.filename ?? ""}`.includes("dashboards-e2e"),
      ),
    ).toEqual([]);
    await page.close();
  });

  test("pin from the catalog, and the rail lists what you pinned", async ({ page }) => {
    await page.goto("/");
    await openView(page, "Library", /^Dashboards/);

    // One page section holding every dashboard in the workspace — a shelf on
    // the page rather than a popover you summon and lose on click-away.
    const menu = page.getByRole("region", { name: "All dashboards" });
    await expect(menu).toBeVisible();
    for (const name of DASHBOARDS) {
      await expect(menu.getByRole("button", { name: `Pin ${name}` })).toBeVisible();
    }

    await menu.getByRole("button", { name: `Pin ${DASHBOARDS[0]}` }).click();
    await menu.getByRole("button", { name: `Pin ${DASHBOARDS[1]}` }).click();
    // The same control now offers the opposite verb, which is how the catalog
    // shows pinned state rather than by styling alone.
    await expect(menu.getByRole("button", { name: `Unpin ${DASHBOARDS[0]}` })).toHaveAttribute(
      "aria-pressed",
      "true",
    );

    // Requirement 1: pinned dashboards appear beneath the rail's own items.
    const pinned = page.getByRole("navigation", { name: "Pinned dashboards" });
    for (const name of DASHBOARDS) {
      await expect(pinned.getByRole("button", { name })).toBeVisible();
    }
  });

  test("a pinned tile draws the numbers its query returned", async ({ page }) => {
    await page.goto("/");
    await openView(page, "Library", /^Dashboards/);

    const tile = page.locator(".dashboard-pin-tile", { hasText: DASHBOARDS[0] });
    await expect(tile).toBeVisible();

    // Not "a chart element exists": the bars carry the summed values, and North
    // is 120 + 45 rather than either row on its own. A tile that rendered an
    // empty chart, or one that failed to run, passes every presence check.
    await expect(tile.locator(".chart-error")).toHaveCount(0);
    const bars = tile.locator(".mini-bar-row");
    await expect(bars).toHaveCount(3);
    await expect(bars.filter({ hasText: "North" })).toContainText("165");
    await expect(bars.filter({ hasText: "East" })).toContainText("200");

    // The bar's own width is the encoding; East is the maximum and North is
    // 82.5% of it. A chart drawn against the wrong scale still shows the right
    // number beside a bar of the wrong length.
    const width = async (region: string) =>
      (await bars.filter({ hasText: region }).locator("i").boundingBox())!.width;
    expect((await width("North")) / (await width("East"))).toBeCloseTo(165 / 200, 1);

    const donut = page.locator(".dashboard-pin-tile", { hasText: DASHBOARDS[1] });
    await expect(donut.locator(".donut-legend li").filter({ hasText: "North" })).toContainText(
      "50%",
    );
  });

  test("arranging the grid survives a reload", async ({ page }) => {
    await page.goto("/");
    await openView(page, "Library", /^Dashboards/);
    await expect(page.locator(".dashboard-pin-tile")).toHaveCount(2);

    const before = placementOf(await pinPlacements(page), DASHBOARDS[0]);

    // Requirement 3, by keyboard: a grid that can only be arranged by dragging
    // cannot be arranged by a keyboard user at all.
    const grip = page.getByRole("button", { name: new RegExp(`^Move ${DASHBOARDS[0]}`) });
    await grip.focus();
    await grip.press("ArrowRight");
    await grip.press("ArrowRight");

    const resize = page.getByRole("button", {
      name: new RegExp(`^Resize ${DASHBOARDS[0]}`),
    });
    await resize.focus();
    await resize.press("ArrowRight");

    await expect
      .poll(async () => placementOf(await pinPlacements(page), DASHBOARDS[0]).x)
      .toBe(before.x + 2);
    const moved = placementOf(await pinPlacements(page), DASHBOARDS[0]);
    expect(moved.w).toBe(before.w + 1);

    await page.reload();
    await openView(page, "Library", /^Dashboards/);
    const tile = page.locator(".dashboard-pin-tile", { hasText: DASHBOARDS[0] });
    // The saved placement is what the browser actually lays the tile out with,
    // not merely what the API stored.
    await expect(tile).toHaveCSS("grid-column-start", String(moved.x + 1));
    expect(placementOf(await pinPlacements(page), DASHBOARDS[0])).toEqual(moved);
  });

  test("a drag lands the tile where it was dropped and moves nobody else off-grid", async ({
    page,
  }) => {
    await page.goto("/");
    await openView(page, "Library", /^Dashboards/);
    await expect(page.locator(".dashboard-pin-tile")).toHaveCount(2);

    const grip = page.getByRole("button", { name: new RegExp(`^Move ${DASHBOARDS[1]}`) });
    const box = (await grip.boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + 300, box.y + box.height / 2, { steps: 15 });
    await page.mouse.up();

    await expect
      .poll(async () => placementOf(await pinPlacements(page), DASHBOARDS[1]).x)
      .toBeGreaterThan(0);

    for (const pin of await pinPlacements(page)) {
      expect(pin.x + pin.w, `${pin.name} runs past the twelfth column`).toBeLessThanOrEqual(12);
      expect(pin.y).toBeLessThanOrEqual(200);
    }
  });

  test("a binding that does not fit is refused by name, not by adjective", async ({
    page,
  }) => {
    await page.goto("/");
    await openView(page, "Library", /^Dashboards/);

    const card = page.locator(".dashboard-template-card", { hasText: TEMPLATE });
    await expect(card).toBeVisible();
    await card
      .getByRole("combobox", { name: new RegExp(`^Dataset for ${TEMPLATE}`) })
      .selectOption({ label: "dashboards-e2e" });

    // `value` is declared number and bound to a string column. The template can
    // never be satisfied this way, and the point of binding at all is that the
    // person is told so now rather than on the morning they open the chart.
    // The selects are, in order: the dataset, then one per declared column in
    // the order the template declares them — `label`, then `value`.
    await card.locator("select").nth(1).selectOption("sold_on");
    await card.locator("select").nth(2).selectOption("region");
    await card.getByRole("button", { name: "Create dashboard" }).click();

    // Requirement 4: the refusal names the column and the type that did not
    // satisfy it. "That binding was refused" is the failure being guarded
    // against, so the assertion is on the sentence, not on the box.
    const refusal = card.locator(".dashboard-bind-refusal");
    await expect(refusal).toBeVisible();
    await expect(refusal).toContainText("value");
    await expect(refusal).toContainText("number");
    await expect(refusal).toContainText("region");
    await expect(refusal).toContainText("string");
    await expect(refusal).not.toContainText("That binding was refused");

    // And the same form binds cleanly once the columns actually fit.
    await card.locator("select").nth(1).selectOption("region");
    await card.locator("select").nth(2).selectOption("amount");
    await card
      .getByRole("textbox", { name: new RegExp(`^Name for a dashboard from ${TEMPLATE}`) })
      .fill("E2E bound totals");
    await card.getByRole("button", { name: "Create dashboard" }).click();
    await expect(card.locator(".dashboard-bind-ok")).toContainText("E2E bound totals");
    await expect(card.locator(".dashboard-bind-refusal")).toHaveCount(0);
  });

  test("unpinning takes the tile off the screen and the entry out of the rail", async ({
    page,
  }) => {
    await page.goto("/");
    await openView(page, "Library", /^Dashboards/);

    await page
      .getByRole("button", { name: new RegExp(`^Unpin ${DASHBOARDS[1]}`) })
      .first()
      .click();

    await expect(
      page.locator(".dashboard-pin-tile", { hasText: DASHBOARDS[1] }),
    ).toHaveCount(0);
    await expect(
      page
        .getByRole("navigation", { name: "Pinned dashboards" })
        .getByRole("button", { name: DASHBOARDS[1] }),
    ).toHaveCount(0);
    // Unpinning is not deleting: the dashboard is still on the shelf.
    await expect(
      page
        .getByRole("region", { name: "All dashboards" })
        .getByRole("button", { name: `Pin ${DASHBOARDS[1]}` }),
    ).toBeVisible();
  });
});
