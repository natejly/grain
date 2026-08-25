"use client";

import type { Bootstrap } from "@workspace/api-client";
import { ShieldAlert } from "lucide-react";
import { DisclosureMenu } from "./disclosure-menu";

/**
 * The topbar's system posture, folded into one quiet dot. It replaces the
 * screen and provider pills, which spelt their facts out permanently — chrome
 * that read the same every day is chrome nobody reads. The dot carries the one
 * bit worth ambient space (is anything wrong?) and the panel holds the rest:
 * which model is answering, whether the prompt-injection screen is watching,
 * whether the API is reachable, and — loudly — whether the development bypass
 * has every tool running unreviewed.
 */
export type SystemStatusProps = {
  /** Null until `/api/bootstrap` lands; the panel says "Loading" rather than lying. */
  bootstrap: Bootstrap | null;
  /** From the shell's one `useApiHealth` loop — the same truth the red banner reads. */
  apiDown: boolean;
};

export function SystemStatus({ bootstrap, apiDown }: SystemStatusProps) {
  // Warn for the two states a user must not tune out: the API is gone, or the
  // dev bypass means nothing is parking. The screen's mode is posture, not a
  // problem, so it never colours the dot.
  const warn = apiDown || Boolean(bootstrap?.unrestricted_agent);
  return (
    <DisclosureMenu
      id="system-status"
      className="system-status"
      triggerLabel="System status"
      trigger={
        <span
          className={warn ? "system-status-dot warn" : "system-status-dot"}
          aria-hidden="true"
        />
      }
      triggerClassName="icon-button system-status-trigger"
      menuLabel="System status"
    >
      {() => (
        <div className="system-status-panel">
          <div className="system-status-row">
            <span className="system-status-term">Model provider</span>
            <span className="system-status-value">
              {bootstrap
                ? `${bootstrap.model_provider.model} · ${
                    bootstrap.model_provider.provider === "openai"
                      ? "OpenAI"
                      : "Deterministic local provider"
                  }`
                : "Loading"}
            </span>
          </div>
          {/* Shown only when the screen is on — same rule the pill had: a
              status indicator, not a control, and the proxy URL never reaches
              the client. */}
          {bootstrap?.screen.enabled && (
            <div className="system-status-row">
              <span className="system-status-term">
                <ShieldAlert size={13} aria-hidden="true" />
                Prompt-injection screen
              </span>
              <span className="system-status-value">
                {bootstrap.screen.mode} mode · {bootstrap.screen.backend} backend
              </span>
            </div>
          )}
          <div className="system-status-row">
            <span className="system-status-term">API</span>
            <span
              className={
                apiDown ? "system-status-value system-status-bad" : "system-status-value"
              }
            >
              {apiDown ? "Unreachable — retrying" : "Reachable"}
            </span>
          </div>
          {bootstrap?.unrestricted_agent && (
            <p className="system-status-warning">
              DEV_UNRESTRICTED_AGENT is on: every tool runs, nothing parks for
              approval.
            </p>
          )}
        </div>
      )}
    </DisclosureMenu>
  );
}
