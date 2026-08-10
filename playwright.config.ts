import { defineConfig, devices } from "@playwright/test";
import { STORAGE_STATE } from "./apps/web/e2e/credentials";

export default defineConfig({
  testDir: "./apps/web/e2e",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: "http://127.0.0.1:3010",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    // Signs in once and saves the cookie jar; the API fails closed now, so
    // without this every spec would meet the login screen instead of the app.
    { name: "setup", testMatch: /auth\.setup\.ts/ },
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], storageState: STORAGE_STATE },
      dependencies: ["setup"],
    },
  ],
  webServer: [
    {
      command: ".venv/bin/python apps/api/scripts/serve_e2e.py",
      url: "http://127.0.0.1:8010/health",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command:
        "NEXT_DIST_DIR=.next-e2e NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx --yes pnpm@9.15.9 --filter @workspace/web exec next dev -p 3010",
      url: "http://127.0.0.1:3010",
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
