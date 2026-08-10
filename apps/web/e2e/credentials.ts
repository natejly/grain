import path from "node:path";

/**
 * The account the browser suite shares, seeded by apps/api/scripts/serve_e2e.py.
 * It owns the demo workspace the existing specs rely on (its agent, its
 * github-zen tool grant, the sources they hand each other), so they sign in as
 * it rather than each signing up an empty tenant.
 */
export const DEMO_EMAIL = "demo@example.com";
export const DEMO_PASSWORD = "e2e-demo-password";

/** Where the setup project parks the signed-in cookie jar. */
export const STORAGE_STATE = path.join(__dirname, ".auth", "user.json");
