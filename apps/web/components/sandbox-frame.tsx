"use client";

import type {
  AppDataBinding,
  DatasetQuery,
  DatasetQueryResult,
  WorkspaceApi,
} from "@workspace/api-client";
import { useEffect, useRef } from "react";
import { useTheme } from "./theme";

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
 *
 * Two protocol namespaces, deliberately. Current apps carry the `jasmine:*`
 * runtime, but a published release freezes its runtime into the stored HTML,
 * so every snapshot cut before the rename still speaks `fieldnote:*` and always
 * will. Unprompted init therefore goes out on both namespaces — each runtime
 * ignores the type it does not know, so neither ever sees a duplicate — and a
 * reply is sent back on whichever namespace the request arrived on.
 */

const NAMESPACES = ["jasmine", "fieldnote"] as const;
type Namespace = (typeof NAMESPACES)[number];

/** "jasmine:query" -> "jasmine", and nothing for a message that is not ours. */
function namespaceOf(type: string | undefined, verb: string): Namespace | null {
  for (const namespace of NAMESPACES) {
    if (type === `${namespace}:${verb}`) return namespace;
  }
  return null;
}

export function SandboxFrame({
  src,
  title,
  snapshots,
  bindings,
  api,
  className,
}: SandboxFrameProps) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  // Generated apps cannot read the host's CSS variables across the origin
  // boundary, so the resolved theme rides the init message. A toggle re-runs
  // this effect and re-posts, which is how a live frame follows the switch.
  const theme = useTheme();

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

      if (namespaceOf(message?.type, "ready")) {
        sendInit();
        return;
      }

      const namespace = namespaceOf(message?.type, "query");
      if (namespace && message.requestId) {
        const reply = (payload: Record<string, unknown>) =>
          post({ type: `${namespace}:result`, requestId: message.requestId, ...payload });
        const binding = bindings?.find((item) => item.name === message.dataset);
        if (!binding || !api) {
          reply({ error: "Dataset is not available to this app" });
          return;
        }
        try {
          const result = await api.queryDataset(
            binding.dataset_id,
            message.query || {},
          );
          reply({ result });
        } catch (caught) {
          reply({ error: caught instanceof Error ? caught.message : "Query failed" });
        }
      }
    }

    function sendInit() {
      for (const namespace of NAMESPACES) {
        post({ type: `${namespace}:init`, snapshots, theme });
      }
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
  }, [snapshots, bindings, api, theme]);

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
