"use client";

import { KeyRound } from "lucide-react";
import Link from "next/link";
import { FormEvent, useEffect, useState } from "react";
import { api } from "../api";
import { authError } from "./errors";

/**
 * The destination of the "Reset your password" link. Confirming revokes every
 * session the account had, so there is nowhere to go from here but sign-in.
 */
export function ResetRoute() {
  const [token, setToken] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState("");

  useEffect(() => {
    setToken(new URLSearchParams(window.location.search).get("token") || "");
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || !token) return;
    if (password !== confirm) {
      setError("Those two passwords do not match.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const ack = await api.confirmPasswordReset(token, password);
      setDone(ack.detail);
    } catch (caught) {
      setError(authError(caught, "That reset link is no longer valid."));
    } finally {
      setBusy(false);
      setPassword("");
      setConfirm("");
    }
  }

  return (
    <div className="auth-shell">
      <div className="auth-stage">
        <div className="auth-brand">Grain <span className="auth-brand-byline">by Rice Labs</span></div>
        <div className="auth-card">
          {done ? (
            <>
              <div className="auth-sent">
                <KeyRound size={22} />
                <h1>Password updated</h1>
                <p>{done}</p>
              </div>
              <Link className="auth-link" href="/auth/login">
                Continue to sign in
              </Link>
            </>
          ) : (
            <>
              <header className="auth-head">
                <h1>Choose a new password</h1>
                <p>Every device signed in to this account will be signed out.</p>
              </header>

              {token === "" && (
                <p className="auth-error" role="alert">
                  That link is missing its token. Request a new one from the sign-in
                  page.
                </p>
              )}

              <form className="auth-form" onSubmit={(event) => void submit(event)}>
                <label>
                  New password
                  <input
                    type="password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    name="new-password"
                    autoComplete="new-password"
                    required
                  />
                </label>
                <label>
                  Confirm password
                  <input
                    type="password"
                    value={confirm}
                    onChange={(event) => setConfirm(event.target.value)}
                    name="confirm-password"
                    autoComplete="new-password"
                    required
                  />
                </label>

                {error && (
                  <p className="auth-error" role="alert">
                    {error}
                  </p>
                )}

                <button
                  type="submit"
                  className="auth-submit"
                  disabled={busy || !token}
                >
                  {busy ? "Working…" : "Set new password"}
                </button>
              </form>

              <footer className="auth-foot">
                <Link className="auth-link" href="/auth/login">
                  Back to sign in
                </Link>
              </footer>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
