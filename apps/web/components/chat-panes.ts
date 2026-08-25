/**
 * Pane-manager: the pure layout rules behind the workspace's multi-pane chat
 * split. Kept apart from `useWorkspace` so every transition — open, close,
 * refocus, prune, and the round trip through localStorage — is exercised
 * without a DOM. The hook owns the state and the effects; this owns the rules,
 * and each function is total: given any input it returns a valid layout rather
 * than throwing, because the persisted value is one hand-editable key away from
 * anything at all.
 */

/**
 * An extra chat pane beside the primary shell chat. `id` is a client-generated
 * uuid, not the conversation id, so the same conversation can be opened in two
 * panes without a React key clash.
 */
export type ChatPane = { id: string; conversationId: string };

/** How many extra panes may open beside the primary — four chats at most. */
export const MAX_EXTRA_PANES = 3;

/** localStorage key for the open-pane layout, following the `grain.*` convention. */
export const CHAT_PANES_KEY = "grain.chat-panes";

/**
 * Decode a persisted layout. A missing, malformed, or hostile value is an empty
 * split, never a throw: a broken layout must not take the whole shell down with
 * it, so rows that are not `{ id, conversationId }` strings are dropped.
 */
export function parseStoredPanes(raw: string | null): ChatPane[] {
  if (!raw) return [];
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const rows = parsed.filter(
      (item): item is ChatPane =>
        Boolean(item) &&
        typeof (item as ChatPane).id === "string" &&
        typeof (item as ChatPane).conversationId === "string",
    );
    // A hand-edited store can carry a duplicate id or more panes than the cap,
    // either of which the running app never produces: a repeated id clashes as a
    // React key, and an over-cap list defeats the open guard. Keep the first
    // occurrence of each id, then hold to the cap.
    const seen = new Set<string>();
    const unique: ChatPane[] = [];
    for (const row of rows) {
      if (seen.has(row.id)) continue;
      seen.add(row.id);
      unique.push(row);
    }
    return unique.slice(0, MAX_EXTRA_PANES);
  } catch {
    return [];
  }
}

/**
 * A collision-proof pane id, mirroring api-client's `makeKey` guard rather than
 * calling `crypto.randomUUID()` unguarded — the latter is absent in non-secure
 * contexts and older runtimes, where an unguarded call throws and would take the
 * open with it.
 */
export function newPaneId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** Encode a layout for persistence — the inverse of `parseStoredPanes`. */
export function serializeStoredPanes(panes: ChatPane[]): string {
  return JSON.stringify(panes);
}

/**
 * Append a conversation as a new extra pane, returning the SAME list unchanged
 * when the open is a no-op: the conversation is already the primary pane (a
 * second copy of what pane 0 shows is not what "split" is for) or the cap is
 * already reached. `id` is injected so the decision stays pure and testable
 * rather than reaching for `crypto.randomUUID()`.
 */
export function addPane(
  panes: ChatPane[],
  conversationId: string,
  activeConversationId: string | null,
  id: string,
): ChatPane[] {
  if (conversationId === activeConversationId) return panes;
  if (panes.length >= MAX_EXTRA_PANES) return panes;
  return [...panes, { id, conversationId }];
}

/** Drop the pane with `paneId`; the rest keep their order. */
export function removePane(panes: ChatPane[], paneId: string): ChatPane[] {
  return panes.filter((pane) => pane.id !== paneId);
}

/**
 * Where focus lands after a pane closes: back on the primary (`null`) when the
 * closed pane held it, otherwise wherever it already was. A closed pane must
 * never remain the focused id — nothing on screen would answer to it.
 */
export function refocusAfterClose(
  focused: string | null,
  closedPaneId: string,
): string | null {
  return focused === closedPaneId ? null : focused;
}

/**
 * Drop panes whose conversation no longer exists, preserving the input's
 * identity when nothing changed so a React state setter can skip a pointless
 * re-render.
 */
export function prunePanes(
  panes: ChatPane[],
  knownConversationIds: Set<string>,
): ChatPane[] {
  const kept = panes.filter((pane) => knownConversationIds.has(pane.conversationId));
  return kept.length === panes.length ? panes : kept;
}
