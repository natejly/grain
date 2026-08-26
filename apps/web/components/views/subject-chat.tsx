"use client";

import type { Citation, GeneratedApp, Source } from "@workspace/api-client";
import { X } from "lucide-react";
import type { useSubjectThread } from "../use-subject-thread";
import { ChatView } from "./chat";

export type SubjectChatPanelProps = {
  /** `document-chat` | `project-chat` | `dashboard-chat`. */
  className: string;
  /** The heading, e.g. "Chat about this project". */
  heading: string;
  /** The landmark's accessible name, which names the subject. */
  label: string;
  close: () => void;
  thread: ReturnType<typeof useSubjectThread>;
  sources: Source[];
  apps: GeneratedApp[];
  openCitation: (citation: Citation) => Promise<void>;
  /**
   * Calls the surrounding column already offers a decision for. Offering
   * Approve twice for one call, in two places on the same screen, means one of
   * them is stale the moment the other is pressed.
   */
  hidden?: Set<string>;
  /** `DEV_UNRESTRICTED_AGENT` is on; the panel must say so. */
  unrestricted?: boolean;
};

/**
 * The chat panel that sits beside a document, a project or a dashboard.
 *
 * One component, three surfaces. The chat *body* was already shared — `ChatView`
 * is what the rail renders — and this is the wrapper around it that was
 * duplicated in the document editor: a landmark, a heading, a close button, the
 * error banner and the prop wiring. Copying those into two more views is how the
 * panels would end up disagreeing about which props ChatView needs, which is the
 * same drift `createThreadHandlers` was extracted to prevent one layer down.
 */
export function SubjectChatPanel({
  className,
  heading,
  label,
  close,
  thread,
  sources,
  apps,
  openCitation,
  hidden,
  unrestricted,
}: SubjectChatPanelProps) {
  return (
    // Two classes: `subject-chat` carries every rule (the panel is the same
    // panel on all three surfaces), and the per-surface one is what the layout
    // grid and the e2e locators name.
    <aside className={`subject-chat ${className}`} aria-label={label}>
      <div className="subject-chat-head">
        <strong>{heading}</strong>
        <button className="icon-button" onClick={close} aria-label={`Close ${label}`}>
          <X size={16} />
        </button>
      </div>
      {thread.error && (
        <div className="tool-error" role="alert">
          {thread.error}
        </div>
      )}
      {/* The same view the rail renders, at compact density. Reused rather than
          rebuilt: streaming, tool cards, approvals, the budget hold and the
          citation verdict are one implementation, so a panel cannot fall behind
          Chat by a bug fix. */}
      <ChatView
        messages={thread.messages}
        sources={sources}
        agentCalls={
          hidden ? thread.agentCalls.filter((call) => !hidden.has(call.id)) : thread.agentCalls
        }
        apps={apps}
        draft={thread.draft}
        setDraft={thread.setDraft}
        activeRun={thread.activeRun}
        runStatus={thread.runStatus}
        budgetPark={thread.budgetPark}
        submitPrompt={thread.submitPrompt}
        cancelActiveRun={thread.cancelActiveRun}
        regenerate={thread.regenerate}
        decideAgentCall={thread.decideAgentCall}
        openCitation={openCitation}
        unrestricted={
          unrestricted ? { conversationId: thread.conversationId } : undefined
        }
        // The approval-mode control the subject panels used to hide — the one
        // control the Foyer trust work says every composer must carry. The
        // label names the subject, so the mode chip's menu reads as governing
        // this panel's thread and no other. No starter cards: an empty panel
        // beside a document is scoped to that document, and the product-verbs
        // lesson belongs to the rail chat.
        approval={{
          mode: thread.approvalMode,
          setMode: thread.setApprovalMode,
          conversationId: thread.conversationId,
          conversationTitle: label,
        }}
        showStarter={false}
        // No paperclip. Attaching a source navigates to the Knowledge view,
        // which would close the thing this panel is about — a button that throws
        // away what you were doing is worse than no button, so ChatView omits it
        // when no handler is given.
        endRef={thread.endRef}
      />
    </aside>
  );
}
