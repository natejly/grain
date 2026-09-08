# Spaces as Projects (ChatGPT-style containers) — DONE, see Review

All items below landed on `worktree-spaces-projects`; every box is checked in
the Review section at the bottom.


Goal: a space behaves like a ChatGPT Project — a container of chats with its
own files and instructions. Chats can be moved in and out; space files stay
scoped to the space; the Spaces UI becomes a container you work in, and the
rail groups chats by space.

No schema change: `Conversation.space_id` and `Source.space_id` already exist.
The gaps are (a) space_id is write-once at creation, (b) thread deletion leaks
attachment rows/bytes, (c) space files leak into the workspace graph, (d) the
UI is an admin page, not a container.

## Backend

- [x] `PUT /api/conversations/{id}/space` — move a thread into a space or out
      (`space_id: ""`). Visible-member gate (same class as rename/approval
      mode); subject threads 409; foreign/deleted space 404; audit
      `conversation.space_set` from/to. No Idempotency-Key (PUT of a value).
- [x] `conversations.purge` teardown: delete `ChatAttachment` rows, purge
      conversation-scoped `Source`s, return object_keys for the post-commit
      disk sweep (new receipt dataclass; update the 3 call sites; sweep in
      `DELETE /api/conversations/{id}` like the spaces route does).
      Attached *documents* survive (same philosophy as `detach`).
- [x] `rebuild_graph`: add `Source.space_id == ""` beside the existing
      `conversation_id == ""` filter — the workspace graph is built from the
      library alone; space files stay in their space (todo.md known leak).
- [x] Register the new route in `tests/isolation.py` ROUTE_CASES with a seeded
      victim row.
- [x] Regenerate the OpenAPI contract if the repo has a make target for it.

## Backend tests

- [x] Move: in/out reflected in `?space_id=` listing; foreign + deleted space
      404; subject thread 409; other member's personal thread 404; a moved
      thread's next turn retrieves space knowledge, a moved-out one doesn't;
      memory shelf follows the conversation's current space.
- [x] Purge: deleting a conversation removes its ChatAttachment rows, purges
      its scoped sources, unlinks bytes; space delete does the same for its
      threads' attachments.
- [x] Graph: a space-scoped source is not projected into the workspace graph.

## Frontend

- [x] api-client: `setConversationSpace(conversationId, spaceId)`.
- [x] Rail: group threads ChatGPT-style — a collapsible group per space, then
      Personal/Shared for unspaced threads; "Move to space" in the thread menu.
- [x] SpacesView redesign as a container: cleaner space list; detail = name
      (inline edit) + overflow menu (template/delete), collapsible
      instructions, Chats panel (open, new chat, add existing chat, remove
      from space), Files panel (upload + list + delete). Keep existing e2e
      accessible names where possible.
- [x] CSS: new container layout + rail groups, tokens only (theme-tokens test).

## Frontend tests

- [x] Update `spaces-view.test.ts`, `rail-threads.test.ts`,
      `spaces-layout-css.test.ts`, `thread-rail-css.test.ts`; add coverage for
      move-to-space and the add-chat picker.
- [x] e2e `spaces.spec.ts`: keep the lifecycle test green; extend with a
      move-in/move-out assertion.

## Gates

- [x] `make lint`; pytest with `PYTHONPATH=<worktree>/apps/api` (verify
      `find_spec('app').origin` first); `pnpm test`; `pnpm build`; e2e spaces
      spec. Capture exit codes to files, never through a pipe.

## Review

Shipped 2026-09-08 on `worktree-spaces-projects` (based on local main
@2490aad, which includes chat attachments).

Backend — all done:
- `PUT /api/conversations/{id}/space` (visible-member gate, subject 409,
  foreign/deleted 404, `conversation.space_set` audit). Registered in the
  isolation ledger. OpenAPI contract regenerated.
