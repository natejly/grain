"use client";

import { Globe2, LockKeyhole } from "lucide-react";
import type { GeneratedApp } from "@workspace/api-client";
import { useState } from "react";
import { api } from "../api";
import { SandboxFrame } from "../sandbox-frame";

export type DashboardTileProps = {
  app: GeneratedApp;
  open: () => void;
  publish: (app: GeneratedApp, releaseId: string) => Promise<void>;
  rollback: (app: GeneratedApp, releaseId: string) => Promise<void>;
};

export function DashboardTile({ app, open, publish, rollback }: DashboardTileProps) {
  const [showVersions, setShowVersions] = useState(false);
  const current =
    app.releases.find((release) => release.id === app.current_release_id) ||
    app.releases[0] ||
    null;
  const latest = app.releases[0] || null;

  return (
    <article className="dashboard-tile">
      <div className="dashboard-tile-preview">
        {current ? (
          <SandboxFrame
            key={current.id}
            src={api.frameUrl(app.id, current.id)}
            title={app.name}
            snapshots={current.manifest.snapshots || {}}
            className="tile-frame"
          />
        ) : (
          <span className="dashboard-tile-blank">No version yet</span>
        )}
        <button className="dashboard-tile-open" onClick={open}>
          Edit
        </button>
      </div>

      <div className="dashboard-tile-foot">
        <div>
          <strong>{app.name}</strong>
          <span>
            {app.visibility === "public" ? (
              <Globe2 size={12} />
            ) : (
              <LockKeyhole size={12} />
            )}
            {current ? `v${current.version} · ${current.status}` : "not built yet"}
          </span>
        </div>
        <div className="dashboard-tile-actions">
          {latest?.status === "draft" && (
            <button
              className="publish-button"
              onClick={() => void publish(app, latest.id)}
            >
              Publish v{latest.version}
            </button>
          )}
          {app.visibility === "public" && app.current_release_id && (
            <a href={`/apps/${app.slug}`} target="_blank" rel="noreferrer">
              Open
            </a>
          )}
          {app.releases.length > 1 && (
            <button onClick={() => setShowVersions((value) => !value)}>
              Versions
            </button>
          )}
        </div>
      </div>

      {showVersions && (
        <div className="release-list">
          {app.releases.map((release) => (
            <div key={release.id}>
              <span>
                v{release.version} · {release.status}
              </span>
              {release.id !== app.current_release_id &&
                release.status !== "draft" && (
                  <button onClick={() => void rollback(app, release.id)}>
                    Roll back
                  </button>
                )}
            </div>
          ))}
        </div>
      )}
    </article>
  );
}
