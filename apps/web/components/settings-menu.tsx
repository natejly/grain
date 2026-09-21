"use client";

import type { DigestPrefs, StylePreset } from "@workspace/api-client";
import { ChevronDown, Settings } from "lucide-react";
import { useState } from "react";
import { DisclosureMenu } from "./disclosure-menu";
import { SETTINGS_GROUPS, type GroupId } from "./views/navigation";

/**
 * Workspace settings — the places you configure rather than work in.
 *
 * This menu used to be called "Settings" and to hide the approval queue, with
 * the count of runs parked on a human riding a gear icon where it read as
 * configuration noise. The queue is the Inbox rail destination now, and this
 * menu carries no badge at all: nothing behind it waits on anyone. What
 * remains really is configuration — Connections and Admin — visited rarely
 * and on purpose.
 *
 * The preferences living directly in the panel are the per-member ones — the
 * daily digest and Safe mode — because they belong beside the member's own
 * controls rather than on an admin page that implies they are done to the
 * workspace.
 *
 * Safe mode is here and NOT in the composer on purpose. The composer already
 * has the approval picker, and that one governs the thread you are looking at;
 * this one only decides what the NEXT thread starts as. Two controls that both
 * said "ask before writes" in the same corner of the screen would be read as
 * one control, and the one that did not change the thread in front of you
 * would be the one that got blamed.
 */
export type WorkspaceSettingsMenuProps = {
  activeGroup: GroupId;
  open: (groupId: GroupId) => void;
  /** The caller's digest opt-in; null until bootstrap lands (controls hidden). */
  digest: DigestPrefs | null;
  onDigestChange: (prefs: DigestPrefs) => void;
  /** Safe mode: new threads start by asking before they write. */
  safeMode: boolean;
  onSafeModeChange: (enabled: boolean) => void;
  /**
   * Whether this member's runs recall and store memories. Null until the
   * bootstrap read lands (control hidden, like the digest) — the toggle
   * governs what the assistant learns about you, which must not be
   * misreported for even a second. Optional so existing bare mounts stand.
   */
  memoryEnabled?: boolean | null;
  onMemoryEnabledChange?: (enabled: boolean) => void;
  /**
   * The member's response style. Null until bootstrap lands (section hidden,
   * like the digest and memory) and on a bare mount, so older call sites
   * stand unchanged. "normal" means no style instruction at all — the hint
   * says so, because that boundary is the whole setting.
   */
  stylePreset?: string | null;
  customStyleText?: string;
  onStyleChange?: (preset: StylePreset, customText: string) => void;
};

/** The preset rows the style select offers, in menu order. */
export const STYLE_PRESETS: { value: StylePreset; label: string }[] = [
  { value: "normal", label: "Normal" },
  { value: "concise", label: "Concise" },
  { value: "explanatory", label: "Explanatory" },
  { value: "formal", label: "Formal" },
  { value: "custom", label: "Custom" },
];

/**
 * Its own component so the picked-but-unsaved "custom" choice and the textarea
 * draft live INSIDE the popover panel: closing it unmounts them, and the next
 * open greets the saved preference rather than a half-typed one.
 */
