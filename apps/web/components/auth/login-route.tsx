"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";
import { AuthScreen, AuthSplash } from "./auth-screen";
import { useSession } from "./session-provider";

/**
 * The standalone /auth/login page the account emails link to. The workspace at
 * "/" already falls back to this screen when there is no session, so this route
 * exists for the links — and sends an already-signed-in visitor onward instead
 * of asking them to sign in twice.
 */
export function LoginRoute() {
  const router = useRouter();
  const { status, adopt, refresh } = useSession();

  useEffect(() => {
    if (status === "authenticated") router.replace("/");
  }, [status, router]);

  if (status === "loading") return <AuthSplash />;
  if (status === "authenticated") return <AuthSplash message="Opening your workspace…" />;

  return (
    <AuthScreen
      offline={status === "offline"}
      onSignedIn={(session) => {
        adopt(session);
        router.replace("/");
      }}
      onRecovered={() => void refresh()}
    />
  );
}
