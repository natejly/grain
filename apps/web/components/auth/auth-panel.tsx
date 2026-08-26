"use client";

import type { AuthSession } from "@workspace/api-client";
import { ArrowLeft, MailCheck } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

type Mode = "signin" | "signup" | "forgot" | "link" | "sent";

export type AuthPanelProps = {
  /** Called once the API has issued a session. */
  onSignedIn: (session: AuthSession) => void;
  /** A message from somewhere else — a failed OAuth round trip, say. */
  notice?: string;
};

/**
 * Google's mark, inline so the strict CSP never has to allow a remote image.
 */
function GoogleMark() {
  return (
    <svg width="15" height="15" viewBox="0 0 48 48" aria-hidden="true">
      <path
        fill="#4285F4"
        d="M45 24c0-1.6-.1-2.7-.4-3.9H24v7.1h12c-.2 1.8-1.5 4.6-4.4 6.4l6.7 5.2C42.2 35.1 45 30 45 24z"
      />
      <path
        fill="#34A853"
        d="M24 46c5.9 0 10.9-2 14.5-5.3l-6.9-5.3c-1.8 1.3-4.3 2.2-7.6 2.2-5.8 0-10.7-3.8-12.5-9.1l-7.1 5.5C8.1 41.1 15.4 46 24 46z"
      />
      <path
        fill="#FBBC05"
        d="M11.5 28.5c-.5-1.4-.7-2.9-.7-4.5s.3-3.1.7-4.5l-7.1-5.5C2.9 16.9 2 20.3 2 24s.9 7.1 2.4 10l7.1-5.5z"
      />
      <path
        fill="#EA4335"
        d="M24 10.6c4.1 0 6.9 1.8 8.5 3.3l6.2-6C34.9 4.4 29.9 2 24 2 15.4 2 8.1 6.9 4.4 14l7.1 5.5c1.8-5.3 6.7-8.9 12.5-8.9z"
      />
    </svg>
  );
}

