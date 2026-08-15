"use client";

import type { InvitePreview } from "@workspace/api-client";
import { CircleAlert, ShieldCheck, Users } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { authError } from "./errors";
import { signInUrlFor } from "./next-path";
import { useSession } from "./session-provider";

/**
 * The destination of an invitation link.
 *
 * Unlike the other token pages this one does *not* redeem on arrival. Two
 * reasons, and they are the whole design:
 *
 * 1. The invitee may have no account here. Accepting writes a `Membership`,
 *    which names a user, so there has to be a signed-in user first — and they
 *    cannot sensibly be asked to create one without first being told what they
 *    are joining. So the page reads the invitation (unauthenticated) and
 *    renders it before asking for anything.
 * 2. Joining somebody's workspace is a decision. A link that silently enrols
 *    whoever opens it — including the person who was forwarded it by mistake —
 *    is worse for them and worse for the workspace.
 *
 * The API refuses an account whose address is not the invited one, so the page
 * says which address is expected rather than letting the wrong account fail at
 * the end of the flow.
 */
export function InviteRoute() {
  const router = useRouter();
  const { status, session } = useSession();
  const [token, setToken] = useState<string | null>(null);
  const [preview, setPreview] = useState<InvitePreview | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setToken(new URLSearchParams(window.location.search).get("token") || "");
  }, []);

  useEffect(() => {
    if (token === null) return;
    if (!token) {
      setError("That invitation link is missing its token.");
      return;
    }
    void api
      .previewInvite(token)
      .then(setPreview)
      .catch((caught) =>
        setError(authError(caught, "That invitation link is not valid.")),
      );
  }, [token]);

  const accept = useCallback(async () => {
    if (!token || busy) return;
    setBusy(true);
    setError("");
    try {
      const accepted = await api.acceptInvite(token);
      // Straight into the workspace they just joined. The switcher reads
      // memberships on load, so it will already list it.
      api.setWorkspaceId(accepted.workspace_id);
      router.replace("/");
    } catch (caught) {
      setError(authError(caught, "That invitation could not be accepted."));
      setBusy(false);
    }
  }, [token, busy, router]);

  const shell = (children: React.ReactNode) => (
    <div className="auth-shell">
      <div className="auth-stage">
        <div className="auth-brand">Jasmine</div>
        <div className="auth-card">{children}</div>
      </div>
    </div>
  );

  if (error && !preview) {
    return shell(
      <>
        <div className="auth-sent">
          <CircleAlert size={22} />
          <h1>That link did not work</h1>
          <p>{error}</p>
        </div>
        <Link className="auth-link" href="/auth/login">
          Continue to sign in
        </Link>
      </>,
    );
  }

  if (!preview || status === "loading") {
    return shell(
      <div className="auth-sent">
        <Users size={22} />
        <h1>Invitation</h1>
        <p>Reading the invitation…</p>
      </div>,
    );
  }

  if (preview.status !== "pending") {
    // The API tells the truth about which of the three it is, because reaching
    // it required the token — there is no address to enumerate here.
    const reason = {
      accepted: "This invitation has already been accepted.",
      revoked: "This invitation was withdrawn.",
      expired: "This invitation has expired. Ask for a new one.",
      pending: "",
    }[preview.status];
    return shell(
      <>
        <div className="auth-sent">
          <CircleAlert size={22} />
          <h1>Invitation to {preview.workspace_name}</h1>
          <p>{reason}</p>
        </div>
        <Link className="auth-link" href="/auth/login">
          Continue to sign in
        </Link>
      </>,
    );
  }

  const invitation = (
    <div className="auth-sent">
      <Users size={22} />
      <h1>Join {preview.workspace_name}</h1>
      <p>
        {preview.invited_by_name || "Someone"} invited{" "}
        <strong>{preview.email}</strong> to {preview.workspace_name} as a{" "}
        {preview.role}.
      </p>
    </div>
  );

  if (status !== "authenticated" || !session) {
    return shell(
      <>
        {invitation}
        <p className="field-hint invite-note">
          Sign in as {preview.email} — or create an account with that address —
          and you will come straight back here.
        </p>
        <Link
          className="auth-link"
          href={signInUrlFor(
            `/auth/invite?token=${encodeURIComponent(token || "")}`,
          )}
        >
          Sign in to accept
        </Link>
      </>,
    );
  }

  if (session.user_email !== preview.email) {
    // Checked here only so the person is told before they click; the API is
    // what enforces it, and it refuses this pairing with a 403.
    return shell(
      <>
        {invitation}
        <p className="auth-error" role="alert">
          You are signed in as {session.user_email}. This invitation can only be
          accepted by {preview.email}.
        </p>
        <Link className="auth-link" href="/auth/login">
          Sign in as somebody else
        </Link>
      </>,
    );
  }

  return shell(
    <>
      {invitation}
      {error && (
        <p className="auth-error" role="alert">
          {error}
        </p>
      )}
      <button
        className="auth-submit"
        type="button"
        disabled={busy}
        onClick={() => void accept()}
      >
        <ShieldCheck size={14} /> {busy ? "Joining…" : `Join ${preview.workspace_name}`}
      </button>
      <footer className="auth-foot">
        <Link className="auth-link" href="/">
          Not now
        </Link>
      </footer>
    </>,
  );
}
