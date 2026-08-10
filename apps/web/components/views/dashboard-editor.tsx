"use client";

import { ArrowUp, BarChart3, Database, X } from "lucide-react";
import type { Dataset, GeneratedApp } from "@workspace/api-client";
import { FormEvent, Fragment, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { SandboxFrame } from "../sandbox-frame";
import { describeError } from "./shared";

export type DashboardEditorProps = {
  app: GeneratedApp | null;
  datasets: Dataset[];
  create: (name: string, visibility: "private" | "public") => Promise<GeneratedApp>;
  generate: (
    app: GeneratedApp,
    prompt: string,
    datasetIds: string[],
  ) => Promise<GeneratedApp>;
  publish: (app: GeneratedApp, releaseId: string) => Promise<void>;
  onCreated: (id: string) => void;
  onClose: () => void;
  setError: (message: string) => void;
};

/**
 * Chat on the left, the live dashboard on the right. Turn history is the
 * release history: every generation stores its prompt in the manifest, so the
 * transcript survives reloads without a second source of truth.
 */
export function DashboardEditor({
  app,
  datasets,
  create,
  generate,
  publish,
  onCreated,
  onClose,
  setError,
}: DashboardEditorProps) {
  const [name, setName] = useState("Untitled dashboard");
  const [visibility, setVisibility] = useState<"private" | "public">("private");
  const [prompt, setPrompt] = useState("");
  const [overrides, setOverrides] = useState<string[] | null>(null);
  const [pending, setPending] = useState("");
  const [working, setWorking] = useState(false);
  const [nonce, setNonce] = useState(0);
  const logRef = useRef<HTMLDivElement>(null);

  const latest = app?.releases[0] || null;
  const bound =
    latest?.manifest.data_bindings?.map((binding) => binding.dataset_id) || null;
  const active = overrides ?? bound ?? datasets.map((dataset) => dataset.id);
  const history = app ? [...app.releases].reverse() : [];
  const versions = app?.releases.length ?? 0;

  useEffect(() => {
    const log = logRef.current;
    if (log) log.scrollTop = log.scrollHeight;
  }, [versions, pending, working]);

  async function send(event?: FormEvent) {
    event?.preventDefault();
    const text = prompt.trim();
    if (!text || working) return;
    setWorking(true);
    setPending(text);
    setPrompt("");
    try {
      let target = app;
      if (!target) {
        target = await create(name.trim() || "Untitled dashboard", visibility);
        onCreated(target.id);
      }
      await generate(target, text, active);
      setNonce((value) => value + 1);
    } catch (caught) {
      setPrompt(text);
      setError(describeError(caught, "Could not build that dashboard"));
    } finally {
      setPending("");
      setWorking(false);
    }
  }

  return (
    <div className="drawer-scrim" onClick={onClose}>
      <aside className="dashboard-editor" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-header">
          <div>
            {app ? (
              <>
                <span>
                  {latest ? `v${latest.version} · ${latest.status}` : "not built yet"}
                </span>
                <strong>{app.name}</strong>
              </>
            ) : (
              <>
                <span>New dashboard</span>
                <input
                  className="editor-name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  aria-label="Dashboard name"
                  maxLength={160}
                />
              </>
            )}
          </div>
          {!app && (
            <label className="editor-visibility">
              <input
                type="checkbox"
                checked={visibility === "public"}
                onChange={(event) =>
                  setVisibility(event.target.checked ? "public" : "private")
                }
              />
              Public link
            </label>
          )}
          {app && latest?.status === "draft" && (
            <button
              className="publish-button"
              onClick={() => void publish(app, latest.id)}
            >
              Publish v{latest.version}
            </button>
          )}
          <button className="icon-button" onClick={onClose} aria-label="Close editor">
            <X size={18} />
          </button>
        </div>

        <div className="editor-body">
          <div className="editor-chat">
            <div className="editor-log" ref={logRef}>
              {history.length === 0 && !pending && (
                <p className="editor-hint">
                  {datasets.length === 0
                    ? "Describe the dashboard you want. Upload a CSV or JSON source to give it data."
                    : "Describe the dashboard you want."}
                </p>
              )}
              {history.map((release) => (
                <Fragment key={release.id}>
                  {release.manifest.prompt && (
                    <div className="editor-turn user">{release.manifest.prompt}</div>
                  )}
                  <div className="editor-turn system">Built v{release.version}</div>
                </Fragment>
              ))}
              {pending && <div className="editor-turn user">{pending}</div>}
              {working && (
                <div className="editor-turn system">
                  <span className="thinking-dots">
                    <i />
                    <i />
                    <i />
                  </span>
                  Building
                </div>
              )}
            </div>

            <form className="editor-composer" onSubmit={(event) => void send(event)}>
              {datasets.length > 0 && (
                <div className="data-chips">
                  <Database size={13} />
                  {datasets.map((dataset) => {
                    const on = active.includes(dataset.id);
                    return (
                      <button
                        type="button"
                        key={dataset.id}
                        className={on ? "data-chip on" : "data-chip"}
                        aria-pressed={on}
                        onClick={() =>
                          setOverrides(
                            on
                              ? active.filter((id) => id !== dataset.id)
                              : [...active, dataset.id],
                          )
                        }
                      >
                        {dataset.name}
                      </button>
                    );
                  })}
                </div>
              )}
              <div className="editor-input">
                <textarea
                  value={prompt}
                  onChange={(event) => setPrompt(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      void send();
                    }
                  }}
                  placeholder={
                    app ? "Describe a change…" : "Describe this dashboard…"
                  }
                  rows={2}
                  maxLength={4000}
                  disabled={working}
                />
                <button
                  className="send-button"
                  type="submit"
                  disabled={!prompt.trim() || working}
                  aria-label="Send"
                >
                  <ArrowUp size={18} />
                </button>
              </div>
            </form>
          </div>

          <div className="editor-preview">
            {app && latest ? (
              <SandboxFrame
                key={`${latest.id}-${nonce}`}
                src={api.frameUrl(app.id, latest.id)}
                title={`${app.name} preview`}
                snapshots={latest.manifest.snapshots || {}}
                bindings={latest.manifest.data_bindings || []}
                api={api}
                className="sandbox-frame"
              />
            ) : (
              <div className="feature-empty">
                <BarChart3 size={22} />
                <strong>Nothing here yet</strong>
                <span>Send a message to build the first version.</span>
              </div>
            )}
          </div>
        </div>
      </aside>
    </div>
  );
}
