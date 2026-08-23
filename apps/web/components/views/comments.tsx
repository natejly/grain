"use client";

import type {
  Comment,
  CommentCreateInput,
  WorkspaceMember,
} from "@workspace/api-client";
import { MessageSquareText, Send, Trash2, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import {
  completeMention,
  matchMembers,
  mentionQuery,
  parseMentions,
  splitMentions,
} from "./comment-format";
import { formatRelative } from "./shared";

/**
 * The comments drawer: one panel for threads, documents and dashboards alike,
 * because a comment's subject is the polymorphic pair the server already uses
 * and three bespoke panels would be three @-pickers to keep honest.
 *
 * Mentions are typed as `@Name` — the completion list opens on "@", copying
 * the slash-picker mechanism from the chat composer — but the request carries
 * member *ids*, derived from the finished text by the same parser that renders
 * chips (`comment-format.ts`). The server keeps only real members, and only
 * viewers of a private thread; what it kept comes back on the row, so a chip
 * is always a person who was actually notified.
 */

export type CommentSubject = {
  kind: "conversation" | "document" | "dashboard";
  id: string;
  /** What the drawer header calls the thing, e.g. the document title. */
  label: string;
};

/** The fetching half, built by `handlers/comments.ts` — this component never
 * touches the network itself. */
export type CommentOps = {
  load: (
    kind: CommentSubject["kind"],
    subjectId: string,
  ) => Promise<Comment[] | null>;
  add: (input: CommentCreateInput) => Promise<Comment | null>;
  remove: (commentId: string) => Promise<boolean>;
  loadMembers: () => Promise<WorkspaceMember[]>;
};

const SUBJECT_LABELS: Record<CommentSubject["kind"], string> = {
  conversation: "thread",
  document: "document",
  dashboard: "dashboard",
};

export function CommentsPanel({
  subject,
  close,
  ops,
  currentUserId,
  isOwner,
}: {
  subject: CommentSubject;
  close: () => void;
  ops: CommentOps;
  currentUserId: string;
  /** Workspace owners may remove what they did not write. */
  isOwner: boolean;
}) {
  const [rows, setRows] = useState<Comment[] | null>(null);
  const [members, setMembers] = useState<WorkspaceMember[]>([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    let stale = false;
    setRows(null);
    void ops.load(subject.kind, subject.id).then((loaded) => {
      if (!stale && loaded) setRows(loaded);
    });
    void ops.loadMembers().then((loaded) => {
      if (!stale) setMembers(loaded);
    });
    return () => {
      stale = true;
    };
    // ops is a stable factory product; the subject is what changes.
  }, [subject.kind, subject.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const query = mentionQuery(draft);
  const completions = query === null ? [] : matchMembers(members, query);

  function nameOf(userId: string): string {
    return members.find((member) => member.user_id === userId)?.name ?? "Someone";
  }

  async function send() {
    const body = draft.trim();
    if (!body || busy) return;
    setBusy(true);
    try {
      const created = await ops.add({
        subject_kind: subject.kind,
        subject_id: subject.id,
        body,
        mentions: parseMentions(body, members),
      });
      if (created) {
        setRows((current) => [...(current ?? []), created]);
        setDraft("");
      }
    } finally {
      setBusy(false);
    }
  }

  async function remove(commentId: string) {
    const removed = await ops.remove(commentId);
    if (removed) {
      setRows((current) =>
        (current ?? []).filter((row) => row.id !== commentId),
      );
    }
  }

  return (
    <div className="drawer-scrim" onClick={close}>
      <aside
        className="comments-drawer"
        aria-label={`Comments on ${subject.label}`}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="drawer-header">
          <div>
            <span>Comments on this {SUBJECT_LABELS[subject.kind]}</span>
            <strong>{subject.label}</strong>
          </div>
          <button className="icon-button" onClick={close} aria-label="Close comments">
            <X size={16} />
          </button>
        </div>

        <div className="comments-scroll">
          {rows === null ? (
            <p className="timeline-empty">Loading…</p>
          ) : rows.length === 0 ? (
            <div className="approval-empty">
              <div>
                <MessageSquareText size={18} />
              </div>
              <strong>No comments yet</strong>
              <p>Say something — @-mention a teammate to put it in their Inbox.</p>
            </div>
          ) : (
            rows.map((row) => (
              <article key={row.id} className="comment-row">
                <div className="comment-row-head">
                  <strong>{nameOf(row.created_by)}</strong>
                  <span>{formatRelative(row.created_at)}</span>
                  {(row.created_by === currentUserId || isOwner) && (
                    <button
                      className="icon-button"
                      aria-label="Delete comment"
                      title="Delete comment"
                      onClick={() => void remove(row.id)}
                    >
                      <Trash2 size={13} />
                    </button>
                  )}
                </div>
                <p className="comment-body">
                  {splitMentions(row.body, row.mentions, members).map(
                    (segment, index) =>
                      segment.kind === "mention" ? (
                        <mark key={index} className="comment-mention">
                          {segment.text}
                        </mark>
                      ) : (
                        <span key={index}>{segment.text}</span>
                      ),
                  )}
                </p>
              </article>
            ))
          )}
        </div>

        <form
          className="comment-composer"
          onSubmit={(event) => {
            event.preventDefault();
            void send();
          }}
        >
          {completions.length > 0 && (
            <ul className="comment-mention-picker" role="listbox" aria-label="Mention a member">
              {completions.map((member, index) => (
                <li key={member.user_id}>
                  <button
                    type="button"
                    className={index === 0 ? "mention-option first" : "mention-option"}
                    onClick={() => {
                      setDraft((current) => completeMention(current, member.name));
                      inputRef.current?.focus();
                    }}
                  >
                    @{member.name}
                  </button>
                </li>
              ))}
            </ul>
          )}
          <textarea
            ref={inputRef}
            value={draft}
            rows={2}
            aria-label={`Comment on ${subject.label}`}
            placeholder="Comment — @ mentions a member"
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                // Enter takes the top completion while the picker is open —
                // the slash-picker convention — and sends otherwise.
                if (completions.length > 0) {
                  setDraft((current) =>
                    completeMention(current, completions[0].name),
                  );
                } else {
                  void send();
                }
              }
            }}
          />
          <button
            type="submit"
            className="primary-button"
            disabled={busy || !draft.trim()}
            aria-label="Send comment"
          >
            <Send size={14} />
          </button>
        </form>
      </aside>
    </div>
  );
}
