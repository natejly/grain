"use client";

import { AuthScreen, AuthSplash } from "./auth-screen";
import { useSession } from "./session-provider";

/**
 * Nothing authenticated renders until the session resolves, and no app chrome
 * renders without one. `children` is an element passed down from the server
 * component, so the workspace does not mount — and fires no requests — while
 * this is showing the splash or the login form.
 */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const { status, adopt, refresh } = useSession();

  if (status === "authenticated") return <>{children}</>;
  if (status === "loading") return <AuthSplash />;

  return (
    <AuthScreen
      offline={status === "offline"}
      onSignedIn={adopt}
      onRecovered={() => void refresh()}
    />
  );
}
