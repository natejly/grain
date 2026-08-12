"use client";

import type { Bootstrap, Citation, Conversation, GeneratedApp, Source } from "@workspace/api-client";
import { X } from "lucide-react";
import type { MouseEvent } from "react";
import { useConversationThread } from "./use-conversation-thread";
import { ChatView } from "./views/chat";

export type ChatPaneProps = {
  /** The conversation this pane shows; its title heads the pane and its
   *  approval mode governs the composer. Read from the shell's list so the
   *  mode has one home. */
  conversation: Conversation;
  bootstrap: Bootstrap | null;
  sources: Source[];
  apps: GeneratedApp[];
  openCitation: (citation: Citation) => Promise<void>;
  /** Close this pane. */
  onClose: () => void;
  /** Whether this pane holds the split's focus outline. */
  focused: boolean;
  /** Take the split's focus (a click anywhere in the pane). */
  onFocus: () => void;
  /** Re-read the shell's conversations list after this pane's run settled. */
  onSettled: () => Promise<void> | void;
  /** Hand back an approval-mode change so the shell's list stays authoritative. */
  onApprovalChanged: (updated: Conversation) => void;
};

/**
 * One extra chat pane beside the shell's primary chat.
 *
 * A self-contained satellite, exactly as the panel beside a document is: its own
 * `useConversationThread` instance holds every bit of turn and composer state,
 * and `ChatView` is reused unchanged — all pane-specific controls flow through
 * its optional `turnControls` / `skills` / `approval` / `onSelectAgent` bundles.
 * There is no paperclip: adding a source navigates to the Knowledge view, which
 * would replace the whole split — a button that throws away what you were doing
 * is worse than no button, so ChatView omits it when no handler is given.
 */
export function ChatPane({
  conversation,
  bootstrap,
  sources,
  apps,
  openCitation,
  onClose,
  focused,
  onFocus,
  onSettled,
  onApprovalChanged,
}: ChatPaneProps) {
  const thread = useConversationThread({
    conversationId: conversation.id,
    defaultAgentId: bootstrap?.default_agent_id,
    defaultEffort: bootstrap?.model_provider.default_effort,
    onSettled,
    onApprovalChanged,
  });

  // Closing unmounts this pane, so move DOM focus to a surviving target FIRST —
  // the next pane's close button, else the primary pane container — otherwise it
  // falls to <body> and a keyboard user loses their place in the split.
  function handleClose(event: MouseEvent<HTMLButtonElement>) {
    const column = event.currentTarget.closest(".chat-split-pane");
    const split = column?.closest(".chat-split");
    if (split && column) {
      const columns = Array.from(split.querySelectorAll<HTMLElement>(".chat-split-pane"));
      const next = columns[columns.indexOf(column as HTMLElement) + 1];
      const target = next?.querySelector<HTMLElement>(".chat-pane-head button") ?? columns[0];
      target?.focus();
    }
    onClose();
  }

  return (
    <section
      className={focused ? "chat-pane focused" : "chat-pane"}
      onFocusCapture={onFocus}
      onPointerDown={onFocus}
      aria-label={`Chat: ${conversation.title}`}
    >
      <div className="chat-pane-head">
        <span className="chat-pane-title">{conversation.title || "New conversation"}</span>
        <button
          className="icon-button"
          title="Close pane"
          aria-label={`Close ${conversation.title || "conversation"} pane`}
          onClick={handleClose}
        >
          <X size={15} />
        </button>
      </div>
      {thread.error && (
        <div className="tool-error" role="alert">
          {thread.error}
        </div>
      )}
      <ChatView
        messages={thread.messages}
        sources={sources}
        agentCalls={thread.agentCalls}
        apps={apps}
        draft={thread.draft}
        setDraft={thread.setDraft}
        activeRun={thread.activeRun}
        runStatus={thread.runStatus}
        budgetPark={thread.budgetPark}
        sharedThread={conversation.shared}
        submitPrompt={thread.submitPrompt}
        cancelActiveRun={thread.cancelActiveRun}
        regenerate={thread.regenerate}
        decideAgentCall={thread.decideAgentCall}
        openCitation={openCitation}
        endRef={thread.endRef}
        selectedAgentId={thread.selectedAgentId}
        onSelectAgent={thread.setSelectedAgentId}
        approval={{
          mode: conversation.approval_mode,
          setMode: thread.setApprovalMode,
          conversationId: conversation.id,
          conversationTitle: conversation.title,
        }}
        turnControls={{
          models: bootstrap?.model_provider.selectable_models ?? [],
          efforts: bootstrap?.model_provider.reasoning_efforts ?? [],
          model: thread.selectedModel,
          setModel: thread.setSelectedModel,
          effort: thread.selectedEffort,
          setEffort: thread.setSelectedEffort,
          fast: thread.fast,
          setFast: thread.setFast,
        }}
        skills={{
          attached: thread.attachedSkill,
          argValues: thread.skillArgs,
          attach: thread.attachSkill,
          detach: thread.detachSkill,
          setArg: thread.setSkillArg,
        }}
      />
    </section>
  );
}
