"use client";

import type { DatasetQueryResult } from "@workspace/api-client";
import { ShieldCheck } from "lucide-react";
import { SandboxFrame } from "./sandbox-frame";

type PublishedCodeFrameProps = {
  slug: string;
  name: string;
  snapshots: Record<string, DatasetQueryResult>;
};

export function PublishedCodeFrame({ slug, name, snapshots }: PublishedCodeFrameProps) {
  const apiBase = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  return (
    <div className="published-code-app">
      <div className="published-code-badge">
        <ShieldCheck size={13} />
        Generated app — runs sandboxed with no network access
      </div>
      <SandboxFrame
        src={`${apiBase}/published/apps/${encodeURIComponent(slug)}/frame`}
        title={name}
        snapshots={snapshots}
        className="sandbox-frame published"
      />
    </div>
  );
}