function StyleSection({
  preset,
  customText,
  onChange,
}: {
  preset: string;
  customText: string;
  onChange: (preset: StylePreset, customText: string) => void;
}) {
  const [choice, setChoice] = useState(preset);
  const [draft, setDraft] = useState(customText);
  const saveCustom = () => {
    // An empty custom block is unrepresentable server-side (422); the Save
    // simply stays inert until there is something to say.
    if (draft.trim()) onChange("custom", draft.trim());
  };
  return (
    <>
      <p className="disclosure-note">Response style</p>
      <label className="approval-assignee">
        Style
        <select
          value={choice}
          aria-label="Response style"
          onChange={(event) => {
            const next = event.target.value as StylePreset;
            setChoice(next);
            // A fixed preset saves on pick; "custom" waits for its text.
            // The pick carries the LIVE draft (falling back to the saved
            // prose when the draft is blank): clicking the select blurs the
            // textarea first, so the blur-save and this pick race as two
            // PUTs — making the pick self-contained means whichever lands
            // last leaves the server holding the text the user actually
            // typed, and updateStylePref sequences the calls so the pick,
            // being the last gesture, is the state both pickers show.
            if (next !== "custom") onChange(next, draft.trim() || customText);
          }}
        >
          {STYLE_PRESETS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
      {choice === "custom" && (
        <>
          <textarea
            className="style-custom-text"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onBlur={() => {
              if (draft.trim() && draft.trim() !== customText) saveCustom();
            }}
            maxLength={2000}
            rows={3}
            aria-label="Custom style instructions"
            placeholder="How should answers to you read?"
          />
          <button
            type="button"
            className="ghost-button"
            disabled={!draft.trim()}
            onClick={saveCustom}
          >
            Save style
          </button>
        </>
      )}
      {/* Names the two boundaries: persistent (every thread, future turns
          only) and that Normal is the absence of an instruction, not a
          fifth flavour of one. */}
      <p className="disclosure-hint">
        Applies to your future turns in every thread. Normal means no style
        instruction at all.
      </p>
    </>
  );
}

/** "9" reads as "09:00 UTC" — the mail goes out after the hour, on the tick. */
export function digestHourLabel(hour: number): string {
  return `${String(hour).padStart(2, "0")}:00 UTC`;
}

export function WorkspaceSettingsMenu({
  activeGroup,
  open,
  digest,
  onDigestChange,
  safeMode,
  onSafeModeChange,
  memoryEnabled = null,
  onMemoryEnabledChange,
  stylePreset = null,
  customStyleText = "",
  onStyleChange,
}: WorkspaceSettingsMenuProps) {
  const inSettings = SETTINGS_GROUPS.some((group) => group.id === activeGroup);

  return (
    <DisclosureMenu
      id="settings-menu"
      triggerLabel="Workspace settings"
      triggerClassName={inSettings ? "chrome-button active" : "chrome-button"}
      trigger={
        <>
          <Settings size={15} />
          <span className="chrome-button-label">Settings</span>
          <ChevronDown size={13} />
        </>
      }
      menuLabel="Workspace settings"
    >
      {(close) => (
        <>
          {SETTINGS_GROUPS.map((group) => {
            const Icon = group.icon;
            return (
              <button
                key={group.id}
                className={
                  activeGroup === group.id
                    ? "disclosure-option active"
                    : "disclosure-option"
                }
                aria-current={activeGroup === group.id ? "page" : undefined}
                onClick={() => {
                  close();
                  open(group.id);
                }}
              >
                <Icon size={14} />
                <span className="disclosure-option-name">{group.label}</span>
              </button>
            );
          })}
          <p className="disclosure-note">Safe mode</p>
          <label className="approval-remember">
            <input
              type="checkbox"
              checked={safeMode}
              onChange={(event) => onSafeModeChange(event.target.checked)}
            />
            Ask me before the assistant writes anything
          </label>
          {/* Says what the setting DOES rather than what it is, and names the
              boundary the toggle actually has: it seeds new threads, so a
              member who flips it looking for the thread on screen to change is
              told here instead of by the thread not changing. */}
          <p className="disclosure-hint">
            {safeMode
              ? "New threads start in “Ask before writes”. Threads already open keep the mode they are in."
              : "New threads act on their own and show you what ran. Denied tools stay denied, and anything flagged still asks."}
          </p>
          {memoryEnabled !== null && (
            <>
              <p className="disclosure-note">Memory</p>
              <label className="approval-remember">
                <input
                  type="checkbox"
                  checked={memoryEnabled}
                  onChange={(event) =>
                    onMemoryEnabledChange?.(event.target.checked)
                  }
                />
                Remember things from my chats
              </label>
              {/* Same doctrine as the Safe mode hint: say what each edge DOES.
                  Off has to name its two boundaries — the explicit remember
                  tool still works (an instruction outranks a default), and
                  nothing already learned is deleted — or a member flips it
                  expecting an erasure this toggle does not perform. */}
              <p className="disclosure-hint">
                {memoryEnabled
                  ? "Your runs recall saved memories and store new ones. Temporary chats never do either."
                  : "Your runs neither recall nor store memories. Asking the assistant to remember something still works, and the Memory page keeps what it already learned."}
              </p>
            </>
          )}
          {stylePreset !== null && onStyleChange && (
            <StyleSection
              preset={stylePreset}
              customText={customStyleText}
              onChange={onStyleChange}
            />
          )}
          {digest && (
            <>
              <p className="disclosure-note">Daily digest</p>
              <label className="approval-remember">
                <input
                  type="checkbox"
                  checked={digest.enabled}
                  onChange={(event) =>
                    onDigestChange({ ...digest, enabled: event.target.checked })
                  }
                />
                Email me a daily digest of items waiting on me
              </label>
              {digest.enabled && (
                <label className="approval-assignee">
                  Send after
                  <select
                    value={digest.hour_utc}
                    aria-label="Hour the daily digest is sent, in UTC"
                    onChange={(event) =>
                      onDigestChange({
                        ...digest,
                        hour_utc: Number(event.target.value),
                      })
                    }
                  >
                    {Array.from({ length: 24 }, (_, hour) => (
                      <option key={hour} value={hour}>
                        {digestHourLabel(hour)}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </>
          )}
        </>
      )}
    </DisclosureMenu>
  );
}
