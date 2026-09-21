"use client";

import type { GroundedReceipt, GroundedReceiptDetail } from "@workspace/api-client";
import { ApiError } from "@workspace/api-client";
import { useEffect, useState } from "react";
import { api } from "../api";
import { CitationVerdictNote, GroundingDrawer } from "./citation-note";
import { describeError, formatRelative } from "./shared";

/**
 * What the grounded-answer API has been asked, and what it said.
 *
 * `POST /api/answers/grounded` is a machine door: a script holds a workspace
 * token and gets back an answer with its citations and its grounding verdict.
 * Nothing about that is visible from inside the product, which is the gap this
 * panel closes — and it belongs on API & Webhooks because this is the surface
 * the token was minted from.
 *
 * Two deliberate scope choices. It is OWNER-gated, like the tokens: a receipt
 * holds the answer text, and the credential that produced it is an owner's to
 * issue and revoke. And it is read-only: there is nothing to do to a receipt,
 * and the answer's own verdict is the thing worth reading.
 *
 * The verdict is rendered by the same components the transcript uses, so the
 * ledger and the chat cannot come to disagree about what "verified" looks like
 * — or about what it means, which is that the sentence's words appear in the
 * passage it cites, not that the sentence is true.
 */

export type GroundedReceiptsPanelProps = {
  setError: (message: string) => void;
};

export function GroundedReceiptsPanel({ setError }: GroundedReceiptsPanelProps) {
  const [rows, setRows] = useState<GroundedReceipt[] | null>(null);
  const [openId, setOpenId] = useState("");
  const [detail, setDetail] = useState<GroundedReceiptDetail | null>(null);

  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const loaded = await api.listGroundedReceipts();
        if (live) setRows(loaded);
      } catch (caught) {
        // A 403 is the ordinary case for a member on this page: the panel is
        // not theirs and renders nothing. Anything else is a real failure.
        if (caught instanceof ApiError && caught.status === 403) {
          if (live) setRows([]);
          return;
        }
        setError(describeError(caught, "Could not load the grounded answers"));
      }
    })();
    return () => {
      live = false;
    };
  }, [setError]);

  async function toggle(receipt: GroundedReceipt) {
    if (openId === receipt.id) {
      setOpenId("");
      setDetail(null);
      return;
    }
    setOpenId(receipt.id);
    setDetail(null);
    try {
      setDetail(await api.getGroundedReceipt(receipt.id));
    } catch (caught) {
      setError(describeError(caught, "Could not open that receipt"));
    }
  }

  if (!rows || rows.length === 0) return null;

  return (
    <section className="admin-panel">
      <div className="panel-title">
        <div>
          <strong>Grounded answers</strong>
          <span>{rows.length} recorded</span>
        </div>
      </div>

      <p className="field-hint">
        Every question answered through <code>POST /api/answers/grounded</code>,
        with the passages it was answered from. The grounding score is a check
        of support, not of truth: it says how much of each cited sentence&rsquo;s
        wording appears in the passage it cites.
      </p>

      <ul className="share-link-list">
        {rows.map((row) => (
          <li key={row.id}>
            <div>
              <button
                type="button"
                className="ghost-button"
                aria-expanded={openId === row.id}
                onClick={() => void toggle(row)}
              >
                {row.question || "(no question recorded)"}
              </button>
              <span className="share-link-meta">
                {row.evidence_count}{" "}
                {row.evidence_count === 1 ? "passage" : "passages"} ·{" "}
                {/*
                  `scored === 0` is "nothing here made a checkable claim", and
                  the schema says so in as many words: it must not be rendered
                  as a 0%. The row used to print one anyway, while the drawer
                  below it — reading the same verdict through
                  `describeGrounding` — rendered nothing at all, so the summary
                  and the detail disagreed about the same receipt.
                */}
                {row.scored === 0
                  ? "no checkable claim"
                  : `${Math.round(row.grounding_score * 100)}% grounded`}{" "}
                · {row.valid ? "citations resolve" : "citation problem"} ·{" "}
                {formatRelative(row.created_at)}
              </span>
            </div>
            {openId === row.id && detail && (
              <div className="grounded-receipt-detail">
                <p>{detail.answer}</p>
                {detail.report && (
                  <>
                    <CitationVerdictNote report={detail.report} />
                    <GroundingDrawer report={detail.report} />
                  </>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
