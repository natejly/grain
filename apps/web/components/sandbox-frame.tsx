"use client";

import type {
  AppDataBinding,
  DatasetQuery,
  DatasetQueryResult,
  WorkspaceApi,
} from "@workspace/api-client";
import { useEffect, useRef } from "react";

type SandboxFrameProps = {
  src: string;
  title: string;
  snapshots: Record<string, DatasetQueryResult>;
  /** Dataset bindings live queries may target. Omit to disable live queries. */
  bindings?: AppDataBinding[];
  api?: WorkspaceApi;
  className?: string;
};

/**
 * Host side of the generated-app sandbox. The iframe runs with
 * sandbox="allow-scripts" only (opaque origin, no cookies, no parent DOM) and
 * a no-network CSP; every byte of data crosses this postMessage boundary and
 * live queries are validated against the release's declared bindings before
 * reaching the typed DatasetQuery engine.
 */
export function SandboxFrame({
  src,
  title,
  snapshots,
  bindings,
  api,
  className,
}: SandboxFrameProps) {
  const frameRef = useRef<HTMLIFrameElement>(null);

  useEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;

    function post(message: unknown) {
      // The sandboxed frame has an opaque origin, so "*" is the only valid
      // target; authenticity is enforced by checking event.source instead.
      frame?.contentWindow?.postMessage(message, "*");
    }

    async function onMessage(event: MessageEvent) {
      if (event.source !== frame?.contentWindow) return;
      const message = event.data as {
        type?: string;
        requestId?: string;
        dataset?: string;
        query?: DatasetQuery;
      };
      if (message?.type === "fieldnote:ready") {
        post({ type: "fieldnote:init", snapshots });
        return;
      }
      if (message?.type === "fieldnote:query" && message.requestId) {
        const binding = bindings?.find((item) => item.name === message.dataset);
        if (!binding || !api) {
          post({
            type: "fieldnote:result",
            requestId: message.requestId,
            error: "Dataset is not available to this app",
          });
          return;
        }
        try {
          const result = await api.queryDataset(
            binding.dataset_id,
            message.query || {},
          );
          post({
            type: "fieldnote:result",
            requestId: message.requestId,
            result,
          });
        } catch (caught) {
          post({
            type: "fieldnote:result",
            requestId: message.requestId,
            error: caught instanceof Error ? caught.message : "Query failed",
          });
        }
      }
    }

    function sendInit() {
      post({ type: "fieldnote:init", snapshots });
    }

    // On a server-rendered page the frame can finish loading before this
    // effect runs, so its "ready" ping is already gone. Sending on mount and
    // on load as well covers every ordering; the frame handles repeats.
    window.addEventListener("message", onMessage);
    frame.addEventListener("load", sendInit);
    sendInit();
    return () => {
      window.removeEventListener("message", onMessage);
      frame.removeEventListener("load", sendInit);
    };
  }, [snapshots, bindings, api]);

  return (
    <iframe
      ref={frameRef}
      className={className || "sandbox-frame"}
      src={src}
      title={title}
      sandbox="allow-scripts"
    />
  );
}