- `conversations.purge` now returns a `PurgeReceipt` and takes the thread's
  `ChatAttachment` rows, its scoped sources and (via the callers'
  post-commit `ingestion.remove_object_files`) their bytes. All five purge
  callers updated (chat delete, space cascade, document/project/dashboard
  subject deletes); the two previously-inline disk sweeps now share the
  helper.
- `rebuild_graph` filters `Source.space_id == ""` — the known "space files
  leak into the workspace graph" hole (was logged further down this file) is
  closed.

Frontend — all done:
- Rail groups threads ChatGPT-Projects-style: a collapsible group per space
  (session-local fold state), Personal/Shared for the rest; the two helpers
  (`spaceThreadGroups`/`unspacedThreads`) partition the list so a thread
  whose space vanished falls back to the flat rail. Thread menu gained
  "Move to <space>" / "Remove from space" on every row.
- SpacesView is a container page now: header = name + Save + actions menu
  (template/delete moved off the stray-click path), threads panel with
  "Add a thread" (files an unspaced thread in) and per-row remove, side
  column with instructions + knowledge. `onMoveThread` rides
  `moveConversationToSpace` in use-workspace, which patches the row and
  re-fetches the space counts.

Gates: ruff + mypy clean; full backend pytest green (incl. new
`test_space_move.py`: move semantics, purge-takes-attachments, graph
scope); web vitest 881/881; `pnpm lint` 0 errors (10 pre-existing warnings,
all verified present at HEAD); `pnpm build` green; spaces e2e (extended
with fold + move-out/move-in) green.

Not done, deliberately: `Document.space_id` (space "documents" remain
ingested Sources — a Document is workspace-global with title-addressing the
agent relies on); persisting rail fold state.

---

# Chat file attachments + editing (worktree-screen-order-fix, 2026-08-27)

Two asks:
1. Remove the "Using 1 source passages" status line.
2. Enable attaching and editing files inside chats.

User steers on (2):
- Text files should become **editable Documents**; the attachment must be openable
  and editable, not read-only evidence.
- A file uploaded in a chat **must not just land in the general knowledge base** —
  "if a user uploads something for a chat likely it is going to be relevant to that
  chat". So chat uploads are conversation-scoped.
- Editing happens by **opening the file in a split pane**, not an inline editor.

## Design — dissolve into existing mechanisms, do not build parallel ones

Three mechanisms already ship and do most of this:
- `services/subjects.py` — injects a file's contents into a turn, framed as user
  material (never instructions), screened for prompt injection, and narrows the
  tool registry. Scoped threads use it; the rail chat has no way to say "this turn
  is about this file".
- `retrieval._live_sources(space_id)` — the ONE tuple every search arm shares.
  Already scopes sources by space with `Source.space_id.in_(("", space_id))`.
  Conversation scoping is the same predicate with another column.
- `edit_document` + `PUT /api/documents/{id}` + proposal-diff / hunk approval —
  the entire "edit a file" surface, human and agent side, already built.

So: an attachment is a polymorphic pointer (document | source) from a conversation
to a file, mirroring `Conversation.subject_kind/subject_id`.

## Plan
- [x] Migration `0068_chat_attachments`: `chat_attachments` table +
      `Source.conversation_id` (indexed, default "")
