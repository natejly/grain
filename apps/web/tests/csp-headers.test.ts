import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * The document CSP is environment-gated: production is 'self'-only, and only a
 * Vercel preview build may name the preview toolbar's origins. The split is a
 * security boundary — a regression that let vercel.live into the production
 * policy would widen script-src on the live site — so it is pinned in both
 * directions, and pinned against the one input that must NOT open it: an unset
 * VERCEL_ENV (a non-Vercel build) has to fall on the strict side.
 */
async function cspFor(vercelEnv: string | undefined): Promise<string> {
  vi.resetModules();
  if (vercelEnv === undefined) vi.stubEnv("VERCEL_ENV", "");
  else vi.stubEnv("VERCEL_ENV", vercelEnv);
  const mod = await import("../next.config");
  const headers = await mod.default.headers!();
  const csp = headers[0].headers.find(
    (h) => h.key === "Content-Security-Policy",
  )?.value;
  if (!csp) throw new Error("no CSP header");
  return csp;
}

afterEach(() => vi.unstubAllEnvs());

describe("document CSP toolbar gating", () => {
  it("keeps production 'self'-only — no vercel.live anywhere", async () => {
    const csp = await cspFor("production");
    expect(csp).not.toContain("vercel.live");
    expect(csp).not.toContain("pusher.com");
    expect(csp).toContain("script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'");
  });

  it("stays strict when VERCEL_ENV is unset (non-Vercel build)", async () => {
    const csp = await cspFor(undefined);
    expect(csp).not.toContain("vercel.live");
  });

  it("allows the toolbar origins on a preview build", async () => {
    const csp = await cspFor("preview");
    expect(csp).toContain("script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' https://vercel.live");
    expect(csp).toContain("wss://*.pusher.com");
    // The toolbar origins ride the existing directives, never default-src.
    expect(csp).toContain("frame-src 'self' blob:");
    expect(csp).toMatch(/frame-src[^;]*https:\/\/vercel\.live/);
  });
});
