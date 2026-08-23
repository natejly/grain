"use client";

import type { Dispatch, SetStateAction } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

export type ApprovalHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  /** Re-read the attention feed — an assignment moves rows between the
   * queue's groups and changes the rail badge. */
  refreshFeed: () => Promise<void>;
};

/**
 * Routing a parked approval to a member. Deciding stays on `decideAgentCall`
 * (handlers/thread.ts); this is only the "who should look at it" half. Per the
 * house pattern the network touch lives here — the Inbox card just calls what
 * it is handed.
 */
export function createApprovalHandlers({ setError, refreshFeed }: ApprovalHandlerDeps) {
  /** Route one approval to `userId`, or back to anyone with "". */
  async function assignApproval(callId: string, userId: string): Promise<boolean> {
    setError("");
    try {
      await api.assignAgentToolCall(callId, userId);
      await refreshFeed();
      return true;
    } catch (caught) {
      setError(describeError(caught, "Could not assign the approval"));
      return false;
    }
  }

  return { assignApproval };
}
