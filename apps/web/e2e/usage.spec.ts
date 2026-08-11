import { expect, test, type Page } from "@playwright/test";
import { openSettings } from "./shell";

/**
 * The spend panel, which is the one admin surface that can be wrong about money.
 *
 * `MODEL_PRICES` is empty on this deployment — it is empty on almost every
 * deployment, because that is the shipped default — so `GET /api/admin/usage`
 * answers `pricing_configured: false` and sums every cost to zero. The first
 * test is therefore the important one: the panel must say the cost is unknown
 * and must not print a dollar figure it did not measure.
 *
 * The last two stub the route. The e2e API runs the scripted model provider,
 * which reports no usage object at all, so the ledger here is empty *by
 * construction* and no amount of driving the UI will populate it — stubbing is
 * the only way to see the panel with rows in it, and a panel that has only ever
 * been looked at empty is a panel nobody has looked at.
 *
 * These create nothing and delete nothing: route handlers live on the page and
 * die with it, and no dialog appears anywhere below, so nothing here arms one.
 */

async function shot(page: Page, name: string) {
  await page.screenshot({ path: `test-results/usage-${name}.png`, fullPage: true });
}

const panelOf = (page: Page) => page.locator(".usage-panel");

async function openAdmin(page: Page) {
  await page.goto("/");
  await openSettings(page, "Admin");
  // The seeded e2e user owns this workspace, so the panels are the answer here;
  // the 403 sentence is asserted by the navigation spec.
  await expect(page.getByRole("heading", { name: "Admin" })).toBeVisible();
}

/** A populated response. `priced` decides whether it carries any money at all. */
function usageBody(priced: boolean) {
  const cost = (value: number) => (priced ? value : 0);
  return {
    window_days: 30,
    since: "2026-07-09T00:00:00Z",
    totals: {
      calls: 1_284,
      input_tokens: 4_120_355,
      cached_input_tokens: 980_000,
      output_tokens: 512_940,
      reasoning_tokens: 61_200,
      total_tokens: 4_633_295,
      cost_usd: cost(38.4172),
      priced_calls: priced ? 1_210 : 0,
      unpriced_calls: priced ? 74 : 1_284,
    },
    by_model: [
      {
        key: "gpt-5.5",
        label: "gpt-5.5",
        calls: 940,
        input_tokens: 3_010_000,
        output_tokens: 410_000,
        total_tokens: 3_420_000,
        cost_usd: cost(31.2),
        unpriced_calls: 0,
      },
      {
        key: "text-embedding-3-small",
        label: "text-embedding-3-small",
        calls: 270,
        input_tokens: 1_100_355,
        output_tokens: 0,
        total_tokens: 1_100_355,
        cost_usd: cost(7.2172),
        unpriced_calls: 0,
      },
      {
        key: "local-llama-3",
        label: "local-llama-3",
        calls: 74,
        input_tokens: 10_000,
        output_tokens: 102_940,
        total_tokens: 112_940,
        cost_usd: 0,
        unpriced_calls: 74,
      },
    ],
    by_user: [
      {
        key: "user-1",
        label: "Nate",
        calls: 1_100,
        input_tokens: 3_900_000,
        output_tokens: 480_000,
        total_tokens: 4_380_000,
        cost_usd: cost(35.1),
        unpriced_calls: 40,
      },
      {
        key: "",
        label: "",
        calls: 184,
        input_tokens: 220_355,
        output_tokens: 32_940,
        total_tokens: 253_295,
        cost_usd: cost(3.3172),
        unpriced_calls: 34,
      },
    ],
    by_operation: [
      {
        key: "chat",
        label: "chat",
        calls: 810,
        input_tokens: 2_900_000,
        output_tokens: 500_000,
        total_tokens: 3_400_000,
        cost_usd: cost(30.9),
        unpriced_calls: 0,
      },
      {
        key: "embedding",
        label: "embedding",
        calls: 474,
        input_tokens: 1_220_355,
        output_tokens: 12_940,
        total_tokens: 1_233_295,
        cost_usd: cost(7.5172),
        unpriced_calls: 74,
      },
    ],
    top_runs: [
      {
        run_id: "run-runaway-0001",
        conversation_id: "conv-1",
        calls: 412,
        total_tokens: 2_940_118,
        cost_usd: cost(24.8),
        unpriced_calls: 0,
        last_call_at: new Date().toISOString(),
      },
      {
        run_id: "9f3c7d21-4bd0-4a11-bd0e-77a1c0ffee00",
        conversation_id: "conv-2",
        calls: 61,
        total_tokens: 402_118,
        cost_usd: 0,
        unpriced_calls: 61,
        last_call_at: new Date().toISOString(),
      },
    ],
    unpriced_models: ["local-llama-3"],
    pricing_configured: priced,
  };
}

