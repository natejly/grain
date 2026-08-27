"use client";

import type { ChatAttachment } from "@workspace/api-client";
import type { Dispatch, SetStateAction } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

export type AttachmentHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setAttaching: Dispatch<SetStateAction<boolean>>;
  setAttachments: Dispatch<SetStateAction<ChatAttachment[]>>;
  /**
   * The thread, made if it does not exist yet. Attaching to an empty composer
   * is an ordinary thing to do — you drop a file and then write the question —
   * so it conjures the thread for the same reason typing into one does, rather
   * than refusing against a null id.
   */
  ensureConversation: () => Promise<string>;
};

export function createAttachmentHandlers({
  setError,
  setAttaching,
  setAttachments,
  ensureConversation,
}: AttachmentHandlerDeps) {
  async function refreshAttachments(conversationId: string | null): Promise<void> {
    if (!conversationId) {
      setAttachments([]);
      return;
    }
    try {
      setAttachments(await api.listAttachments(conversationId));
    } catch {
      // A thread whose attachments cannot be listed still has to be usable, so
      // this is not an error toast: the chips are absent, not wrong. Anything
      // the user then does to a file reports for itself.
      setAttachments([]);
    }
  }

  /**
   * Attach one file to the current thread. Returns the row so the caller can
   * act on what the file became — text comes back as a document, which is the
   * one the editor can open.
   */
  async function attachFile(files: FileList | File[]): Promise<ChatAttachment | null> {
    const file = Array.from(files)[0];
    if (!file) return null;
    setAttaching(true);
    setError("");
    try {
      const conversationId = await ensureConversation();
      const attachment = await api.attachFile(conversationId, file);
      setAttachments((current) => [...current, attachment]);
      return attachment;
    } catch (caught) {
      setError(describeError(caught, "Could not attach that file"));
      return null;
    } finally {
      setAttaching(false);
    }
  }

  async function detachFile(attachment: ChatAttachment): Promise<void> {
    // A document survives detaching and a source does not, so only one of them
    // is worth stopping to ask about. Saying so plainly beats a generic "are
    // you sure" that means something different depending on the file.
    if (
      attachment.kind === "source" &&
      !window.confirm(
        `Remove “${attachment.filename}” from this chat? It was uploaded here, ` +
          "so it will stop being searchable.",
      )
    ) {
      return;
    }
    try {
      await api.detachFile(attachment.id);
      setAttachments((current) => current.filter((row) => row.id !== attachment.id));
    } catch (caught) {
      setError(describeError(caught, "Could not remove that file"));
    }
  }

  return { attachFile, detachFile, refreshAttachments };
}
