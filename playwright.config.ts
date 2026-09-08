import { defineConfig, devices } from "@playwright/test";
import { existsSync } from "node:fs";
import { STORAGE_STATE } from "./apps/web/e2e/credentials";

/**
 * The interpreter that runs the API under test.
 *
 * `.venv/bin/python` is the repo's own virtualenv and the right answer on a
 * developer machine — but it is not the only place a working interpreter lives,
 * and hardcoding it made two environments unable to run this suite at all:
 *
 *   - CI installs the api package with `pip install -e` into the interpreter
 *     actions/setup-python put on PATH. There is no `.venv` there, so the
 *     webServer command exited 127 and every spec failed as
 *     "Process from config.webServer was not able to start" — a message that
 *     says nothing about a missing interpreter.
 *   - git worktrees do not get their own `.venv`, so the suite could not be run
 *     from one without symlinking the venv in by hand.
 *
 * Fall back to PATH, and let E2E_PYTHON override for anyone whose interpreter is
 * somewhere else again. Checked with `existsSync` rather than assumed, because
 * the venv being present is precisely what varies.
 */
const API_PYTHON =
  process.env.E2E_PYTHON ??
  (existsSync(".venv/bin/python") ? ".venv/bin/python" : "python3");

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
      command: `${API_PYTHON} apps/api/scripts/serve_e2e.py`,
      url: "http://127.0.0.1:8010/health",
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      // `sandbox-assets` first, which is not optional and not incidental.
      // public/sandbox/ is generated from node_modules and gitignored, so a
      // fresh clone or a new worktree has none of it, and the sandbox frame
      // serves a 404 for the React runtime it inlines — subject-chat.spec.ts
      // fails there and nowhere else. The package's own `dev` and `build`
      // scripts both run it for this reason; only this config skipped it, and
      // CI stayed green solely because `pnpm build` happens to run earlier in
      // the same job. That made the suite pass on the machines that had
      // already built and fail on the ones that had not, which reads as
      // flakiness rather than as a missing step.
      //
      // Spelled out here rather than by calling the `dev` script, because
      // pnpm appends forwarded args to the END of a script: `dev -p 3010`
      // would put the port on whatever command that script happens to end
      // with today.
      command:
        "npx --yes pnpm@9.15.9 --filter @workspace/web run sandbox-assets && NEXT_DIST_DIR=.next-e2e NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx --yes pnpm@9.15.9 --filter @workspace/web exec next dev -p 3010",
      url: "http://127.0.0.1:3010",
      reuseExistingServer: false,
      // Bundling the runtime with esbuild-wasm happens before the server
      // starts listening, and it is the cold-cache case that needs the room.
      timeout: 120_000,
    },
  ],
});
