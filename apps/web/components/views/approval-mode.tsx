"use client";

import { Check, ChevronDown, ShieldCheck, Zap } from "lucide-react";
import type { AgentToolCall, ApprovalMode } from "@workspace/api-client";
import { useState } from "react";
import { DisclosureMenu } from "../disclosure-menu";
import { APPROVAL_MODES, describeMode, summariseAutoApproved } from "./approval-format";

/**
 * How much this thread asks before acting, and — far more important — whether
 * it has stopped asking.
 *
 * The dangerous state is not turning the bypass on. It is forgetting it is on
 * three days later, in a thread you reopened, while pasting in something you
 * did not write. So this is deliberately not a settings checkbox that looks the
 * same either way:
 *
 *   - the picker's trigger changes shape and wording in the bypass, so the
 *     control itself never reads as neutral;
 *   - a banner sits in the composer zone — not in the transcript — because the
 *     transcript scrolls and the composer does not, so the warning cannot be
 *     left behind above the fold;
 *   - the banner names the *thread*, since the mode is per conversation, and
 *     switching threads visibly changes it;
 *   - it lists what it has already let through, so the trail is legible where
 *     the writes happened rather than only in an audit table;
 *   - turning it off is one click from the warning, not a trip back to a menu.
 */

export type ApprovalModeControlProps = {
  mode: ApprovalMode;
  setMode: (mode: ApprovalMode) => Promise<void>;
};

export function ApprovalModeControl({ mode, setMode }: ApprovalModeControlProps) {
  const current = describeMode(mode);
  const Icon = current.bypass ? Zap : ShieldCheck;

  return (
    <DisclosureMenu
      id="chat-approval-mode"
      className="approval-mode"
      triggerLabel={`Approval mode: ${current.label}`}
      triggerClassName={current.bypass ? "approval-trigger bypass" : "approval-trigger"}
      trigger={
        <>
          <Icon size={14} aria-hidden="true" />
          <span>{current.label}</span>
          <ChevronDown size={13} aria-hidden="true" />
        </>
      }
      menuLabel="Approval mode"
    >
      {(close) => (
        <>
          {APPROVAL_MODES.map((option) => (
            <button
              key={option.mode}
              type="button"
              className={option.mode === mode ? "approval-option active" : "approval-option"}
              // Pressed rather than checked: these are buttons in a group, and
              // a screen reader has to be able to hear which one is in force
              // without reading the tick glyph.
              aria-pressed={option.mode === mode}
              onClick={() => {
                close();
                void setMode(option.mode);
              }}
            >
              <span className="approval-option-label">
                {option.mode === mode && <Check size={13} aria-hidden="true" />}
                {option.label}
              </span>
              <span className="approval-option-detail">{option.detail}</span>
            </button>
          ))}
          <p className="approval-note">Applies to this thread only.</p>
        </>
      )}
    </DisclosureMenu>
  );
}

export type UnrestrictedIndicatorProps = {
  /** What has gone through unreviewed in this thread, oldest first. */
  approved: AgentToolCall[];
};

/**
 * The development bypass, said out loud.
 *
 * `DEV_UNRESTRICTED_AGENT` is a stronger `auto_writes`: nothing parks AND the
 * per-subject tool scoping is off, so a panel about one paragraph of prose can
 * reach the filesystem tools. The failure mode is the same one the mode banner
 * exists for — not switching it on, but forgetting it is on — so it wears the
 * same shape rather than a quieter second treatment.
 *
 * Two deliberate differences from `BypassIndicator`. There is no "Turn off"
 * button, because this is not a per-thread setting a click can change: it is an
 * environment variable the server refuses to accept outside development, and a
 * button that could not do what it said would be worse than none. And it says
 * *every thread*, not this one, because that is what is true.
 */
export function UnrestrictedIndicator({ approved }: UnrestrictedIndicatorProps) {
  return (
    <div className="bypass-banner" role="status">
      <Zap size={15} aria-hidden="true" />
      <div className="bypass-copy">
        <strong>Development mode: no approvals, every tool</strong>
        <span>
          {approved.length === 0
            ? "Nothing has gone through unreviewed yet. Applies to every thread."
            : `${approved.length} ${approved.length === 1 ? "call" : "calls"} went through unreviewed · ${summariseAutoApproved(approved)}`}
        </span>
      </div>
    </div>
  );
}

export type BypassIndicatorProps = {
  /** The thread's title, so the warning says which conversation it governs. */
  conversationTitle: string;
  /** What the bypass has let through, oldest first. */
  approved: AgentToolCall[];
  stop: () => Promise<void>;
  /**
   * How loudly to say it.
   *
   * "warning" is the original treatment, and it is for the case this component
   * was built for: writes running unreviewed in a thread belonging to someone
   * who asked to be asked. "notice" is the same trail — same list, same "Turn
   * off" — with the alarm taken out, for a member whose default this simply is.
   *
   * The split is not cosmetic. An alarm that is on by default on every thread
   * stops being read within a week, and then it is not there for the thread
   * that needed it. Defaults to the warning: a caller that has not thought
   * about which case it is in should get the louder one.
   */
  tone?: "warning" | "notice";
};

export function BypassIndicator({
  conversationTitle,
  approved,
  stop,
  tone = "warning",
}: BypassIndicatorProps) {
  const [open, setOpen] = useState(false);
  const notice = tone === "notice";

  if (notice) {
    return (
      <div className="bypass-banner notice" role="status">
        <Zap size={14} aria-hidden="true" />
        <div className="bypass-copy">
          <span>
            {approved.length === 0
              ? "Acting without asking — anything it runs shows up here."
              : `${approved.length} ${approved.length === 1 ? "call" : "calls"} ran without asking · ${summariseAutoApproved(approved)}`}
          </span>
        </div>
        {approved.length > 0 && (
          <button
            type="button"
            className="ghost-button"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
          >
            {open ? "Hide" : "Show"} what ran
          </button>
        )}
        {/* Kept even in the quiet treatment: the whole argument for an agentic
            default is that stopping it is always one click away, wherever the
            person happens to be looking when they change their mind. */}
        <button type="button" className="ghost-button" onClick={() => void stop()}>
          Ask me first
        </button>
        {open && (
          <ul className="bypass-trail">
            {approved.map((call) => (
              <li key={call.id}>
                <span className="tool-name">{call.name}</span>
                <span>{call.proposal_preview || call.result_preview}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    );
  }

  return (
    // role="status" and not "alert": this is a standing condition, not an event
    // that just happened, and an assertive live region would interrupt a screen
    // reader on every re-render of the composer it lives in.
    <div className="bypass-banner" role="status">
      <Zap size={15} aria-hidden="true" />
      <div className="bypass-copy">
        <strong>Auto-approving writes in “{conversationTitle}”</strong>
        <span>
          {approved.length === 0
            ? "Nothing has gone through unreviewed yet."
            : `${approved.length} ${approved.length === 1 ? "call" : "calls"} went through unreviewed · ${summariseAutoApproved(approved)}`}
        </span>
      </div>
      {approved.length > 0 && (
        <button
          type="button"
          className="ghost-button"
          aria-expanded={open}
          onClick={() => setOpen((value) => !value)}
        >
          {open ? "Hide" : "Show"} what ran
        </button>
      )}
      <button type="button" className="primary-button" onClick={() => void stop()}>
        Turn off
      </button>
      {open && (
        <ul className="bypass-trail">
          {approved.map((call) => (
            <li key={call.id}>
              <span className="tool-name">{call.name}</span>
              <span>{call.proposal_preview || call.result_preview}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
