"use client";

import type { CoverageLedger } from "@workspace/api-client";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import { api } from "../api";
import { NO_COVERAGE, summarizeCoverage } from "./coverage-format";

/**
 * What a run actually looked at, under the answer it produced.
 *
 * FETCHED ON DEMAND, not on render, and that is the whole design. Only a
 * plan-mode run or the deliverable preset writes a ledger, so most runs have
 * none — a drawer that fetched eagerly would fire one 404 per assistant
 * message in the transcript to learn that. Asking costs one request, and only
 * when somebody wants to know.
 *
 * A 404 is INFORMATION here, not a failure: "no coverage recorded for this
 * run" is the ordinary answer. It renders as that sentence and never reaches
 * the error toast.
 */
export function CoverageDrawer({ runId }: { runId: string }) {
  const [ledger, setLedger] = useState<CoverageLedger | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "none" | "ready">("idle");

  async function open() {
    if (state !== "idle") return;
    setState("loading");
    try {
      const found = await api.getRunCoverage(runId);
      setLedger(found);
      setState("ready");
    } catch {
      // Every failure reads the same way, deliberately: a run with no ledger
      // and a run whose ledger could not be fetched are both "nothing to show
      // here", and neither is worth a toast over a diagnostic.
      setState("none");
    }
  }

  return (
    <details
      className="coverage-ledger"
      onToggle={(event) => {
        if ((event.currentTarget as HTMLDetailsElement).open) void open();
      }}
    >
      <summary>
        {state === "ready" && ledger
          ? summarizeCoverage(ledger)
          : state === "none"
            ? NO_COVERAGE
            : state === "loading"
              ? "Checking coverage…"
              : "Coverage"}
      </summary>
      {state === "ready" && ledger && (
        <ReactMarkdown>{ledger.report_markdown}</ReactMarkdown>
      )}
    </details>
  );
}
