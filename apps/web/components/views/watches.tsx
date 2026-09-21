"use client";

import type {
  Source,
  Space,
  Watch,
  WatchObservation,
} from "@workspace/api-client";
import { Eye, Play, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { describeActionError, describeError, formatRelative } from "./shared";
import {
  FIELD_NAME_HELPER_TEXT,
  MAX_EXTRACTION_FIELDS,
  SHARED_HELPER_TEXT,
  describeLastChange,
  describeRunNow,
  describeSchedule,
  describeTarget,
} from "./watch-format";

/**
 * Watches: a standing question about a source or a space, re-asked on a
 * schedule by the same tick that fires crons.
 *
 * One control on this screen does something the rest of the product cannot
 * undo quietly: "Share with the workspace" decides whether what the watch
 * learns lands on everyone's memory shelf or only its creator's. Its helper
 * text says exactly that, in the words the server means, because a person
 * ticking it is granting the only visibility widening this family can do.
 */
export type WatchesViewProps = {
  setError: (message: string) => void;
  /**
   * Pre-fill from the affordance that opened this view — "Watch this file" on
   * a source row, "Watch this space" on a space. The shell lowers the flag
   * once handled, so navigating back does not reopen a dismissed composer.
   */
  composeSeed?: { targetKind: "source" | "space"; targetId: string; name: string };
  onComposeSeedHandled?: () => void;
};

const DEFAULT_CRON = "0 9 * * *";

function WatchComposer({
  sources,
  spaces,
  seed,
  onCreated,
  onCancel,
  setError,
}: {
  sources: Source[];
  spaces: Space[];
  seed?: WatchesViewProps["composeSeed"];
  onCreated: (watch: Watch) => void;
  onCancel: () => void;
  setError: (message: string) => void;
}) {
  const [name, setName] = useState(seed?.name ?? "");
  const [targetKind, setTargetKind] = useState<"source" | "space">(
    seed?.targetKind ?? "source",
  );
  const [targetId, setTargetId] = useState(seed?.targetId ?? "");
  const [scheduleCron, setScheduleCron] = useState(DEFAULT_CRON);
  const [timezone, setTimezone] = useState("UTC");
  const [shared, setShared] = useState(false);
  const [fields, setFields] = useState<string[]>([""]);
  const [busy, setBusy] = useState(false);

  const ready = name.trim() && targetId && scheduleCron.trim();

  async function submit() {
    if (!ready || busy) return;
    setBusy(true);
    try {
      const created = await api.createWatch({
        name: name.trim(),
        target_kind: targetKind,
        target_id: targetId,
        schedule_cron: scheduleCron.trim(),
        schedule_timezone: timezone.trim() || "UTC",
        shared,
        extraction_fields: fields.map((field) => field.trim()).filter(Boolean),
      });
      onCreated(created);
    } catch (caught) {
      setError(describeActionError(caught, "Could not create that watch"));
    } finally {
      setBusy(false);
    }
  }

  const options = targetKind === "space" ? spaces : sources;

  return (
    <section className="workflow-author">
      <div className="page-heading">
        <div>
          <h1>New watch</h1>
        </div>
      </div>
      <form
        className="cron-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <label className="cron-field">
          <span>Name</span>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Release notes"
          />
        </label>

        <label className="cron-field">
          <span>Watch a</span>
          <select
            value={targetKind}
            onChange={(event) => {
              setTargetKind(event.target.value as "source" | "space");
              setTargetId("");
            }}
          >
            <option value="source">File</option>
            <option value="space">Space</option>
          </select>
        </label>

        <label className="cron-field">
          <span>{targetKind === "space" ? "Space" : "File"}</span>
          <select
            value={targetId}
            onChange={(event) => setTargetId(event.target.value)}
          >
            <option value="">Choose one…</option>
            {options.map((option) => (
              <option key={option.id} value={option.id}>
                {"filename" in option ? option.filename : option.name}
              </option>
            ))}
          </select>
        </label>

        <div className="cron-field-row">
          <label className="cron-field">
            <span>Schedule</span>
            <input
              value={scheduleCron}
              onChange={(event) => setScheduleCron(event.target.value)}
              placeholder={DEFAULT_CRON}
              spellCheck={false}
              autoCapitalize="none"
            />
            {/* The server refuses anything finer than hourly: every fire of a
                changed watch writes one memory row per declared field. */}
            <span className="cron-zone-note">
              At most once an hour — name a fixed minute.
            </span>
          </label>
          <label className="cron-field">
            <span>Timezone</span>
            <input
              value={timezone}
              onChange={(event) => setTimezone(event.target.value)}
              placeholder="UTC"
            />
          </label>
        </div>

        <label className="cron-field watch-shared">
          <span>
            <input
              type="checkbox"
              checked={shared}
              onChange={(event) => setShared(event.target.checked)}
            />{" "}
            Share with the workspace
          </span>
          <span className="cron-zone-note">{SHARED_HELPER_TEXT}</span>
        </label>

        <div className="cron-field">
          <span>Fields to extract</span>
          <span className="cron-zone-note">{FIELD_NAME_HELPER_TEXT}</span>
          {fields.map((field, index) => (
            <div className="cron-compile-row" key={index}>
              <input
                value={field}
                onChange={(event) =>
                  setFields((rows) =>
                    rows.map((row, position) =>
                      position === index ? event.target.value : row,
                    ),
                  )
                }
                placeholder="owner"
              />
              <button
                type="button"
                className="ghost-button"
                onClick={() =>
                  setFields((rows) => rows.filter((_row, position) => position !== index))
                }
              >
                Remove
              </button>
            </div>
          ))}
          <button
            type="button"
            className="ghost-button"
            disabled={fields.length >= MAX_EXTRACTION_FIELDS}
            onClick={() => setFields((rows) => [...rows, ""])}
          >
            <Plus size={14} />
            Add a field
          </button>
        </div>

        <div className="cron-form-actions">
          <button type="submit" className="primary-button" disabled={!ready || busy}>
            {busy ? "Creating…" : "Create watch"}
          </button>
          <button type="button" className="ghost-button" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </form>
    </section>
  );
}

export function WatchesView({
  setError,
  composeSeed,
  onComposeSeedHandled,
}: WatchesViewProps) {
  const [watches, setWatches] = useState<Watch[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const [spaces, setSpaces] = useState<Space[]>([]);
  const [observations, setObservations] = useState<WatchObservation[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [activeId, setActiveId] = useState("");
  const [composing, setComposing] = useState(false);
  // The seed, copied local BEFORE the handled callback lowers the parent's
  // flag — the parent clears its copy on that callback, so passing the prop
  // straight through mounted the composer over an already-empty seed (the
  // crons view learned the same lesson). The manual New-watch button clears
  // it so a blank composer stays blank after a seeded one.
  const [seed, setSeed] = useState<WatchesViewProps["composeSeed"]>();
  const [ranNow, setRanNow] = useState("");
  const [busy, setBusy] = useState(false);

  const active = watches.find((watch) => watch.id === activeId) ?? null;

  const load = useCallback(async () => {
    try {
      const [rows, sourceRows, spaceRows] = await Promise.all([
        api.listWatches(),
        api.listSources().catch(() => [] as Source[]),
        api.listSpaces().catch(() => [] as Space[]),
      ]);
      setWatches(rows);
      setSources(sourceRows);
      setSpaces(spaceRows);
    } catch (caught) {
      setError(describeError(caught, "Could not load watches"));
    } finally {
      setLoaded(true);
    }
  }, [setError]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!composeSeed) return;
    setSeed(composeSeed);
    onComposeSeedHandled?.();
    setComposing(true);
    setActiveId("");
  }, [composeSeed, onComposeSeedHandled]);

  useEffect(() => {
    if (!activeId) {
      setObservations([]);
      return;
    }
    let live = true;
    void api
      .listWatchObservations(activeId)
      .then((rows) => {
        if (live) setObservations(rows);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [activeId, ranNow]);

  function targetName(watch: Watch): string {
    if (watch.target_kind === "space") {
      return spaces.find((space) => space.id === watch.target_id)?.name ?? "";
    }
    return sources.find((source) => source.id === watch.target_id)?.filename ?? "";
  }

  async function setEnabled(watch: Watch, enabled: boolean) {
    try {
      const updated = await api.updateWatch(watch.id, { enabled });
      setWatches((rows) => rows.map((row) => (row.id === updated.id ? updated : row)));
    } catch (caught) {
      setError(describeActionError(caught, "Could not change that watch"));
    }
  }

  async function runNow(watch: Watch) {
    if (busy) return;
    setBusy(true);
    setRanNow("");
    try {
      const result = await api.runWatchNow(watch.id);
      setRanNow(describeRunNow(result.observation.changed));
      setWatches(await api.listWatches());
    } catch (caught) {
      setError(describeActionError(caught, "Could not run that watch now"));
    } finally {
      setBusy(false);
    }
  }

  async function remove(watch: Watch) {
    if (
      !window.confirm(
        `Delete “${watch.name}”? Its brief document is kept, and so is what it has already learned.`,
      )
    ) {
      return;
    }
    try {
      await api.deleteWatch(watch.id);
      setWatches((rows) => rows.filter((row) => row.id !== watch.id));
      if (activeId === watch.id) setActiveId("");
    } catch (caught) {
      setError(describeActionError(caught, "Could not delete that watch"));
    }
  }

  return (
    <div className="workflow-layout">
      <aside className="workflow-sidebar">
        <div className="workflow-sidebar-head">
          <span>Watches</span>
          <button
            className="icon-button"
            aria-label="New watch"
            onClick={() => {
              setSeed(undefined);
              setComposing(true);
              setActiveId("");
            }}
          >
            <Plus size={16} />
          </button>
        </div>
        {watches.length === 0 ? (
          <p className="workflow-empty">
            {loaded ? "No watches yet." : "Loading…"}
          </p>
        ) : (
          <ul className="workflow-items">
            {watches.map((watch) => (
              <li key={watch.id}>
                <button
                  type="button"
                  className={watch.id === activeId ? "workflow-item active" : "workflow-item"}
                  onClick={() => {
                    setComposing(false);
                    setActiveId(watch.id);
                    setRanNow("");
                  }}
                >
                  <span className="workflow-item-name">
                    <Eye size={14} />
                    {watch.name}
                  </span>
                  <span className="workflow-item-meta">
                    {describeTarget(watch, targetName(watch))}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <section className="workflow-detail">
        {composing ? (
          <WatchComposer
            key={seed ? `${seed.targetKind}:${seed.targetId}` : "blank"}
            sources={sources}
            spaces={spaces}
            seed={seed}
            setError={setError}
            onCancel={() => setComposing(false)}
            onCreated={(watch) => {
              setWatches((rows) => [watch, ...rows]);
              setComposing(false);
              setActiveId(watch.id);
            }}
          />
        ) : !active ? (
          <p className="workflow-empty">Select a watch.</p>
        ) : (
          <>
            <div className="page-heading">
              <div>
                <h1>{active.name}</h1>
                <p className="page-subtitle">
                  {describeTarget(active, targetName(active))} ·{" "}
                  {describeSchedule(active)} ·{" "}
                  {describeLastChange(active, formatRelative)}
                </p>
              </div>
              <div className="page-actions">
                <button
                  type="button"
                  className="ghost-button"
                  aria-disabled={busy}
                  onClick={() => void runNow(active)}
                >
                  <Play size={14} />
                  Run now
                </button>
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => void setEnabled(active, !active.enabled)}
                >
                  {active.enabled ? "Disable" : "Enable"}
                </button>
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => void remove(active)}
                >
                  <Trash2 size={14} />
                  Delete
                </button>
              </div>
            </div>

            {ranNow && <p className="cron-compiled">{ranNow}</p>}

            <p className="page-subtitle">
              {active.shared
                ? "Shared — what it learns goes to everyone’s memory shelf."
                : "Personal — what it learns goes to your own memory shelf."}
            </p>

            {active.brief_document_id && (
              <p className="page-subtitle">
                Its standing brief is a document in Library → Documents.
              </p>
            )}

            <section className="page-citations">
              <h2>Observations</h2>
              {observations.length === 0 ? (
                <p className="workflow-empty">Nothing recorded yet.</p>
              ) : (
                <ol>
                  {observations.map((observation) => (
                    <li key={observation.id}>
                      <span className="page-citation-head">
                        <strong>{formatRelative(observation.created_at)}</strong>{" "}
                        {observation.summary}
                      </span>
                      {Object.keys(observation.fields).length > 0 && (
                        <ul>
                          {Object.entries(observation.fields).map(([field, value]) => (
                            <li key={field}>
                              <code>{field}</code>: {value || "—"}
                            </li>
                          ))}
                        </ul>
                      )}
                    </li>
                  ))}
                </ol>
              )}
            </section>
          </>
        )}
      </section>
    </div>
  );
}
