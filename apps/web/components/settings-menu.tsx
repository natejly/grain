"use client";

import type { DigestPrefs } from "@workspace/api-client";
import { ChevronDown, Settings } from "lucide-react";
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
};

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
