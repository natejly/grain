"use client";

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
 */
export type WorkspaceSettingsMenuProps = {
  activeGroup: GroupId;
  open: (groupId: GroupId) => void;
};

export function WorkspaceSettingsMenu({ activeGroup, open }: WorkspaceSettingsMenuProps) {
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
      {(close) =>
        SETTINGS_GROUPS.map((group) => {
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
        })
      }
    </DisclosureMenu>
  );
}
