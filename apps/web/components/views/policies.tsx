"use client";

import type {
  PolicyScope,
  TaintGatingMode,
  TaintStatus,
  ToolPolicy,
} from "@workspace/api-client";
import { ApiError } from "@workspace/api-client";
import { RefreshCw, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useSession } from "../auth/session-provider";
import { OrganizationPanel } from "./organization";
import { RetrievalContractPanel } from "./retrieval-contract";
import { describeError, formatRelative } from "./shared";

/**
 * The Rules ledger: every standing tool grant that decides a call before
 * anyone is asked.
 *
 * Until this table existed the only surface a grant had was the moment of its
 * creation — the "always allow" checkbox on an approval card — and the only
 * way to revoke one was a hand-built DELETE. A permission that can be granted
 * in one click but audited nowhere is how "why did that run without asking?"
 * becomes unanswerable, so the ledger shows each rule with everything needed
 * to judge it: what the tool does (the registry description, not just its
 * machine name), which scope it covers, who it covers, who made it, and when.
 *
 * `listToolPolicies` returns the workspace's shared rows plus the caller's own
 * personal rows and never another member's — so a personal row here is always
 * "You". Shared rows are owner-managed: the revoke button disables for a
 * member, and the server re-checks regardless (the disabled control is a
 * courtesy, the 403 is the control).
 */

/** Scope, in the words a person granted it under — not the enum's. */
const SCOPE_WORDS: Record<PolicyScope, string> = {
  chat: "while chatting",
  workflow: "unattended runs",
};

/**
 * Who made a shared grant. There is no member-readable roster to resolve an id
 * against — `/api/admin/members` is owner-only and `/api/org/members` is
 * org-admin-only — so the ledger names the one grantor it can prove ("you")
 * and shows a shortened id for anyone else rather than guessing at a name.
 */
function grantorLabel(createdBy: string, selfId: string): string {
  if (!createdBy) return "";
  if (createdBy === selfId) return "you";
  return `${createdBy.slice(0, 8)}…`;
}

const rowKey = (row: ToolPolicy) =>
  `${row.shared ? "shared" : "personal"}:${row.scope}:${row.tool_name}`;

