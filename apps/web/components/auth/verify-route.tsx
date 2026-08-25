"use client";

import { CircleAlert, MailCheck } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { authError } from "./errors";

type Outcome = { kind: "working" | "done" | "failed"; message: string };

/** The destination of the "Confirm your email" link. */
export function VerifyRoute() {
  const [outcome, setOutcome] = useState<Outcome>({
    kind: "working",
    message: "Confirming your address…",
  });
  // The token is single-use, so React's development double-effect must not
  // spend it and then report the second, already-consumed attempt as a failure.
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = new URLSearchParams(window.location.search).get("token") || "";
    if (!token) {
      setOutcome({ kind: "failed", message: "That link is missing its token." });
      return;
    }
    void api
      .verifyEmail(token)
      .then((ack) => setOutcome({ kind: "done", message: ack.detail }))
      .catch((caught) =>
        setOutcome({
          kind: "failed",
          message: authError(caught, "That link is no longer valid."),
        }),
      );
  }, []);

  return (
    <div className="auth-shell">
      <div className="auth-stage">
        <div className="auth-brand">Grain <span className="auth-brand-byline">by Rice Labs</span></div>
        <div className="auth-card">
          <div className="auth-sent">
            {outcome.kind === "failed" ? (
              <CircleAlert size={22} />
            ) : (
              <MailCheck size={22} />
            )}
            <h1>
              {outcome.kind === "done"
                ? "Email confirmed"
                : outcome.kind === "failed"
                  ? "That link did not work"
                  : "Confirm your email"}
            </h1>
            <p>{outcome.message}</p>
          </div>
          {outcome.kind !== "working" && (
            <Link className="auth-link" href="/auth/login">
              Continue to sign in
            </Link>
          )}
        </div>
      </div>
    </div>
  );
}
