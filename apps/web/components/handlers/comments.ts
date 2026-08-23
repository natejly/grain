"use client";

import type {
  Comment,
  CommentCreateInput,
  WorkspaceMember,
} from "@workspace/api-client";
import type { Dispatch, SetStateAction } from "react";
import { api } from "../api";
import { describeError } from "../views/shared";

export type CommentHandlerDeps = {
  setError: Dispatch<SetStateAction<string>>;
  /** Re-read the attention feed — a resolved mention must leave the badge. */
  refreshFeed: () => Promise<void>;
};

/**
 * Comments and the mentions they produce. The list/add/remove trio feeds the
 * comments drawer (any subject kind); `resolveMention` is the Inbox's resolve
 * button. Per the house pattern, every network touch lives here — the drawer
 * and the Inbox card only call what they are handed.
 */
export function createCommentHandlers({ setError, refreshFeed }: CommentHandlerDeps) {
  async function loadComments(
    kind: Comment["subject_kind"],
    subjectId: string,
  ): Promise<Comment[] | null> {
    try {
      return await api.listComments(kind, subjectId);
    } catch (caught) {
      setError(describeError(caught, "Could not load comments"));
      return null;
    }
  }

  async function addComment(input: CommentCreateInput): Promise<Comment | null> {
    setError("");
    try {
      return await api.createComment(input);
    } catch (caught) {
      setError(describeError(caught, "Could not add the comment"));
      return null;
    }
  }

  async function removeComment(commentId: string): Promise<boolean> {
    setError("");
    try {
      await api.deleteComment(commentId);
      return true;
    } catch (caught) {
      setError(describeError(caught, "Could not delete the comment"));
      return false;
    }
  }

  async function loadMembers(): Promise<WorkspaceMember[]> {
    try {
      return await api.listMembers();
    } catch (caught) {
      setError(describeError(caught, "Could not load the member list"));
      return [];
    }
  }

  /** Flip one mention out of the waiting set, then re-read the feed so the
   * badge and the list agree it is gone. */
  async function resolveMention(notificationId: string): Promise<void> {
    setError("");
    try {
      await api.resolveNotification(notificationId);
      await refreshFeed();
    } catch (caught) {
      setError(describeError(caught, "Could not resolve the mention"));
    }
  }

  /** The same notification store, worn by a monitor alert: '' -targeted, so
   * resolving clears it for every member — hence the honest separate copy. */
  async function resolveAlert(notificationId: string): Promise<void> {
    setError("");
    try {
      await api.resolveNotification(notificationId);
      await refreshFeed();
    } catch (caught) {
      setError(describeError(caught, "Could not resolve the alert"));
    }
  }

  return {
    loadComments,
    addComment,
    removeComment,
    loadMembers,
    resolveMention,
    resolveAlert,
  };
}
