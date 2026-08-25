"use client";

import type { Bootstrap, Citation, Conversation, GeneratedApp, Source } from "@workspace/api-client";
import { Maximize2, Minimize2, X } from "lucide-react";
import { useEffect, useState, type MouseEvent } from "react";
import { useSession } from "./auth/session-provider";
import { useConversationThread } from "./use-conversation-thread";
import { ChatView } from "./views/chat";
import type { DashboardPinning } from "./views/dashboard-pin-bar";

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
  /** The shell's pin bundle: a dashboard made in THIS pane gets the same
   *  finish-the-job bar the primary chat shows. Optional, like ChatView's own
   *  prop — the subject panels stay without it. */
  pinning?: DashboardPinning;
  /** Whether this pane currently holds the whole split's surface. Threaded
   *  from ChatSplit, which owns the maximize state — the pane only draws the
   *  control. Optional as a pair with `onToggleMaximize`. */
  maximized?: boolean;
  /** Maximize this pane, or restore the split when it already is. */
  onToggleMaximize?: () => void;
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
  pinning,
  maximized,
  onToggleMaximize,
}: ChatPaneProps) {
  const thread = useConversationThread({
    conversationId: conversation.id,
    defaultAgentId: bootstrap?.default_agent_id,
    defaultEffort: bootstrap?.model_provider.default_effort,
    // What this thread remembers, so the pane's pickers open where the rail's
    // composer would — and a pick here writes back through the same row.
    threadDefaults: {
      agentId: conversation.default_agent_id,
      model: conversation.default_model,
      effort: conversation.default_effort,
    },
    onSettled,
    onApprovalChanged,
  });
  // The signed-in member, so this pane's shared thread offers the edit pencil
  // only on their own prompts — same rule as the primary chat.
  const { session } = useSession();

  // Whether the head is currently asking "close a pane whose turn is still
  // running?". Never a window.confirm and never a scrim: the pane itself is
  // the context the question is about, so it must stay visible under it.
  const [confirmingClose, setConfirmingClose] = useState(false);

  // A run that settles while the question is up answers it: closing an idle
  // pane needs no confirmation, so a stale prompt must not keep demanding one.
  useEffect(() => {
    if (!thread.activeRun) setConfirmingClose(false);
  }, [thread.activeRun]);

  // Closing unmounts this pane, so move DOM focus to a surviving target FIRST —
  // the next pane's close button, else the primary pane container — otherwise it
  // falls to <body> and a keyboard user loses their place in the split.
  function closeMovingFocus(from: HTMLElement) {
    const column = from.closest(".chat-split-pane");
    const split = column?.closest(".chat-split");
    if (split && column) {
      const columns = Array.from(split.querySelectorAll<HTMLElement>(".chat-split-pane"));
      const next = columns[columns.indexOf(column as HTMLElement) + 1];
      const target = next?.querySelector<HTMLElement>(".chat-pane-head button") ?? columns[0];
      target?.focus();
    }
    onClose();
  }

  function handleClose(event: MouseEvent<HTMLButtonElement>) {
    // A live run is about to be orphaned by this close — it keeps running on
    // the server with nothing on screen showing it. Ask once, inline.
    if (thread.activeRun) {
      setConfirmingClose(true);
      return;
    }
    closeMovingFocus(event.currentTarget);
  }

  return (
    <section
      className={focused ? "chat-pane focused" : "chat-pane"}
      onFocusCapture={onFocus}
      onPointerDown={onFocus}
      aria-label={`Chat: ${conversation.title}`}
    >
      {confirmingClose ? (
        <div className="chat-pane-head chat-pane-confirm" role="group" aria-label="Confirm close">
          <span className="chat-pane-title">A turn is running —</span>
          <button
            className="ghost-button"
            onClick={(event) => closeMovingFocus(event.currentTarget)}
          >
            Close anyway
          </button>
          <button className="ghost-button" autoFocus onClick={() => setConfirmingClose(false)}>
            Keep
          </button>
        </div>
      ) : (
        <div className="chat-pane-head">
          <span className="chat-pane-title">{conversation.title || "New conversation"}</span>
          {onToggleMaximize && (
            <button
              className="icon-button"
              title={maximized ? "Restore split" : "Maximize pane"}
              aria-label={
                maximized
                  ? "Restore split"
                  : `Maximize ${conversation.title || "conversation"} pane`
              }
              onClick={onToggleMaximize}
            >
              {maximized ? <Minimize2 size={14} /> : <Maximize2 size={14} />}
            </button>
          )}
          <button
            className="icon-button"
            title="Close pane"
            aria-label={`Close ${conversation.title || "conversation"} pane`}
            onClick={handleClose}
          >
            <X size={15} />
          </button>
        </div>
      )}
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
        steer={thread.steerActiveRun}
        regenerate={thread.regenerate}
        editMessage={thread.editMessage}
        viewerId={session?.user_id}
        decideAgentCall={thread.decideAgentCall}
        openCitation={openCitation}
        // The pin/open halves are pane-agnostic, but the composer seed is not:
        // the shell's askForChart feeds the PRIMARY composer, and a request
        // about this pane's chart typed into another thread asks the wrong
        // agent about a chart it never drew. Re-aim it at this pane's draft,
        // appending like the shell does so a half-typed message survives.
        pinning={
          pinning && {
            ...pinning,
            askForChart: (seed) =>
              thread.setDraft(
                thread.draft.trim() ? `${thread.draft}\n${seed}` : seed,
              ),
          }
        }
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
