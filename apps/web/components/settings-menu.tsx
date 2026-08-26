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
 * The one preference living directly in the panel is the daily digest: a
 * per-member mail opt-in, so it belongs beside the member's own controls
 * rather than on an admin page that implies it is done to the workspace.
 */
export type WorkspaceSettingsMenuProps = {
  activeGroup: GroupId;
  open: (groupId: GroupId) => void;
  /** The caller's digest opt-in; null until bootstrap lands (controls hidden). */
  digest: DigestPrefs | null;
  onDigestChange: (prefs: DigestPrefs) => void;
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