/**
 * Serve the fixture, and give the Runs panel one matching prompt so the join
 * that names a costly run is exercised rather than assumed.
 */
async function stubUsage(page: Page, priced: boolean) {
  await page.route("**/api/admin/usage**", async (route) => {
    await route.fulfill({ json: usageBody(priced) });
  });
  await page.route("**/api/admin/activity**", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.recent_runs = [
      {
        id: "run-runaway-0001",
        conversation_id: "conv-1",
        agent_id: "agent-1",
        created_by: "user-1",
        status: "failed",
        prompt_preview: "Reconcile every invoice against the ledger",
        error: "",
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
      ...body.recent_runs,
    ];
    await route.fulfill({ json: body });
  });
}

test("with no rates configured, the panel reports tokens and refuses to price them", async ({
  page,
}) => {
  await openAdmin(page);
  const panel = panelOf(page);
  await expect(panel).toBeVisible();

  // The whole point. A confident $0.00 is indistinguishable from "we spent
  // nothing", so with MODEL_PRICES empty there must be no currency on screen.
  await expect(panel.getByText(/Pricing is not configured — costs are unknown/))
    .toBeVisible();
  await expect(panel).toContainText("MODEL_PRICES");
  await expect(panel).not.toContainText("$");

  await shot(page, "unpriced-live");
});

test("the window is selectable and refetches the aggregates", async ({ page }) => {
  await openAdmin(page);
  const window = panelOf(page).getByRole("combobox", { name: "Usage window" });
  await expect(window).toHaveValue("30");

  const request = page.waitForRequest((call) => call.url().includes("/api/admin/usage?days=7"));
  await window.selectOption("7");
  await request;
  await expect(window).toHaveValue("7");
});

test("a populated unpriced window shows real tokens and no invented money", async ({
  page,
}) => {
  await stubUsage(page, false);
  await openAdmin(page);
  const panel = panelOf(page);

  // Tokens are measurements and are shown in full, with separators.
  await expect(panel).toContainText("4,633,295");
  await expect(panel).toContainText("1,284");
  // Costs are not, anywhere: totals, breakdown rows and runs alike.
  await expect(panel).not.toContainText("$");
  await expect(panel.getByText("Not priced").first()).toBeVisible();
  await expect(panel.getByText(/Bars show tokens/)).toBeVisible();

  // The runaway detector: named from the Runs panel's prompts where we have
  // them, and greppable by id where we do not.
  await expect(panel).toContainText("Reconcile every invoice against the ledger");
  await expect(panel).toContainText("9f3c7d21");
  await expect(panel).toContainText("2,940,118");

  await shot(page, "unpriced-populated");

  // The same panel under the other theme. The warning colours here are the
  // clay tokens, which are a different hue in dark, and a panel whose caveat is
  // unreadable is a panel without a caveat.
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(panel.getByText(/Pricing is not configured/)).toBeVisible();
  await shot(page, "unpriced-populated-dark");
});

test("with rates configured, money appears — and a partly-priced figure says so", async ({
  page,
}) => {
  await stubUsage(page, true);
  await openAdmin(page);
  const panel = panelOf(page);

  // 74 of 1,284 calls ran on a model with no rate, so the total is a floor.
  await expect(panel).toContainText("$38.42+");
  await expect(panel.getByText(/74 calls ran on a model with no configured rate/))
    .toBeVisible();
  await expect(panel).toContainText("local-llama-3");
  // The run that spent nothing measurable still refuses to claim it spent $0.00.
  await expect(panel).not.toContainText("$0.00");
  await expect(panel.getByText("Not priced").first()).toBeVisible();

  await shot(page, "priced-populated");
});
