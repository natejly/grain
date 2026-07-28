"use client";

import type { DatasetQueryResult } from "@workspace/api-client";
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
      <SandboxFrame
        src={`${apiBase}/published/apps/${encodeURIComponent(slug)}/frame`}
        title={name}
        snapshots={snapshots}
        className="sandbox-frame published"
      />
    </div>
  );
}
