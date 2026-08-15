"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { AuthScreen, AuthSplash } from "./auth-screen";
import { DEFAULT_NEXT, nextPathFrom } from "./next-path";
import { useSession } from "./session-provider";

/**
 * The standalone /auth/login page the account emails link to. The workspace at
 * "/" already falls back to this screen when there is no session, so this route
 * exists for the links — and sends an already-signed-in visitor onward instead
 * of asking them to sign in twice.
 *
 * `?next=` exists for the invitation flow, where signing in is a step in the
 * middle of something rather than the destination: the invitee arrives holding
 * a token in a URL, and sending them to "/" afterwards would mean finding the
 * email again. It is fenced by `safeNextPath` — a redirect target read out of a
 * query string is an open redirect otherwise.
 */
export function LoginRoute() {
  const router = useRouter();
  const { status, adopt, refresh } = useSession();
  // Read on mount rather than during render: `window` does not exist while this
  // is being server-rendered, and the value cannot change without a navigation.
  const [next, setNext] = useState(DEFAULT_NEXT);

  useEffect(() => {
    setNext(nextPathFrom(window.location.search));
  }, []);

  useEffect(() => {
    if (status === "authenticated") router.replace(next);
  }, [status, router, next]);

  if (status === "loading") return <AuthSplash />;
  if (status === "authenticated") return <AuthSplash message="Opening your workspace…" />;

  return (
    <AuthScreen
      offline={status === "offline"}
      onSignedIn={(session) => {
        adopt(session);
        router.replace(next);
      }}
      onRecovered={() => void refresh()}
    />
  );
}
