"use client";

import type { ChatAttachment } from "@workspace/api-client";
import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

export type AttachmentHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  setAttaching: Dispatch<SetStateAction<boolean>>;
  setAttachments: Dispatch<SetStateAction<ChatAttachment[]>>;
  /**
   * A monotonic token that orders attachment writes against the per-thread
   * refetch. `attachFile`'s optimistic append bumps it; `refreshAttachments`
   * captures it and drops a response that a later write has superseded — so a
   * listAttachments that read the server before the upload committed cannot
   * clobber the just-added chip back to empty.
   */
  attachmentEpoch: MutableRefObject<number>;
  /**
   * The conversation whose chips are on screen right now: the rail's active
   * thread, or a pane's own fixed thread. `attachFile` reads it after its
   * upload resolves so it only appends (and supersedes the refetch) when the
   * file's thread is still the one being shown — otherwise a mid-upload thread
   * switch would strand the chip on the wrong thread and block that thread's
   * own refetch.
   */
  currentConversationId: () => string | null;
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
  attachmentEpoch,
  currentConversationId,
  ensureConversation,
}: AttachmentHandlerDeps) {
  async function refreshAttachments(conversationId: string | null): Promise<void> {
    const epoch = (attachmentEpoch.current += 1);
    if (!conversationId) {
      setAttachments([]);
      return;
    }
    try {
      const rows = await api.listAttachments(conversationId);
      // A newer write (an optimistic append, or a later thread's refetch) has
      // moved on; this response is stale, so drop it rather than overwrite.
      if (epoch !== attachmentEpoch.current) return;
      setAttachments(rows);
    } catch {
      // A thread whose attachments cannot be listed still has to be usable, so
      // this is not an error toast: the chips are absent, not wrong. Anything
      // the user then does to a file reports for itself.
      if (epoch === attachmentEpoch.current) setAttachments([]);
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
      // Only show (and let this append win over a refetch) when the file's
      // thread is still on screen. If the user switched threads while the upload
      // was in flight, appending here would strand the chip on the wrong thread
      // and — via the epoch bump — block that thread's own legitimate refetch;
      // the file is safely attached server-side and appears when its thread is
      // reopened. When it IS still current (the common case, including attaching
      // to an empty composer), the bump supersedes the stale refetch that the
      // thread's creation kicked off, so the chip is not blanked back to empty.
      if (currentConversationId() === conversationId) {
        attachmentEpoch.current += 1;
        setAttachments((current) => [...current, attachment]);
      }
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