export function RulesTable() {
  const { session } = useSession();
  const [rows, setRows] = useState<ToolPolicy[]>([]);
  const [descriptions, setDescriptions] = useState<Map<string, string>>(new Map());
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState("");
  const [problem, setProblem] = useState("");

  const load = useCallback(async () => {
    try {
      const [policies, tools] = await Promise.all([
        api.listToolPolicies(),
        api.listTools(),
      ]);
      setRows(policies);
      setDescriptions(new Map(tools.map((tool) => [tool.name, tool.description])));
      setLoaded(true);
    } catch (caught) {
      setProblem(describeError(caught, "Could not load the rules"));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const owner = session?.role === "owner";
  const selfId = session?.user_id ?? "";

  async function revoke(row: ToolPolicy) {
    setProblem("");
    setBusy(rowKey(row));
    try {
      await api.deleteToolPolicy(row.tool_name, row.scope, { shared: row.shared });
      // Refetched rather than filtered locally: the delete may race another
      // member's write, and this table is the audit surface — it must show
      // what the server holds, not what this tab believes it just did.
      await load();
    } catch (caught) {
      setProblem(describeError(caught, "Could not revoke that rule"));
    } finally {
      setBusy("");
    }
  }

  return (
    <section className="admin-panel">
      <div className="panel-title">
        <div>
          <strong>Rules</strong>
          <span>Standing tool grants</span>
        </div>
        <button className="ghost-button" onClick={() => void load()}>
          <RefreshCw size={12} /> Refresh
        </button>
      </div>

      <p className="field-hint">
        A rule decides a tool call before anyone is asked. &ldquo;You&rdquo; rows
        cover only your own calls; &ldquo;Everyone&rdquo; rows cover the whole
        workspace and only an owner can revoke them.
      </p>

      {rows.length > 0 && (
        <div className="admin-table-scroll">
          <table className="admin-table">
            <thead>
              <tr>
                <th scope="col">Tool</th>
                <th scope="col">Rule</th>
                <th scope="col">Applies</th>
                <th scope="col">Covers</th>
                <th scope="col">Since</th>
                <th scope="col">
                  <span className="visually-hidden">Revoke</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const key = rowKey(row);
                // A member cannot take back what an owner set for everyone.
                const blocked = row.shared && !owner;
                return (
                  <tr key={key}>
                    <td>
                      <strong>{row.tool_name}</strong>
                      {descriptions.get(row.tool_name) && (
                        <span>{descriptions.get(row.tool_name)}</span>
                      )}
                    </td>
                    <td>
                      <span className="admin-tag">{row.policy}</span>
                    </td>
                    <td>{SCOPE_WORDS[row.scope]}</td>
                    <td>
                      <strong>{row.shared ? "Everyone" : "You"}</strong>
                      {row.shared && grantorLabel(row.created_by, selfId) && (
                        <span>set by {grantorLabel(row.created_by, selfId)}</span>
                      )}
                    </td>
                    <td>{formatRelative(row.created_at)}</td>
                    <td>
                      <button
                        className="ghost-button"
                        disabled={blocked || busy === key}
                        title={
                          blocked
                            ? "Only a workspace owner can revoke a rule that covers everyone"
                            : undefined
                        }
                        aria-label={`Revoke the ${row.shared ? "shared" : "personal"} ${row.scope} rule for ${row.tool_name}`}
                        onClick={() => void revoke(row)}
                      >
                        <Trash2 size={12} /> Revoke
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {loaded && rows.length === 0 && (
        <p className="admin-empty">
          No standing rules. Ticking &ldquo;Always allow&rdquo; on an approval
          writes one here — that tool then runs without asking, in that scope,
          until the rule is revoked.
        </p>
      )}

      {problem && (
        <p className="budget-problem" role="alert">
          {problem}
        </p>
      )}
    </section>
  );
}

/** The gated classes, in the words this page uses for them elsewhere. */
const CLASS_WORDS: Record<string, string> = {
  web_fetch: "pages fetched from the web",
  mcp_result: "results from connected MCP servers",
  sandbox_output: "output from the sandbox",
  workspace_chunk: "passages from your library",
  memory_item: "saved memories",
  tool_result: "tool output",
};

const TAINT_CHOICES: { value: TaintGatingMode; label: string; detail: string }[] = [
  {
    value: "default",
    label: "Follow the deployment",
    detail: "Whatever this installation ships with. The usual choice.",
  },
  {
    value: "on",
    label: "Always on",
    detail: "Keep the gate armed even if the deployment default changes.",
  },
  {
    value: "off",
    label: "Off",
    detail:
      "Outside content stops raising approvals. Standing denials and per-thread approval modes are unaffected.",
  },
];

export type TaintGatingPanelProps = {
  setError: (message: string) => void;
};

/**
 * The gate that ADDS approval cards, in the stack of the ones that remove them.
 *
 * Rules & policies is already "the standing grants that skip the approval card,
 * and the ceilings nothing here can loosen". This is the other direction: once
 * the assistant has read something from outside this workspace, a write or a
 * network call waits for a person for the rest of that turn — whatever the
 * thread's approval mode says, and whatever a standing "always allow" says.
 *
 * Owner-only to change, readable by everyone. The posture governs every member,
 * and a control hidden from the people it governs is how "why did that ask me?"
 * becomes unanswerable; the disabled control is a courtesy, the 403 is the
 * control.
 */
export function TaintGatingPanel({ setError }: TaintGatingPanelProps) {
  const { session } = useSession();
  const [status, setStatus] = useState<TaintStatus | null>(null);
  // A member who cannot write still reads the posture; this only disables the
  // radio, the same read-only branch RetrievalContractPanel takes on a 403.
  const [readOnly, setReadOnly] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setStatus(await api.getTaintGating());
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 403) {
        setReadOnly(true);
        return;
      }
      setError(describeError(caught, "Could not load the content gate"));
    }
  }, [setError]);

  useEffect(() => {
    void load();
  }, [load]);

  if (!status) return null;

  const owner = session?.role === "owner";
  const locked = readOnly || !owner;
  const current = (status.workspace_override || "default") as TaintGatingMode;

  async function choose(mode: TaintGatingMode) {
    if (mode === current) return;
    setBusy(true);
    try {
      await api.setTaintGating(mode);
      // Refetched, not patched locally: what the gate APPLIES is resolved
      // server-side from this row plus the deployment, and a panel that
      // rendered its own guess would disagree with the run path.
      await load();
    } catch (caught) {
      setError(describeError(caught, "Could not change the content gate"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="admin-panel">
      <div className="panel-title">
        <div>
          <strong>Outside content</strong>
          <span>{status.enabled ? "Gating on" : "Not gating"}</span>
        </div>
      </div>

      <p className="field-hint">
        When the assistant reads something from outside this workspace, anything
        it then does that changes something or reaches the network waits for a
        person — for the rest of that turn, whatever the thread is set to. It is
        decided by where the content came from, not by what it says.
      </p>

      <div className="policies-radio-group" role="group" aria-label="Outside content gate">
        {TAINT_CHOICES.map((choice) => (
          <label key={choice.value} className="approval-remember">
            <input
              type="radio"
              name="taint-gating"
              value={choice.value}
              checked={current === choice.value}
              disabled={locked || busy}
              title={
                locked
                  ? "Only a workspace owner can change the content gate"
                  : undefined
              }
              onChange={() => void choose(choice.value)}
            />
            <span>
              <strong>{choice.label}</strong> — {choice.detail}
            </span>
          </label>
        ))}
      </div>

      <p className="field-hint">
        {status.classes.length > 0
          ? `Gated right now: ${status.classes
              .map((name) => CLASS_WORDS[name] ?? name)
              .join(", ")}.`
          : "Nothing is gated right now. What the assistant reads is still recorded."}
      </p>
    </section>
  );
}

export type PoliciesViewProps = {
  setError: (message: string) => void;
};

/**
 * Rules & policies: the grants this workspace holds, above the organization
 * posture none of them can loosen.
 *
 * The org panel used to mount only inside Admin, whose owner-only fetches
 * 403-walled the whole page — so the members the posture governs were exactly
 * the people who could not read it. Its reads were never owner-gated; this
 * page is where that stops being hidden. Ledger first, ceilings under it: a
 * rule reads top-down the same way it is evaluated.
 */
export function PoliciesView({ setError }: PoliciesViewProps) {
  return (
    <section className="content-page admin-page">
      <div className="page-heading">
        <div>
          <h1>Rules &amp; policies</h1>
          <p>
            Standing grants that skip the approval card, and the organization
            ceilings nothing here can loosen.
          </p>
        </div>
      </div>
      <div className="policies-stack">
        <RulesTable />
        {/* Between the ledger and the ceilings, because that is where it sits
            in the argument: the rules above remove approval cards, this adds
            them, and the org panel below outranks both. */}
        <TaintGatingPanel setError={setError} />
        <OrganizationPanel setError={setError} />
        <RetrievalContractPanel setError={setError} />
      </div>
    </section>
  );
}
