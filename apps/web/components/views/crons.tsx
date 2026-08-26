"use client";

import type {
  Cron,
  CronKind,
  ScheduleCompileResult,
} from "@workspace/api-client";
import {
  Bot,
  Check,
  Clock,
  LoaderCircle,
  MessageSquare,
  Play,
  Plus,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { formatNextFires } from "./cron-compile-format";
import { FavoriteStar, type FavoritesApi } from "./favorites";
import { describeError, formatRelative } from "./shared";
import type { ScheduleNote } from "./workflow-format";

/**
 * Personal crons: recurring work a user schedules and then walks away from.
 *
 * Structurally this is the Workflows view's smaller sibling — a list on the
 * left, a detail on the right — and it reuses that view's layout classes on
 * purpose, because the two answer the same question ("what runs unattended, and
 * when") and should not look like two different products. What it deliberately
 * does *not* borrow is a run inbox: a cron that fired a task run parks its
 * writes in the conversation that run created, so the approval a person answers
 * lives in Chat, next to the turn that raised it, not here.
 *
 * The one claim this screen is careful never to overstate is that a schedule
 * will fire. A stored cron expression is an intention; only the ticker makes it
 * a promise, and the ticker is a separate deployment concern. So the schedule
 * note reads its armed state from `workflowSchedulingEnabled()` — the same
 * probe the Workflows view uses, because crons ride the same tick — and every
 * branch that cannot promise a dispatch says so in the headline, not the small
 * print.
 */
export type CronsViewProps = {
  setError: (message: string) => void;
  /** The shell's one favorites list; without it the rows offer no star. */
  favorites?: FavoritesApi;
};

const TIMELINE_PLACEHOLDER = "0 9 * * 1";

function KindChip({ kind }: { kind: CronKind }) {
  return (
    <span className="workflow-chip">
      {kind === "message" ? <MessageSquare size={11} /> : <Bot size={11} />}
      {kind === "message" ? "message" : "task"}
    </span>
  );
}

/**
 * The truth about when this cron runs, in the Workflows view's tone.
 *
 * Mirrors `describeSchedule` branch for branch, but speaks about a cron rather
 * than a graph: it has no draft/active status, only an `enabled` flag, and a
 * disabled cron is dispatched by nobody however armed the ticker is. `enabled`
 * is checked first for that reason — an operator who turned a cron off should
 * not read "Runs on …" because the deployment happens to have a secret set.
 */
export function describeCronSchedule(
  cron: Cron,
  schedulingEnabled: boolean | null,
): ScheduleNote {
  const when = `${cron.schedule_cron} · ${cron.schedule_timezone}`;
  if (!cron.enabled) {
    return {
      tone: "warn",
      headline: "Disabled — nothing will fire it",
      detail: `Enable this automation to run it on ${when}.`,
    };
  }
  if (schedulingEnabled === false) {
    return {
      tone: "warn",
      headline: "Schedule stored, but nothing will fire it",
      detail:
        `This deployment has no cron ticker configured, so ${when} is a ` +
        "recorded intention and not a promise. Until an operator arms the " +
        "tick endpoint, this automation only runs when you press Run now.",
    };
  }
  if (schedulingEnabled === null) {
    return {
      tone: "warn",
      headline: "Schedule stored; the scheduler did not answer",
      detail:
        `${when}. We could not reach the tick endpoint to find out whether ` +
        "anything dispatches it, so treat this as unscheduled until it answers.",
    };
  }
  return {
    tone: "live",
    headline: `Runs on ${when}`,
    detail: cron.last_dispatched_at
      ? `Scheduling is configured. Last fired ${new Date(
          cron.last_dispatched_at,
        ).toLocaleString()}.`
      : "Scheduling is configured. It has not fired yet.",
  };
}

/** The form that makes a cron: a name, a schedule, a kind, and the payload. */
function CronForm({
  setError,
  onCreated,
}: {
  setError: (message: string) => void;
  onCreated: (cron: Cron) => void;
}) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState<CronKind>("task");
  const [scheduleCron, setScheduleCron] = useState("");
  // The viewer's own zone, not UTC: "every weekday at 9am" means THEIR 9am
  // unless they say otherwise. The compiled receipt names the zone either way,
  // and the Advanced field still takes any zone by hand.
  const [timezone, setTimezone] = useState(() => {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
    } catch {
      return "UTC";
    }
  });
  const [prompt, setPrompt] = useState("");
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  /** The English sentence, and what the compiler last said it means. */
  const [scheduleText, setScheduleText] = useState("");
  const [compiled, setCompiled] = useState<ScheduleCompileResult | null>(null);
  const [compileError, setCompileError] = useState("");
  const [compiling, setCompiling] = useState(false);

  const payloadText = kind === "message" ? body : prompt;
  const ready = Boolean(name.trim() && scheduleCron.trim() && payloadText.trim());

  /**
   * Turn the sentence into a cron and let it fill the real fields: submit posts
   * `scheduleCron`/`timezone` verbatim, so a successful compile writes into
   * exactly that state and the saved schedule is the previewed one, always. A
   * refused compile (422, a human sentence) is this row's problem, not the
   * workspace's — it renders beside the input and never in the global toast.
   */
  async function compile() {
    const text = scheduleText.trim();
    if (!text || compiling) return;
    setCompiling(true);
    setCompileError("");
    try {
      const result = await api.compileSchedule(text, timezone.trim() || "UTC");
      setCompiled(result);
      setScheduleCron(result.schedule_cron);
      setTimezone(result.schedule_timezone);
    } catch (caught) {
      setCompiled(null);
      setCompileError(describeError(caught, "Could not compile that schedule"));
    } finally {
      setCompiling(false);
    }
  }

  async function submit() {
    if (!ready || busy) return;
    setBusy(true);
    try {
      const created = await api.createCron({
        name: name.trim(),
        kind,
        schedule_cron: scheduleCron.trim(),
        schedule_timezone: timezone.trim() || "UTC",
        // Only the field this kind uses is sent; the server ignores the other,
        // but there is no reason to carry a stale draft of it across a kind flip.
        ...(kind === "message" ? { body } : { prompt }),
      });
      onCreated(created);
    } catch (caught) {
      setError(describeError(caught, "Could not create that automation"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="workflow-author">
      <div className="page-heading">
        <div>
          <h1>New automation</h1>
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
            placeholder="Morning digest"
          />
        </label>

        <label className="cron-field">
          <span>Kind</span>
          <select
            value={kind}
            onChange={(event) => setKind(event.target.value as CronKind)}
          >
            {/* A task is re-run as a fresh unattended chat turn, at workflow
                policy scope — so its first write waits for you. A message is
                just posted into a conversation, and writes nothing on its own. */}
            <option value="task">Task — run a prompt as an unattended turn</option>
            <option value="message">Message — post text into a conversation</option>
          </select>
        </label>

        {/* The schedule is written in English and compiled, not typed in cron.
            A div rather than a label: a label wrapping both the input and the
            Compile button would forward clicks on the button to the input. */}
        <div className="cron-field">
          <span>Schedule</span>
          <div className="cron-compile-row">
            <input
              value={scheduleText}
              onChange={(event) => {
                setScheduleText(event.target.value);
                // Editing the sentence after a compile voids the receipt AND
                // the cron it wrote: submit must never post a schedule the
                // sentence on screen no longer describes. A hand-typed
                // Advanced expression (compiled already null) is left alone.
                if (compiled) {
                  setCompiled(null);
                  setScheduleCron("");
                }
              }}
              placeholder="every weekday at 9am"
              aria-label="Describe the schedule"
            />
            {/* aria-disabled while compiling (compile() refuses re-entry), so
                the button keeps focus through the round trip; the empty
                sentence keeps the real disabled — the button cannot hold
                focus before there is anything to compile. */}
            <button
              type="button"
              className="ghost-button"
              disabled={!scheduleText.trim()}
              aria-disabled={compiling || !scheduleText.trim()}
              onClick={() => void compile()}
            >
              {compiling ? <LoaderCircle size={14} className="spin" /> : null}
              {compiling ? "Compiling…" : "Compile"}
            </button>
          </div>
          {compileError && <p className="cron-compile-error">{compileError}</p>}
          {compiled && (
            <p className="cron-compiled">
              <code className="cron-compiled-chip" aria-label="Compiled schedule">
                {compiled.schedule_cron}
              </code>
              <span>{compiled.schedule_timezone}</span>
              {compiled.next_fires.length > 0 && (
                <span className="cron-next-fires">
                  Next:{" "}
                  {formatNextFires(compiled.next_fires, compiled.schedule_timezone)}
                </span>
              )}
            </p>
          )}
        </div>

        {/* The raw fields still exist — some schedules have no sentence — but
            behind a disclosure. Hand-typing here drops the compiled preview:
            the chip echoes what submit will post, and a preview that could
            disagree with the save is worse than none. */}
        <details className="cron-advanced">
          <summary>Advanced</summary>
          <div className="cron-field-row">
            <label className="cron-field">
              <span>Cron expression</span>
              <input
                value={scheduleCron}
                onChange={(event) => {
                  setScheduleCron(event.target.value);
                  setCompiled(null);
                }}
                placeholder={TIMELINE_PLACEHOLDER}
                // A 5-field cron expression, shown and stored verbatim. Validation
                // is the server's — it refuses a malformed one with a 422.
                spellCheck={false}
                autoCapitalize="none"
              />
              {/* The zone claim, beside the expression it governs: the field
                  now defaults to the VIEWER'S zone (right for a typed
                  sentence), which silently shifted the muscle-memory workflow
                  of hand-typing a cron that always meant UTC. The sentence
                  makes the shift visible where the fingers are. */}
              <span className="cron-zone-note">
                Fires in {timezone.trim() || "UTC"} — change it beside this field
              </span>
            </label>
            <label className="cron-field">
              <span>Timezone</span>
              <input
                value={timezone}
                onChange={(event) => {
                  setTimezone(event.target.value);
                  setCompiled(null);
                }}
                placeholder="UTC"
                spellCheck={false}
                autoCapitalize="none"
              />
            </label>
          </div>
        </details>

        {kind === "message" ? (
          <label className="cron-field">
            <span>Message</span>
            <textarea
              className="workflow-prompt"
              value={body}
              onChange={(event) => setBody(event.target.value)}
              placeholder="Posted verbatim into the target conversation each time this fires."
            />
          </label>
        ) : (
          <label className="cron-field">
            <span>Prompt</span>
            <textarea
              className="workflow-prompt"
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder="Re-run as a fresh chat turn each time this fires — e.g. “Summarise yesterday’s open pull requests.”"
            />
          </label>
        )}

        <div className="cron-form-actions">
          <button className="primary-button" type="submit" disabled={busy || !ready}>
            {busy ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />}
            {busy ? "Creating…" : "Create automation"}
          </button>
        </div>
      </form>
    </section>
  );
}

export function CronsView({ setError, favorites }: CronsViewProps) {
  const [crons, setCrons] = useState<Cron[]>([]);
  const [activeId, setActiveId] = useState("");
  const [composing, setComposing] = useState(false);
  const [schedulingEnabled, setSchedulingEnabled] = useState<boolean | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  /** A one-line confirmation of the last Run now, cleared on the next action. */
  const [fired, setFired] = useState("");

  const active = crons.find((item) => item.id === activeId) ?? null;

  const load = useCallback(async () => {
    try {
      setCrons(await api.listCrons());
    } catch (caught) {
      setError(describeError(caught, "Could not load automations"));
    } finally {
      setLoaded(true);
    }
  }, [setError]);

  useEffect(() => {
    void load();
  }, [load]);

  /**
   * Whether the ticker can actually fire a cron, asked of the ticker itself,
   * once, and only when this workspace has one to fire.
   *
   * Every cron is schedule-driven, so unlike the Workflows view — which asks
   * only when a *schedule* trigger exists — a single stored cron is reason
   * enough to ask. The probe is meant to fail (503 unarmed, 401 armed), so a
   * workspace with no automations never makes it, and an unreachable answer
   * stays `null`, which the note reads as "we do not know" and never as "yes".
   */
  const probed = useRef(false);
  useEffect(() => {
    if (crons.length === 0 || probed.current) return;
    probed.current = true;
    void api
      .workflowSchedulingEnabled()
      .then(setSchedulingEnabled)
      .catch(() => undefined);
  }, [crons.length]);

  function select(cron: Cron) {
    setComposing(false);
    setActiveId(cron.id);
    setFired("");
  }

  function created(cron: Cron) {
    setCrons((rows) => [cron, ...rows.filter((row) => row.id !== cron.id)]);
    setComposing(false);
    setActiveId(cron.id);
    setFired("");
  }

  async function setEnabled(cron: Cron, enabled: boolean) {
    setBusy(true);
    try {
      const updated = await api.updateCron(cron.id, { enabled });
      setCrons((rows) => rows.map((row) => (row.id === updated.id ? updated : row)));
    } catch (caught) {
      setError(describeError(caught, "Could not change that automation"));
    } finally {
      setBusy(false);
    }
  }

  async function runNow(cron: Cron) {
    setBusy(true);
    setFired("");
    try {
      await api.runCronNow(cron.id);
      setFired(
        cron.kind === "message"
          ? "Posted into the automation’s conversation."
          : "Started an unattended run. Any write it makes waits for you in Chat.",
      );
    } catch (caught) {
      setError(describeError(caught, "Could not run that automation now"));
    } finally {
      setBusy(false);
    }
  }

  async function remove(cron: Cron) {
    if (!window.confirm(`Delete “${cron.name}”?`)) return;
    try {
      await api.deleteCron(cron.id);
      setCrons((rows) => rows.filter((row) => row.id !== cron.id));
      if (activeId === cron.id) setActiveId("");
    } catch (caught) {
      setError(describeError(caught, "Could not delete that automation"));
    }
  }

  const schedule = active ? describeCronSchedule(active, schedulingEnabled) : null;

  return (
    <div className="workflow-layout">
      <aside className="workflow-sidebar">
        <div className="workflow-sidebar-head">
          <span>Schedules</span>
          <button
            className="icon-button"
            aria-label="New automation"
            onClick={() => {
              setComposing(true);
              setActiveId("");
              setFired("");
            }}
          >
            <Plus size={16} />
          </button>
        </div>

        {crons.length === 0 ? (
          <p className="workflow-empty">{loaded ? "No automations yet." : "Loading…"}</p>
        ) : (
          <ul className="workflow-items">
            {crons.map((cron) => (
              <li key={cron.id}>
                <button
                  className={cron.id === activeId ? "workflow-item active" : "workflow-item"}
                  onClick={() => select(cron)}
                >
                  <Clock size={14} />
                  <span className="workflow-item-name">{cron.name}</span>
                  <span
                    className={`workflow-chip ${cron.enabled ? "active" : "disabled"}`}
                  >
                    {cron.enabled ? "on" : "off"}
                  </span>
                </button>
                <div className="workflow-item-meta">
                  <span>{cron.kind === "message" ? "Message" : "Task"}</span>
                  <span>{cron.schedule_cron || "unscheduled"}</span>
                  <span>
                    {cron.last_dispatched_at
                      ? formatRelative(cron.last_dispatched_at)
                      : "never fired"}
                  </span>
                  {/* Beside the row rather than inside its open button —
                      starring must not also open the automation, and a button
                      may not nest one. */}
                  {favorites && (
                    <FavoriteStar
                      kind="cron"
                      targetId={cron.id}
                      label={cron.name}
                      favorites={favorites}
                      size={12}
                    />
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </aside>

      {composing && (
        <div className="workflow-main">
          <CronForm setError={setError} onCreated={created} />
        </div>
      )}

      {!composing && !active && (
        <div className="workflow-main empty">
          <div className="empty-state">
            <Clock size={22} />
            <p>
              {loaded
                ? "Pick an automation, or schedule a new one."
                : "Loading automations…"}
            </p>
          </div>
        </div>
      )}

      {!composing && active && (
        <div className="workflow-main">
          <header className="workflow-head">
            <div>
              <h1>{active.name}</h1>
              <p>
                {active.kind === "message"
                  ? "Posts a message into its conversation on a schedule."
                  : "Re-runs a prompt as a fresh unattended turn on a schedule."}
              </p>
            </div>
            <div className="workflow-head-actions">
              <button
                className="primary-button"
                disabled={busy}
                onClick={() => void runNow(active)}
              >
                <Play size={14} /> Run now
              </button>
              <button
                className="ghost-button"
                disabled={busy}
                onClick={() => void setEnabled(active, !active.enabled)}
              >
                {active.enabled ? "Disable" : "Enable"}
              </button>
              <button
                className="icon-button"
                aria-label={`Delete ${active.name}`}
                onClick={() => void remove(active)}
              >
                <Trash2 size={15} />
              </button>
            </div>
          </header>

          <div className="workflow-facts">
            <KindChip kind={active.kind} />
            <span className={`workflow-chip ${active.enabled ? "active" : "disabled"}`}>
              {active.enabled ? "enabled" : "disabled"}
            </span>
            <span>
              {active.last_dispatched_at
                ? `last fired ${formatRelative(active.last_dispatched_at)}`
                : "has not fired yet"}
            </span>
          </div>

          {fired && (
            <p className="workflow-approval-note live">
              <Check size={14} />
              {fired}
            </p>
          )}

          {schedule && (
            <div className="workflow-notes">
              <div className={`workflow-note ${schedule.tone}`}>
                <strong>{schedule.headline}</strong>
                <span>{schedule.detail}</span>
              </div>
            </div>
          )}

          {/* The payload, shown the way the run form shows a source prompt — an
              automation you cannot read is an automation you cannot trust. */}
          <blockquote className="workflow-source">
            {(active.kind === "message" ? active.body : active.prompt) ||
              "This automation has no text yet."}
          </blockquote>
        </div>
      )}
    </div>
  );
}
