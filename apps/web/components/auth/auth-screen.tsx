"use client";

import type { AuthSession } from "@workspace/api-client";
import { useEffect, useState } from "react";
import { api } from "../api";
import { ApiHealthBanner } from "../api-health-banner";
import { AuthPanel } from "./auth-panel";

/**
 * `GET /api/auth/google/callback` bounces failures back to the web app as
 * `/?auth_error=<reason>` — it is a top-level navigation, so a JSON body would
 * strand the user on the API's domain. These are the reasons it can send.
 */
const OAUTH_ERRORS: Record<string, string> = {
  state: "That Google sign-in link expired before it was used. Try again.",
  denied: "Google sign-in was cancelled.",
  exchange: "Google could not confirm that sign-in. Try again.",
  account_exists:
    "That address already has a Fieldnote password. Sign in with your password instead.",
  account: "That account is not available. Contact whoever owns the workspace.",
  no_email: "Google did not share an email address, so there is nothing to sign in as.",
};

/** The only thing on screen while the session is still resolving. */
export function AuthSplash({ message = "Restoring your session…" }: { message?: string }) {
  return (
    <div className="auth-shell">
      <div className="auth-stage">
        <div className="auth-brand">Fieldnote</div>
        <p className="auth-splash">{message}</p>
      </div>
    </div>
  );
}

export type AuthScreenProps = {
  /** True when the API is unreachable — which is not the same as signed out. */
  offline: boolean;
  onSignedIn: (session: AuthSession) => void;
  onRecovered: () => void;
};

export function AuthScreen({ offline, onSignedIn, onRecovered }: AuthScreenProps) {
  const [notice, setNotice] = useState("");

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const reason = params.get("auth_error");
    if (!reason) return;
    // Strip it so a reload does not re-accuse a sign-in that already failed.
    window.history.replaceState(null, "", window.location.pathname);
    setNotice(OAUTH_ERRORS[reason] || "Google sign-in did not complete. Try again.");
  }, []);

  return (
    <div className="auth-shell">
      <ApiHealthBanner api={api} onRecovered={onRecovered} />
      <div className="auth-stage">
        <div className="auth-brand">Fieldnote</div>
        {offline ? (
          <div className="auth-card">
            <div className="auth-sent">
              <h1>Reconnecting</h1>
              <p>
                Signing in needs the API. This page picks up as soon as it answers
                again.
              </p>
            </div>
          </div>
        ) : (
          <AuthPanel onSignedIn={onSignedIn} notice={notice} />
        )}
      </div>
    </div>
  );
}
