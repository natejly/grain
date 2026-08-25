"use client";

import { MailCheck } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { authError } from "./errors";
import { useSession } from "./session-provider";

/**
 * The destination of an emailed magic-link. Unlike the reset route it does not
 * ask for anything: redeeming the token *is* the sign-in, so it mints a session
 * on arrival and forwards to the workspace. On failure it stops on a plain
 * message with a way back — never guessing why, since the API returns the same
 * generic 400 for an expired, spent, or unknown token.
 */
export function LoginLinkRoute() {
  const router = useRouter();
  const { adopt } = useSession();
  const [error, setError] = useState("");
  // The link is single-use, so redemption must fire exactly once. React's dev
  // double-mount would otherwise spend the token, then 400 the second attempt
  // and show a spurious "no longer valid" on a link that just worked.
  const spent = useRef(false);

  useEffect(() => {
    if (spent.current) return;
    spent.current = true;
    const token = new URLSearchParams(window.location.search).get("token") || "";
    if (!token) {
      setError(
        "That sign-in link is missing its token. Request a new one from the sign-in page.",
      );
      return;
    }
    void (async () => {
      try {
        const session = await api.consumeLoginLink(token);
        adopt(session);
        router.replace("/");
      } catch (caught) {
        setError(authError(caught, "That sign-in link is no longer valid."));
      }
    })();
  }, [adopt, router]);

  return (
    <div className="auth-shell">
      <div className="auth-stage">
        <div className="auth-brand">Grain <span className="auth-brand-byline">by Rice Labs</span></div>
        <div className="auth-card">
          {error ? (
            <>
              <header className="auth-head">
                <h1>Sign-in link</h1>
              </header>
              <p className="auth-error" role="alert">
                {error}
              </p>
              <footer className="auth-foot">
                <Link className="auth-link" href="/auth/login">
                  Back to sign in
                </Link>
              </footer>
            </>
          ) : (
            <div className="auth-sent">
              <MailCheck size={22} />
              <h1>Signing you in…</h1>
              <p>Opening your workspace.</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
