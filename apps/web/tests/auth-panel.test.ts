import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { act, createElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * The auth screen's own logic — which mode is showing, what it renders back to
 * the visitor, and which optional buttons the discovery calls unlock. None of
 * this touches the network in earnest; the point of the tests is that the panel
 * treats the API's acknowledgement as opaque (renders the sentence, never reads
 * an outcome out of it) and shows the credential-free "Try the playground" door
 * only when the deployment reported it enabled.
 */

const googleLoginAvailable = vi.fn();
const devOverride = vi.fn();
const playgroundAvailable = vi.fn();
const playgroundLogin = vi.fn();
const requestLoginLink = vi.fn();
const requestPasswordReset = vi.fn();
const signup = vi.fn();
const login = vi.fn();
const googleLoginUrl = vi.fn();

vi.mock("../components/api", () => ({
  api: {
    googleLoginAvailable: (...a: unknown[]) => googleLoginAvailable(...a),
    devOverride: (...a: unknown[]) => devOverride(...a),
    playgroundAvailable: (...a: unknown[]) => playgroundAvailable(...a),
    playgroundLogin: (...a: unknown[]) => playgroundLogin(...a),
    requestLoginLink: (...a: unknown[]) => requestLoginLink(...a),
    requestPasswordReset: (...a: unknown[]) => requestPasswordReset(...a),
    signup: (...a: unknown[]) => signup(...a),
    login: (...a: unknown[]) => login(...a),
    googleLoginUrl: (...a: unknown[]) => googleLoginUrl(...a),
  },
}));

import { AuthPanel } from "../components/auth/auth-panel";

const SESSION = {
  user_id: "u1",
  user_name: "Ada",
  user_email: "ada@example.test",
  email_verified: true,
  workspace_id: "w1",
  workspace_name: "Playground",
  role: "member",
  csrf_token: "csrf",
};

beforeEach(() => {
  vi.clearAllMocks();
  // Discovery defaults: every optional door closed. Individual tests open one.
  googleLoginAvailable.mockResolvedValue(false);
  devOverride.mockResolvedValue({ enabled: false, handle: "" });
  playgroundAvailable.mockResolvedValue(false);
  googleLoginUrl.mockReturnValue("https://example.test/oauth");
});

afterEach(cleanup);

/** Render the panel and let the three discovery promises settle. */
async function mount() {
  const onSignedIn = vi.fn();
  render(createElement(AuthPanel, { onSignedIn }));
  // The discovery effect fires three resolved promises; flush their microtasks
  // so state that gates optional buttons has landed before any assertion.
  await act(async () => {
    await Promise.resolve();
  });
  return { onSignedIn };
}

function heading() {
  return screen.getByRole("heading", { level: 1 }).textContent;
}

function submitLabel() {
  return screen.getByRole("button", { name: /^(Sign in|Create account|Send sign-in link|Send reset link|Working)/ });
}

describe("auth panel — mode switching", () => {
  it("opens on sign-in with a password field and no name field", async () => {
    await mount();
    expect(heading()).toBe("Sign in");
    expect(screen.getByLabelText("Password")).toBeTruthy();
    expect(screen.queryByLabelText("Name")).toBeNull();
  });

  it("switches to passwordless link mode, dropping the password field", async () => {
    await mount();
    act(() => screen.getByRole("button", { name: "Email me a sign-in link" }).click());

    expect(heading()).toBe("Sign in with a link");
    // A password field in a passwordless flow is a lie about what's required;
    // the mode must remove it, not merely ignore it.
    expect(screen.queryByLabelText("Password")).toBeNull();
    expect(screen.getByLabelText("Email")).toBeTruthy();
    expect(submitLabel().textContent).toBe("Send sign-in link");
  });

  it("switches to forgot-password mode, also without a password field", async () => {
    await mount();
    act(() => screen.getByRole("button", { name: "Forgot your password?" }).click());

    expect(heading()).toBe("Reset your password");
    expect(screen.queryByLabelText("Password")).toBeNull();
    expect(submitLabel().textContent).toBe("Send reset link");
  });

  it("switches to sign-up mode, adding a name field", async () => {
    await mount();
    act(() => screen.getByRole("button", { name: "Create an account" }).click());

    expect(heading()).toBe("Create your workspace");
    expect(screen.getByLabelText("Name")).toBeTruthy();
    expect(screen.getByLabelText("Password")).toBeTruthy();
    expect(submitLabel().textContent).toBe("Create account");
  });

  it("returns to sign-in from link mode via Back to sign in", async () => {
    await mount();
    act(() => screen.getByRole("button", { name: "Email me a sign-in link" }).click());
    expect(heading()).toBe("Sign in with a link");

    act(() => screen.getByRole("button", { name: /Back to sign in/ }).click());
    expect(heading()).toBe("Sign in");
    expect(screen.getByLabelText("Password")).toBeTruthy();
  });
});

