// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ChatAttachment } from "@workspace/api-client";
import { createElement, createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChatView, type ChatViewProps } from "../components/views/chat";

/**
 * The composer half of chat attachments.
 *
 * Two things are worth pinning here, because both are the kind of detail that
 * looks like styling and is actually the feature:
 *
 * 1. A document chip opens; a source chip does not. The difference is what the
 *    file became on the way in, and a control that looked identical on both
 *    would promise an editor that half of them cannot deliver.
 * 2. The strip shows every attachment the thread has, not only the unsent
 *    ones. A file attached three turns ago is still quoted into every turn.
 */

const BASE: ChatViewProps = {
  messages: [],
  sources: [],
  agentCalls: [],
  apps: [],
  draft: "",
  setDraft: () => undefined,
  activeRun: null,
  runStatus: "",
  budgetPark: null,
  submitPrompt: async () => undefined,
  cancelActiveRun: async () => undefined,
  regenerate: async () => undefined,
  decideAgentCall: async () => undefined,
  openCitation: async () => undefined,
  endRef: createRef<HTMLDivElement>(),
};

function attachment(overrides: Partial<ChatAttachment> = {}): ChatAttachment {
  return {
    id: "att-1",
    conversation_id: "conv-1",
    message_id: "",
    kind: "document",
    target_id: "doc-1",
    filename: "notes.md",
    created_at: "2026-08-27T00:00:00Z",
    ...overrides,
  };
}

function attachProp(overrides: Record<string, unknown> = {}) {
  return {
    upload: async () => null,
    uploading: false,
    attachToChat: async () => null,
    attaching: false,
    attachments: [] as ChatAttachment[],
    ...overrides,
  };
}

afterEach(cleanup);

describe("the attachment strip", () => {
  it("shows every attached file, sent or still staged", () => {
    render(
      createElement(ChatView, {
        ...BASE,
        attach: attachProp({
          attachments: [
            attachment({ id: "a", filename: "sent.md", message_id: "msg-1" }),
            attachment({ id: "b", filename: "staged.md" }),
          ],
        }),
      }),
    );
    expect(screen.getByText("sent.md")).toBeTruthy();
    expect(screen.getByText("staged.md")).toBeTruthy();
  });

  it("renders nothing at all when the thread has no files", () => {
    render(createElement(ChatView, { ...BASE, attach: attachProp() }));
    expect(screen.queryByLabelText("Files attached to this chat")).toBeNull();
  });

  it("opens a document chip in a pane, passing the document id and not the attachment id", () => {
    const openFile = vi.fn();
    render(
      createElement(ChatView, {
        ...BASE,
        attach: attachProp({
          attachments: [attachment()],
          openFile,
        }),
      }),
    );
    fireEvent.click(screen.getByTitle("Open notes.md"));
    // The pane loads by document id; handing it the attachment id would open
    // nothing and is the easy mistake to make with two ids in one row.
    expect(openFile).toHaveBeenCalledWith("doc-1", "notes.md");
  });

  it("offers no editor on a source chip, because a PDF has none", () => {
    const openFile = vi.fn();
    render(
      createElement(ChatView, {
        ...BASE,
        attach: attachProp({
          attachments: [
            attachment({ kind: "source", filename: "contract.pdf", target_id: "src-1" }),
          ],
          openFile,
        }),
      }),
    );
    expect(screen.queryByTitle("Open contract.pdf")).toBeNull();
    expect(screen.getByText("contract.pdf")).toBeTruthy();
  });

  it("removes a file through detach, handing over the whole row", () => {
    const detach = vi.fn(async () => undefined);
    const row = attachment();
    render(
      createElement(ChatView, {
        ...BASE,
        attach: attachProp({ attachments: [row], detach }),
      }),
    );
    fireEvent.click(screen.getByLabelText("Remove notes.md from this chat"));
    // The row, not the id: detach warns differently for a source than for a
    // document, and it cannot tell them apart from an id alone.
    expect(detach).toHaveBeenCalledWith(row);
  });

  it("shows no remove control where detaching is not offered", () => {
    render(
      createElement(ChatView, {
        ...BASE,
        attach: attachProp({ attachments: [attachment()] }),
      }),
    );
    expect(screen.queryByLabelText("Remove notes.md from this chat")).toBeNull();
  });
});

describe("the attach popover", () => {
  it("keeps today's single destination when the thread cannot take files", () => {
    // The subject panels pass `attach` without `attachToChat`: they already
    // have a subject, and a second way to say what the conversation is about
    // would be one too many.
    render(
      createElement(ChatView, {
        ...BASE,
        attach: { upload: async () => null, uploading: false },
      }),
    );
    fireEvent.click(screen.getByLabelText("Attach a file"));
    expect(screen.getByText("Added to workspace knowledge, citable from this thread.")).toBeTruthy();
  });

  it("offers both destinations, and says what each one means", () => {
    render(createElement(ChatView, { ...BASE, attach: attachProp() }));
    fireEvent.click(screen.getByLabelText("Attach a file"));
    expect(
      screen.getByText("Attach it to this chat, or add it to workspace knowledge."),
    ).toBeTruthy();
  });
});
