"use client";

import type { ShareLink, ShareLinkKind } from "@workspace/api-client";
import { Check, Copy, Link2, Link2Off, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "./api";
import {
  SHARE_EXPIRY_CHOICES,
  expiresAtFrom,
  expiryLabel,
  linksFor,
  shareLinkState,
  shareLinkStateLabel,
  shareUrl,
} from "./views/share-format";
import { describeError, formatRelative } from "./views/shared";

/**
 * The share modal for one dashboard or document: mint a public link, see the
 * links that already exist, and stop any of them working.
 *
 * The raw URL follows the invitation-link contract (`views/members.tsx`): the
 * server returns it exactly once, in the 201 that minted it, so it is shown
 * here once — copy it now or mint another. The list underneath never carries
 * a token in any form; a row is its status and its dates.
 *
 * Imports `api` directly, as `members.tsx` does: the modal is self-contained
 * and touches no shell state, so threading three callbacks through two views
 * and the workspace hook would be wiring for wiring's sake.
 */
export function ShareLinksModal({
  kind,
  resourceId,
  resourceName,
  close,
}: {
  kind: ShareLinkKind;
  resourceId: string;
  resourceName: string;
  close: () => void;
}) {
  const [links, setLinks] = useState<ShareLink[]>([]);
  const [loaded, setLoaded] = useState(false);
  /** The just-minted URL — the one time it exists on this side. */
  const [minted, setMinted] = useState("");
  const [copied, setCopied] = useState(false);
  const [problem, setProblem] = useState("");
  const [busy, setBusy] = useState("");
  /** Days until the next minted link expires; "" is "never" (revocable only). */
  const [expiryDays, setExpiryDays] = useState("");

  useEffect(() => {
    let cancelled = false;
    api
      .listShareLinks()
      .then((rows) => {
        if (cancelled) return;
        setLinks(linksFor(rows, kind, resourceId));
        setLoaded(true);
      })
      .catch((caught) => {
        if (cancelled) return;
        setProblem(describeError(caught, "Could not load the existing links"));
        setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [kind, resourceId]);

  async function mint() {
    setProblem("");
    setBusy("mint");
    try {
      const created = await api.createShareLink(
        kind,
        resourceId,
        expiresAtFrom(expiryDays),
      );
      setLinks((rows) => [created.link, ...rows]);
      setMinted(shareUrl(window.location.origin, created.url_path));
      setCopied(false);
    } catch (caught) {
      setProblem(describeError(caught, "Could not create a share link"));
    } finally {
      setBusy("");
    }
  }

  async function revoke(link: ShareLink) {
    setProblem("");
    setBusy(link.id);
    try {
      const revoked = await api.revokeShareLink(link.id);
      setLinks((rows) => rows.map((row) => (row.id === revoked.id ? revoked : row)));
    } catch (caught) {
      setProblem(describeError(caught, "Could not revoke that link"));
    } finally {
      setBusy("");
    }
  }

  async function copy() {
    // Guarded rather than assumed, as in members.tsx: the Clipboard API is
    // absent on insecure origins, and the link is on screen and selectable
    // anyway — copying is the convenience, not the mechanism.
    try {
      await navigator.clipboard?.writeText(minted);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  return (
    <div className="share-modal-backdrop" onClick={close}>
      <section
        className="share-modal"
        role="dialog"
        aria-label={`Share ${resourceName}`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="share-modal-header">
          <div>
            <strong>Share “{resourceName}”</strong>
            <p className="field-hint">
              Anyone holding the link can read this {kind}. No account needed.
            </p>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="Close share dialog"
            title="Close"
            onClick={close}
          >
            <X size={15} />
          </button>
        </header>

        {problem && (
          <p className="budget-problem" role="alert">
            {problem}
          </p>
        )}

        <label className="cron-field">
          <span>Expiry</span>
          <select
            aria-label="Link expiry"
            value={expiryDays}
            onChange={(event) => setExpiryDays(event.target.value)}
          >
            {SHARE_EXPIRY_CHOICES.map((choice) => (
              <option key={choice.value} value={choice.value}>
                {choice.label}
              </option>
            ))}
          </select>
        </label>

        <button
          type="button"
          className="primary-button"
          disabled={busy === "mint"}
          onClick={() => void mint()}
        >
          <Link2 size={13} />
          {busy === "mint" ? "Creating…" : "Create share link"}
        </button>

        {minted && (
          <div className="invite-link" role="status">
            <p className="field-hint">Shown once. Copy it now.</p>
            <div className="invite-link-row">
              <code>{minted}</code>
              <button
                type="button"
                className="ghost-button"
                aria-label="Copy share link"
                onClick={() => void copy()}
              >
                {copied ? <Check size={12} /> : <Copy size={12} />}
                {copied ? "Copied" : "Copy"}
              </button>
            </div>
          </div>
        )}

        {loaded && links.length === 0 ? (
          <p className="section-note">No links yet — nobody outside can see this.</p>
        ) : (
          <ul className="share-link-list">
            {links.map((link) => {
              const state = shareLinkState(link);
              const expiry = expiryLabel(link);
              return (
                <li key={link.id}>
                  <div>
                    <span className="admin-tag">{state}</span>
                    <span className="share-link-meta">
                      {shareLinkStateLabel(state)} · created{" "}
                      {formatRelative(link.created_at)}
                      {expiry ? ` · ${expiry}` : ""}
                    </span>
                  </div>
                  {state === "active" && (
                    <button
                      type="button"
                      className="ghost-button"
                      disabled={busy === link.id}
                      onClick={() => void revoke(link)}
                    >
                      <Link2Off size={12} />
                      {busy === link.id ? "Revoking…" : "Revoke"}
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}