export function AuthPanel({ onSignedIn, notice = "" }: AuthPanelProps) {
  const [mode, setMode] = useState<Mode>("signin");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState({ title: "", body: "" });
  const [googleReady, setGoogleReady] = useState(false);
  const [devOverride, setDevOverride] = useState<{ enabled: boolean; handle: string }>({
    enabled: false,
    handle: "",
  });
  const [playground, setPlayground] = useState(false);

  // The button is hidden rather than rendered-and-broken: the API answers 503
  // when no login client is configured, which is the only way to know.
  useEffect(() => {
    let cancelled = false;
    void api.googleLoginAvailable().then((ready) => {
      if (!cancelled) setGoogleReady(ready);
    });
    void api.devOverride().then((override) => {
      if (!cancelled) setDevOverride(override);
    });
    // Same discovery-before-session pattern: the "Try it" button appears only
    // when the deployment opted anonymous playground mode in.
    void api.playgroundAvailable().then((ok) => {
      if (!cancelled) setPlayground(ok);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  function go(next: Mode) {
    setMode(next);
    setError("");
    setPassword("");
  }

  async function continueAsDev() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      onSignedIn(await api.devLogin());
    } catch (caught) {
      setError(describeError(caught, "Dev sign-in failed."));
    } finally {
      setBusy(false);
    }
  }

  async function tryPlayground() {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      onSignedIn(await api.playgroundLogin());
    } catch (caught) {
      setError(describeError(caught, "Could not open the playground."));
    } finally {
      setBusy(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      if (mode === "signin") {
        onSignedIn(await api.login(email.trim(), password));
        return;
      }
      if (mode === "signup") {
        // 202 whether or not the address is taken. Rendering the API's own
        // sentence keeps this page from becoming an account-enumeration oracle.
        const ack = await api.signup(email.trim(), password, name.trim());
        setSent({ title: "Check your email", body: ack.detail });
        setPassword("");
        setMode("sent");
        return;
      }
      if (mode === "link") {
        // Same non-oracle discipline as reset: one acknowledgement, rendered
        // verbatim, whether or not the address has an account.
        const ack = await api.requestLoginLink(email.trim());
        setSent({ title: "Check your email", body: ack.detail });
        setMode("sent");
        return;
      }
      const ack = await api.requestPasswordReset(email.trim());
      setSent({ title: "Check your email", body: ack.detail });
      setMode("sent");
    } catch (caught) {
      setError(describeError(caught, "Something went wrong. Try again."));
    } finally {
      setBusy(false);
    }
  }

  if (mode === "sent") {
    return (
      <div className="auth-card">
        <div className="auth-sent">
          <MailCheck size={22} />
          <h1>{sent.title}</h1>
          <p>{sent.body}</p>
        </div>
        <button className="auth-link" onClick={() => go("signin")}>
          <ArrowLeft size={13} /> Back to sign in
        </button>
      </div>
    );
  }

  const heading =
    mode === "signin"
      ? "Sign in"
      : mode === "signup"
        ? "Create your workspace"
        : mode === "link"
          ? "Sign in with a link"
          : "Reset your password";

  const showDevOverride = mode === "signin" && devOverride.enabled && !!devOverride.handle;
  const handleLabel =
    (devOverride.handle.slice(0, 1).toUpperCase() + devOverride.handle.slice(1)) || "dev";

  return (
    <div className="auth-card">
      <header className="auth-head">
        <h1>{heading}</h1>
        {mode === "forgot" && <p>We will email a link to set a new password.</p>}
        {mode === "link" && <p>We will email a one-time link that signs you in.</p>}
      </header>

      {notice && (
        <p className="auth-error" role="alert">
          {notice}
        </p>
      )}

      {showDevOverride && (
        <>
          <button
            type="button"
            className="auth-submit"
            disabled={busy}
            onClick={() => void continueAsDev()}
          >
            {busy ? "Working…" : `Continue as ${handleLabel}`}
          </button>
          <div className="auth-divider">
            <span>or</span>
          </div>
        </>
      )}

      {mode === "signin" && playground && (
        <>
          <button
            type="button"
            className="auth-google"
            disabled={busy}
            onClick={() => void tryPlayground()}
          >
            {busy ? "Working…" : "Try the playground"}
          </button>
          <div className="auth-divider">
            <span>or</span>
          </div>
        </>
      )}

      {googleReady && mode !== "forgot" && (
        <>
          <button
            type="button"
            className="auth-google"
            onClick={() => {
              // Top-level navigation: the endpoint sets the OAuth state cookie
              // and Google will not render inside a fetch or an iframe.
              window.location.href = api.googleLoginUrl();
            }}
          >
            <GoogleMark />
            Continue with Google
          </button>
          <div className="auth-divider">
            <span>or</span>
          </div>
        </>
      )}

      <form className="auth-form" onSubmit={(event) => void submit(event)}>
        {mode === "signup" && (
          <label>
            Name
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            name="name"
            autoComplete="name"
          />
          </label>
        )}

        <label>
          Email
          <input
            type="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            name="email"
            autoComplete="email"
            required
          />
        </label>

        {mode !== "forgot" && mode !== "link" && (
          <label>
            Password
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              name="password"
              autoComplete={mode === "signup" ? "new-password" : "current-password"}
            required
          />
          {mode === "signup" && null}
        </label>
        )}

        {error && (
          <p className="auth-error" role="alert">
            {error}
          </p>
        )}

        <button type="submit" className="auth-submit" disabled={busy}>
          {busy
            ? "Working…"
            : mode === "signin"
              ? "Sign in"
              : mode === "signup"
                ? "Create account"
                : mode === "link"
                  ? "Send sign-in link"
                  : "Send reset link"}
        </button>
      </form>

      <footer className="auth-foot">
        {mode === "signin" && (
          <>
            <button className="auth-link" onClick={() => go("link")}>
              Email me a sign-in link
            </button>
            <button className="auth-link" onClick={() => go("forgot")}>
              Forgot your password?
            </button>
            <button className="auth-link" onClick={() => go("signup")}>
              Create an account
            </button>
          </>
        )}
        {mode === "signup" && (
          <button className="auth-link" onClick={() => go("signin")}>
            Already have an account? Sign in
          </button>
        )}
        {(mode === "forgot" || mode === "link") && (
          <button className="auth-link" onClick={() => go("signin")}>
            <ArrowLeft size={13} /> Back to sign in
          </button>
        )}
      </footer>
    </div>
  );
}
