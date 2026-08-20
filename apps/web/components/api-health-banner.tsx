"use client";

import type { WorkspaceApi } from "@workspace/api-client";
import { RefreshCw, WifiOff } from "lucide-react";
import { useEffect, useState } from "react";

type ApiHealthBannerProps = {
  api: WorkspaceApi;
  onRecovered: () => void;
};

export function ApiHealthBanner({ api, onRecovered }: ApiHealthBannerProps) {
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

  if (!down) return null;
  return (
    <div className="api-down-banner" role="alert">
      <WifiOff size={15} />
      <span>
        The Grain API is unreachable at {api.baseUrl}. Start it with{" "}
        <code>make dev</code>.
      </span>
      <button onClick={() => setNonce((value) => value + 1)}>
        <RefreshCw size={14} />
        Retry
      </button>
    </div>
  );
}