describe("auth panel — acknowledgement rendering", () => {
  it("renders the login-link acknowledgement verbatim without reading an outcome", async () => {
    const detail = "If that address has an account, a sign-in link is on its way.";
    requestLoginLink.mockResolvedValue({ status: "ok", detail });
    await mount();

    act(() => screen.getByRole("button", { name: "Email me a sign-in link" }).click());
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "  ada@example.test  " } });
    const form = document.querySelector("form")!;
    await act(async () => {
      fireEvent.submit(form);
    });

    // The email is trimmed on the way out, and the API's exact sentence is what
    // the visitor reads back — no "we found your account" branch anywhere.
    expect(requestLoginLink).toHaveBeenCalledWith("ada@example.test");
    expect(await screen.findByText(detail)).toBeTruthy();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Check your email");
  });

  it("renders the password-reset acknowledgement through the same sent view", async () => {
    const detail = "If that address has an account, reset instructions are on the way.";
    requestPasswordReset.mockResolvedValue({ status: "ok", detail });
    await mount();

    act(() => screen.getByRole("button", { name: "Forgot your password?" }).click());
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "ada@example.test" } });
    await act(async () => {
      fireEvent.submit(document.querySelector("form")!);
    });

    expect(await screen.findByText(detail)).toBeTruthy();
    // Same acknowledgement surface regardless of which non-oracle request fed it.
    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe("Check your email");
  });

  it("keeps the acknowledgement identical whatever the API's sentence says", async () => {
    // Two different bodies flow straight through, proving the panel is a pipe
    // for the acknowledgement rather than an enumeration oracle of its own.
    const a = "Body A: check your inbox.";
    requestLoginLink.mockResolvedValueOnce({ status: "ok", detail: a });
    await mount();

    act(() => screen.getByRole("button", { name: "Email me a sign-in link" }).click());
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "nobody@example.test" } });
    await act(async () => {
      fireEvent.submit(document.querySelector("form")!);
    });
    expect(await screen.findByText(a)).toBeTruthy();
  });
});

describe("auth panel — playground button gating", () => {
  it("hides the playground button when discovery reports it disabled", async () => {
    playgroundAvailable.mockResolvedValue(false);
    await mount();
    expect(screen.queryByRole("button", { name: "Try the playground" })).toBeNull();
  });

  it("shows the playground button on sign-in when discovery reports it enabled", async () => {
    playgroundAvailable.mockResolvedValue(true);
    await mount();
    expect(await screen.findByRole("button", { name: "Try the playground" })).toBeTruthy();
  });

  it("hides the playground button outside sign-in even when enabled", async () => {
    playgroundAvailable.mockResolvedValue(true);
    await mount();
    expect(await screen.findByRole("button", { name: "Try the playground" })).toBeTruthy();

    // Leaving sign-in retires the credential-free door: it belongs to the
    // landing mode, not the link/forgot/signup flows.
    act(() => screen.getByRole("button", { name: "Email me a sign-in link" }).click());
    expect(screen.queryByRole("button", { name: "Try the playground" })).toBeNull();
  });

  it("mints a session through playgroundLogin when the button is pressed", async () => {
    playgroundAvailable.mockResolvedValue(true);
    playgroundLogin.mockResolvedValue(SESSION);
    const { onSignedIn } = await mount();

    const button = await screen.findByRole("button", { name: "Try the playground" });
    await act(async () => {
      button.click();
    });

    await waitFor(() => expect(onSignedIn).toHaveBeenCalledWith(SESSION));
    expect(playgroundLogin).toHaveBeenCalledTimes(1);
  });
});