- [x] `Source.conversation_id`: "" = workspace library (today's behaviour);
      non-empty = uploaded in that chat and invisible to every other thread
- [x] Extend `_live_sources` to take `conversation_id` and thread it through every
      arm + `search_evidence` — all arms or it is a bypass with extra steps
- [x] `services/attachments.py`: upload → text becomes a `Document` (editable),
      non-text becomes a conversation-scoped `Source` (indexed evidence)
- [x] API: POST/GET `/api/conversations/{cid}/attachments`, DELETE `/api/attachments/{id}`
- [x] Turn context: inject attached documents into the turn (bounded, screened),
      reusing the subjects.py framing
- [x] api-client: attachment types + methods
- [x] Composer: attach popover gains "Attach to this chat"; chips above composer
- [x] Split pane: clicking a document chip opens `AttachmentPane` beside the chat
- [x] Tests: 14 api + 12 web
- [x] Verify: pytest, pnpm test, typecheck, lint (ruff/mypy/eslint), build, alembic

## Review

### 1. "Using 1 source passages" — removed

A transient run-status line in `handlers/thread.ts`, set on the
`retrieval.completed` SSE event. The branch is gone; a comment records why
retrieval is not surfaced there — the evidence still reaches the reader as `[n]`
citations on the finished answer, which is where it means something. The backend
still emits the event, so `test_golden_path` is untouched.

### 2. Attach + edit

**The scope is the load-bearing half.** `Source.conversation_id` mirrors
`space_id` exactly, so `_live_sources` filters both in one tuple — a scope
enforced by some ranking arms and not others is a bypass with extra steps. All
five arms carry it (`legacy_lexical_ranking`, `bm25_ranking`, `dense_ranking`,
`rank_arms`, `search_evidence` plus the hydrate spread), and so do the three
other places a file could leak: `_search_sources` (the tool entry, the real
would-be bypass), the graph projection, and `GET /api/sources`.

The Sources listing defaults the *opposite* way from the space filter, on
purpose: absent means the library alone. A space's files are still the
workspace's files, so an unfiltered call showing them is right; a file attached
to one chat is not, and the Sources page calls that route with no arguments.

**Routing.** `.txt/.md/.markdown` → `Document` (editable, injected whole);
everything else on the upload allowlist → conversation-scoped `Source` (indexed,
quotable, not editable — there is no text to edit, and re-chunking a file under
live citations would strand them). `.csv`/`.json` sit on the Source side despite
being text: their value is being queried and quoted, and CSV already has a
destination of its own in datasets.

**Editing needed almost no new code.** A document that arrived as an attachment
is an ordinary document, so `edit_document` (with its diff review and hunk-level
approval), versions and undo all already applied, and the rail chat already had
the `artifacts` family. The only new surface is `AttachmentPane`, a small editor
column that writes through the same `PUT /api/documents/{id}` the Documents page
uses.

**Injection.** The attachment context and the subject context are now one local
(`spliced_context`) that is both injected *and* screened — two expressions that
must stay equal are one edit away from not being, and the failure mode there is
splicing an unscreened upload straight into the prompt.

### Three defects the repo's own tripwires caught

- **`detach` cleared `Source.conversation_id` to `""`** — which in this schema is
  *the workspace library*, so removing a file from one chat would have published
  it to every other one: the exact leak the feature exists to prevent. Now runs
  `purge_source`, the same teardown the Sources page uses. Caught by the test
  written for it.
- **`test_every_db_get_call_site_is_reviewed`** — four new `db.get` sites needed
  recorded justifications; each re-checks `workspace_id` on the next line.
- **`test_route_table_matches_the_app`** and **`theme-tokens`** — the three new
  routes needed isolation cases and a seeded victim attachment, and a
  `var(--danger, #b3261e)` fallback was a hardcoded colour.

A fourth, caught by a web test: `AttachmentPane` rendered an empty textarea when
the load *failed*, which invites the user to "fix" the blank and save it over a
healthy document. The editor is now gated on having the content.

### Not done / worth knowing

- **`pnpm test:e2e` was not run.** Everything else in `make verify` was
  (pytest, `pnpm test`, typecheck, ruff, mypy, eslint, build, alembic up+down).
  Playwright needs a live API + web + database and this worktree has no `.venv`
  of its own.
- **Spaces still leak into the graph.** `rebuild_graph` filters the conversation
  axis now but has never filtered `space_id` — pre-existing, and left alone
  because changing it would alter Spaces behaviour that is not this task's.
- `chat-pane.tsx` and `subject-chat.tsx` deliberately get no `attachToChat`: a
  panel beside a document already has a subject, and a second way to say what the
  conversation is about would be one too many.
- File panes are **not persisted** and close on a thread switch — a working
  surface is not a layout, and a revived editor would reopen files the user had
  closed. This is why they are a separate list from `chat-panes`, whose store,
  pruning and saved layouts keep the one shape they already have.
