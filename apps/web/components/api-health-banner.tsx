"use client";

import type { WorkspaceApi } from "@workspace/api-client";
import { RefreshCw, WifiOff } from "lucide-react";
import { useEffect, useState } from "react";

/** What one poll loop knows: is the API down, and a hand-cranked re-check. */
export type ApiHealth = {
  down: boolean;
  retry: () => void;
};

/**
 * The health poll, hoisted out of the banner so one loop can feed every
 * surface that states the API's reachability — the banner and the system
 * status popover must be the same truth, not two pollers that disagree for a
 * cycle. Slow cadence while healthy, fast while down, `onRecovered` fired on
 * the first healthy answer after a failure.
 */
export function useApiHealth(api: WorkspaceApi, onRecovered: () => void): ApiHealth {
  const [down, setDown] = useState(false);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    let timer = 0;
    async function check(wasDown: boolean) {
      let healthy = true;
      try {
        await api.health();
      } catch {
        healthy = false;
      }
      if (cancelled) return;
      setDown(!healthy);
      if (healthy && wasDown) onRecovered();
      timer = window.setTimeout(() => void check(!healthy), healthy ? 15_000 : 4_000);
    }
    void check(false);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [api, onRecovered, nonce]);

  return { down, retry: () => setNonce((value) => value + 1) };
}

type ApiHealthBannerProps = {
  api: WorkspaceApi;
  health: ApiHealth;
};

export function ApiHealthBanner({ api, health }: ApiHealthBannerProps) {
  if (!health.down) return null;
  return (
    <div className="api-down-banner" role="alert">
      <WifiOff size={15} />
      <span>
        The Grain API is unreachable at {api.baseUrl}. Start it with{" "}
        <code>make dev</code>.
      </span>
      <button onClick={health.retry}>
        <RefreshCw size={14} />
        Retry
      </button>
    </div>
  );
}
