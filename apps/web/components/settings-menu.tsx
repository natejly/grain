"use client";

import { ChevronDown, Settings } from "lucide-react";
import { DisclosureMenu } from "./disclosure-menu";
import { SETTINGS_GROUPS, type GroupId } from "./views/navigation";

/**
 * Settings — the places you configure and audit rather than work in.
 *
 * Connections, Activity and Admin each held a permanent slot on the rail, which
 * put "which OAuth apps am I connected to" at the same weight as "my documents".
 * They are visited rarely, so they moved behind one trigger; what does *not*
 * move behind it is the count of approvals waiting on a human, which rides on
 * the trigger itself so a parked agent run is still visible from anywhere.
 */
export type SettingsMenuProps = {
  activeGroup: GroupId;
  /** Per-group badge totals, for the groups that have something to count. */
  counts: Partial<Record<GroupId, number>>;
  approvals: number;
  open: (groupId: GroupId) => void;
};

export function SettingsMenu({
  activeGroup,
  counts,
  approvals,
  open,
}: SettingsMenuProps) {
  const inSettings = SETTINGS_GROUPS.some((group) => group.id === activeGroup);

  return (
    <DisclosureMenu
      id="settings-menu"
      triggerLabel={
        approvals > 0
          ? `Settings (${approvals} awaiting approval)`
          : "Settings"
      }
      triggerClassName={inSettings ? "chrome-button active" : "chrome-button"}
      trigger={
        <>
          <Settings size={15} />
          <span className="chrome-button-label">Settings</span>
          {approvals > 0 && <span className="approval-count">{approvals}</span>}
          <ChevronDown size={13} />
        </>
      }
      menuLabel="Settings"
    >
      {(close) =>
        SETTINGS_GROUPS.map((group) => {
          const Icon = group.icon;
          const count = counts[group.id];
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
              {count !== undefined && <span className="nav-count">{count}</span>}
              {group.id === "activity" && approvals > 0 && (
                <span className="approval-count">{approvals}</span>
              )}
            </button>
          );
        })
      }
    </DisclosureMenu>
  );
}
