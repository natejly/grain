"use client";

import type { AdminBudget, AdminBudgetInput } from "@workspace/api-client";
import { ApiError } from "@workspace/api-client";
import { CirclePause, Gauge, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import { DisclosureMenu } from "../disclosure-menu";
import {
  ceilingPhrase,
  describeBudgetPark,
  describeCeiling,
  hoursLabel,
  readBudgetSpend,
  unpricedRisk,
  type BudgetPark,
} from "./budget-format";
import { describeError, formatRelative } from "./shared";
import { formatCount, shortId } from "./usage-format";

/**
 * The controls for the spend ceiling, and the panel a run parked on it gets.
 *
 * One file for both because they are one gesture seen from two places. ADR 0008
 * put the release *inside* the raise — `PUT /api/admin/budget` re-evaluates
 * every parked run and resumes the ones that now fit — so the thing a stopped
 * user needs is the same form the Admin screen shows, and duplicating it would
 * mean two forms that can disagree about what "no limit" means.
 *
 * The load-bearing decision here is what a non-owner sees. Raising a ceiling is
 * `require_owner`, which answers **403 "Owner role required"**, and the honest
 * thing to do with that answer is show it as a sentence — the way the Admin
 * view already treats its own 403 — rather than render a form whose submit
 * button is a trap. So the panel asks the route rather than guessing from a
 * role string: the route is the authority on who may raise a limit, and a
 * client-side guess would be a second, drifting copy of that rule.
 */

/** Empty means *no limit of that kind*, which is not the same as zero. */
type Draft = { windowHours: string; usd: string; tokens: string };

function draftOf(budget: AdminBudget): Draft {
  return {
    windowHours: String(budget.ceiling.window_hours),
    usd: budget.ceiling.usd_per_window === null ? "" : String(budget.ceiling.usd_per_window),
    tokens:
      budget.ceiling.tokens_per_window === null
        ? ""
        : String(budget.ceiling.tokens_per_window),
  };
}

/**
 * A field to a number, or null for "no limit".
 *
 * Blank is null and "0" is zero, and the difference matters more here than
 * anywhere else in the app: null lets every run through and 0 stops the next
 * one. `Number("")` is 0, which would silently turn a cleared field into a
 * total freeze, so the blank check comes first and on purpose.
 */
function limitOf(raw: string): number | null {
  const value = raw.trim();
  if (!value) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : null;
}

function invalid(draft: Draft): string {
  const hours = Number(draft.windowHours.trim());
  if (!Number.isInteger(hours) || hours < 1 || hours > 8760) {
    return "The window must be a whole number of hours between 1 and 8760.";
  }
  for (const [raw, what] of [
    [draft.usd, "dollar ceiling"],
    [draft.tokens, "token ceiling"],
  ] as const) {
    const value = raw.trim();
    if (value && !(Number.isFinite(Number(value)) && Number(value) >= 0)) {
      return `The ${what} must be a number of 0 or more, or empty for no limit.`;
    }
  }
  return "";
}

function inputOf(draft: Draft): AdminBudgetInput {
  return {
    window_hours: Number(draft.windowHours.trim()),
    usd_per_window: limitOf(draft.usd),
    tokens_per_window: limitOf(draft.tokens),
  };
}

/** What a PUT actually did, said plainly — including when it released nothing. */
function releaseNote(saved: AdminBudget): string {
  if (saved.resumed_run_ids.length > 0) {
    const count = saved.resumed_run_ids.length;
    return `Saved. ${count} parked ${count === 1 ? "run is" : "runs are"} resuming now.`;
  }
  if (saved.runs_parked_on_budget.length > 0) {
    return (
      `Saved, but ${formatCount(saved.runs_parked_on_budget.length)} run(s) are still ` +
      "parked: the new ceiling is still at or below what this window has spent."
    );
  }
  return "Saved. Nothing was parked on the old ceiling.";
}

export type CeilingEditorProps = {
  /** Unique per instance: every field here is labelled by id. */
  idPrefix: string;
  budget: AdminBudget;
  onSaved: (saved: AdminBudget) => void;
};

/**
 * The ceiling itself: three numbers, replaced as a set.
 *
 * Replace rather than patch, because the API does: an omitted limit is no limit
 * of that kind, so the form always states the whole ceiling and there is no way
 * to raise one number while silently keeping a second one you have forgotten
 * about.
 */
export function CeilingEditor({ idPrefix, budget, onSaved }: CeilingEditorProps) {
  const [draft, setDraft] = useState<Draft>(() => draftOf(budget));
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const [problem, setProblem] = useState("");
  const spend = readBudgetSpend(budget.spend);
  const risk = unpricedRisk(
    { usd_per_window: limitOf(draft.usd), tokens_per_window: limitOf(draft.tokens) },
    budget.spend,
  );

  async function submit(event: FormEvent) {
    event.preventDefault();
    const complaint = invalid(draft);
    setProblem(complaint);
    setNote("");
    if (complaint || busy) return;
    setBusy(true);
    try {
      const saved = await api.setAdminBudget(inputOf(draft));
      setNote(releaseNote(saved));
      onSaved(saved);
    } catch (caught) {
      setProblem(describeError(caught, "Could not save the spend limit"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="budget-form" onSubmit={(event) => void submit(event)}>
      <p className="budget-form-lead">
        {budget.enforced
          ? `Now ${ceilingPhrase(budget.ceiling)} per ${hoursLabel(
              budget.ceiling.window_hours,
            )}. This window: ${formatCount(budget.spend.total_tokens)} tokens, ` +
            `${spend.kind === "unknown" ? "nothing priced" : `${spend.label} spent`}.`
          : "No limit set."}
      </p>

      <div className="budget-fields">
        <label htmlFor={`${idPrefix}-usd`}>
          Dollar ceiling
          <input
            id={`${idPrefix}-usd`}
            type="number"
            min={0}
            step="0.01"
            inputMode="decimal"
            value={draft.usd}
            onChange={(event) => setDraft({ ...draft, usd: event.target.value })}
          />
        </label>
        <label htmlFor={`${idPrefix}-tokens`}>
          Token ceiling
          <input
            id={`${idPrefix}-tokens`}
            type="number"
            min={0}
            step={1000}
            inputMode="numeric"
            value={draft.tokens}
            onChange={(event) => setDraft({ ...draft, tokens: event.target.value })}
          />
        </label>
        <label htmlFor={`${idPrefix}-window`}>
          Window (hours)
          <input
            id={`${idPrefix}-window`}
            type="number"
            min={1}
            max={8760}
            step={1}
            inputMode="numeric"
            value={draft.windowHours}
            onChange={(event) => setDraft({ ...draft, windowHours: event.target.value })}
          />
        </label>
      </div>

      {risk && (
        <p className="budget-warning">
          <TriangleAlert size={13} />
          {risk}
        </p>
      )}
      {problem && (
        <p className="budget-problem" role="alert">
          {problem}
        </p>
      )}
      {note && (
        <p className="budget-note" role="status">
          {note}
        </p>
      )}

      <div className="budget-form-actions">
        <button type="submit" className="primary-button" disabled={busy}>
          {busy ? "Saving…" : "Save limit"}
        </button>
      </div>
    </form>
  );
}

type Loaded =
  | { state: "loading" }
  | { state: "forbidden" }
  | { state: "failed"; message: string }
  | { state: "ready"; budget: AdminBudget };

/**
 * Fetch the ceiling, and keep the 403 as an answer rather than an error.
 *
 * Every caller here is a place a *member* can stand — a chat turn that parked,
 * a workflow run that stopped — so "you are not an owner" is the expected
 * outcome for some of them and must not reach the error toast.
 */
function useBudget() {
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });

  const reload = useCallback(async () => {
    try {
      setLoaded({ state: "ready", budget: await api.getAdminBudget() });
    } catch (caught) {
      if (caught instanceof ApiError && caught.forbidden) {
        setLoaded({ state: "forbidden" });
        return;
      }
      setLoaded({
        state: "failed",
        message: describeError(caught, "Could not read the spend limit"),
      });
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const replace = useCallback((budget: AdminBudget) => {
    setLoaded({ state: "ready", budget });
  }, []);

  return { loaded, reload, replace };
}

/**
 * The contents of the "raise the limit" popover: the form, or the reason there
 * is no form.
 */
function CeilingMenuBody({
  idPrefix,
  onSaved,
}: {
  idPrefix: string;
  onSaved: (saved: AdminBudget) => void;
}) {
  const { loaded, replace } = useBudget();

  if (loaded.state === "loading") {
    return <p className="budget-menu-note">Reading this workspace’s limit…</p>;
  }
  if (loaded.state === "forbidden") {
    return (
      <p className="budget-menu-note">
        Only an owner of this workspace can change the spend limit. Ask one to
        raise it — the run stays parked until they do, and resumes by itself the
        moment they save. Nothing is lost while it waits.
      </p>
    );
  }
  if (loaded.state === "failed") {
    return <p className="budget-menu-note">{loaded.message}</p>;
  }
  return (
    <CeilingEditor
      idPrefix={idPrefix}
      budget={loaded.budget}
      onSaved={(saved) => {
        replace(saved);
        onSaved(saved);
      }}
    />
  );
}

export type CeilingMenuProps = {
  /** Unique per mounted menu: it becomes the popover's id and its field ids. */
  id: string;
  onSaved?: (saved: AdminBudget) => void;
};

/** The way forward, where the wall is. Owner-only, and it says so if you aren't. */
export function CeilingMenu({ id, onSaved }: CeilingMenuProps) {
  return (
    <DisclosureMenu
      id={id}
      triggerLabel="Raise the spend limit"
      triggerClassName="primary-button budget-trigger"
      trigger={
        <>
          <Gauge size={13} /> Raise the limit
        </>
      }
      menuLabel="Spend limit"
      className="budget-disclosure"
    >
      {(close) => (
        <CeilingMenuBody
          idPrefix={id}
          onSaved={(saved) => {
            onSaved?.(saved);
            // Left open on purpose when nothing was released: the note under
            // the form is the answer to what just happened, and closing over it
            // would leave the user looking at the same parked run with no
            // explanation of why their raise did not help.
            if (saved.resumed_run_ids.length > 0) close();
          }}
        />
      )}
    </DisclosureMenu>
  );
}

export type BudgetHoldProps = {
  /**
   * The `run.waiting_for_budget` payload when we have it — chat streams it. A
   * workflow run reports only that it is parked on budget, so this is null
   * there and the panel says less rather than inventing figures.
   */
  park: BudgetPark | null;
  /** Unique id for the popover this panel owns. */
  menuId: string;
  onReleased?: () => void;
};

/**
 * A run stopped by the ceiling, explained.
 *
 * Deliberately **not** a tool card. `_park_for_budget` writes no `AgentToolCall`
 * — the model had not been asked yet, so there is no proposed call — and an
 * approve/deny pair here would approve nothing and call an endpoint that has no
 * record to decide. What this offers instead is the only thing that actually
 * releases the run: a higher ceiling.
 */
export function BudgetHold({ park, menuId, onReleased }: BudgetHoldProps) {
  const note = park ? describeBudgetPark(park) : null;
  return (
    <section className="budget-hold" role="status">
      <div className="budget-hold-head">
        <CirclePause size={15} />
        <div>
          <strong>{note ? note.headline : "Held by the spend limit"}</strong>
          <p>
            {note
              ? note.detail
              : "This run stopped just before its next model call because this " +
                "workspace reached its spend ceiling. Nothing was written and " +
                "nothing failed — it picks up where it stopped once the ceiling " +
                "is raised past it."}
          </p>
        </div>
      </div>

      {note && note.facts.length > 0 && (
        <dl className="budget-facts">
          {note.facts.map((fact) => (
            <div key={fact.label}>
              <dt>{fact.label}</dt>
              <dd className={fact.muted ? "muted" : undefined} title={fact.title}>
                {fact.value}
              </dd>
            </div>
          ))}
        </dl>
      )}

      <div className="budget-hold-foot">
        <p>
          {note
            ? note.remedy
            : "Raising the ceiling releases it; so does waiting for the window to roll."}
        </p>
        <CeilingMenu id={menuId} onSaved={() => onReleased?.()} />
      </div>
    </section>
  );
}

export type BudgetPanelProps = {
  budget: AdminBudget;
  /** Re-reads the panel's own data after a save changed it. */
  onSaved: (saved: AdminBudget) => void;
};

/**
 * The Admin screen's ceiling, next to the usage panel that shows the spend.
 *
 * They belong side by side and not on separate screens: a ceiling with no
 * current figure beside it cannot be raised by the right amount, and a spend
 * figure with no ceiling beside it does not say whether it is near anything.
 *
 * The two spend readings are shown separately because they are measured over
 * different things — the workspace ceiling covers everything, the unattended
 * one covers only workflow nodes and is measured against workflow spend alone,
 * so a busy afternoon of chat cannot starve tonight's report.
 */
export function BudgetPanel({ budget, onSaved }: BudgetPanelProps) {
  const note = describeCeiling(budget);
  const spend = readBudgetSpend(budget.spend);
  const unattended = readBudgetSpend(budget.unattended_spend);
  const NoticeIcon = note.tone === "live" ? Gauge : TriangleAlert;

  return (
    <section className="admin-panel budget-panel">
      <div className="panel-title">
        <div>
          <strong>Spend ceiling</strong>
        </div>
        {budget.runs_parked_on_budget.length > 0 && (
          <span className="panel-count">{budget.runs_parked_on_budget.length} parked</span>
        )}
      </div>

      <div className={`usage-notice ${note.tone === "live" ? "clear" : ""}`}>
        <NoticeIcon size={14} />
        <div>
          <strong>{note.headline}</strong>
          <p>{note.detail}</p>
        </div>
      </div>

      <div className="admin-stats budget-stats">
        <div>
          <strong>{ceilingPhrase(budget.ceiling)}</strong>
          <span>workspace, per {hoursLabel(budget.ceiling.window_hours)}</span>
        </div>
        <div>
          <strong className={spend.kind === "unknown" ? "muted" : undefined} title={spend.detail}>
            {spend.label}
          </strong>
          <span>spent{spend.kind === "floor" ? " (at least)" : ""}</span>
        </div>
        <div>
          <strong>{formatCount(budget.spend.total_tokens)}</strong>
          <span>tokens in the window</span>
        </div>
        <div>
          <strong>{ceilingPhrase(budget.unattended_ceiling)}</strong>
          <span>unattended workflows</span>
        </div>
        <div>
          <strong
            className={unattended.kind === "unknown" ? "muted" : undefined}
            title={unattended.detail}
          >
            {unattended.label}
          </strong>
          <span>spent unattended</span>
        </div>
      </div>

      {budget.runs_parked_on_budget.length > 0 && (
        <div className="budget-parked">
          <h3>Parked on this ceiling</h3>
          <ul className="admin-list">
            {budget.runs_parked_on_budget.map((run) => (
              <li key={run.run_id}>
                <div>
                  <strong>Run {shortId(run.run_id)}</strong>
                  <span title={run.run_id}>
                    Waiting since {formatRelative(run.created_at)} · raising the ceiling
                    below releases it
                  </span>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      <CeilingEditor idPrefix="admin-budget" budget={budget} onSaved={onSaved} />
    </section>
  );
}
