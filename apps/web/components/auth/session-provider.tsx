"use client";

import { ApiError, type AuthSession } from "@workspace/api-client";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import { api } from "../api";

/**
 * `offline` is deliberately not folded into `anonymous`: an API nobody can
 * reach has not told us the user is signed out, and showing a login form would
 * invite a password that cannot be checked. It gets the API-down banner
 * instead, which retries and resolves the session on its own.
 */
export type SessionStatus = "loading" | "authenticated" | "anonymous" | "offline";

export type SessionValue = {
  status: SessionStatus;
  session: AuthSession | null;
  /** Re-read GET /api/auth/me; resolves to null when signed out. */
  refresh: () => Promise<AuthSession | null>;
  /** Take a session the login form already holds, without a second round trip. */
  adopt: (session: AuthSession) => void;
  signOut: () => Promise<void>;
};

const SessionContext = createContext<SessionValue | null>(null);

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<SessionStatus>("loading");
  const [session, setSession] = useState<AuthSession | null>(null);

  const refresh = useCallback(async () => {
    try {
      const next = await api.me();
      setSession(next);
      setStatus("authenticated");
      return next;
    } catch (caught) {
      setSession(null);
      setStatus(caught instanceof ApiError && caught.offline ? "offline" : "anonymous");
      return null;
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // One place turns "the API said 401" into "show the login screen", for every
  // request in the app — including the ones a view never awaited.
  useEffect(
    () =>
      api.onUnauthorized(() => {
        setSession(null);
        setStatus("anonymous");
      }),
    [],
  );

  const adopt = useCallback((next: AuthSession) => {
    // `api.login()` already took the session's CSRF token and workspace.
    setSession(next);
    setStatus("authenticated");
  }, []);

  const signOut = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      // A logout the server refused still means this tab stops acting signed in.
    }
    setSession(null);
    setStatus("anonymous");
  }, []);

  const value = useMemo(
    () => ({ status, session, refresh, adopt, signOut }),
    [status, session, refresh, adopt, signOut],
  );

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) {
    throw new Error("useSession must be used inside a SessionProvider");
  }
  return value;
}
