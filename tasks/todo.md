# Feature sweep (started 2026-08-22, ultracode)

Scope approved by user: everything from the 2026-08-22 brainstorm EXCEPT agent
eval suites (deferred — see "Later in line" below). Build order is by
dependency: internal reuse first, then Inbox-integrated features, then the
egress tier (mail service, share links, webhooks) last since it needs new
infra. Each feature lands with migration guards, route-table/tenant-isolation
cases, unit tests, and its own commit.

- [x] 1. Templates: space templates + duplicate dashboard + workflow templates
- [x] 2. Thread forking: branch a conversation from any message
- [x] 3. Comments & @mentions on documents/dashboards/threads → Inbox
- [x] 4. Assignable approvals: route an approval to a member; Inbox "assigned"
- [x] 5. Metric monitors: dataset/typed-query threshold trips → Inbox alerts
- [x] 6. Run undo: revert a run's write-tool effects from recorded state
- [x] 7. Per-agent cost attribution + spend anomaly flags → Inbox
      (model_usage.agent_id bound in the agent-loop usage scope; admin usage
      by_agent axis; hourly spend watch on the tick claiming via the new
      shared sweep_claims table — reused by F13; anomalies are their own
      Inbox list + badge term, resolved via the shared notification route)
- [ ] 8. Mail service abstraction (console/dev fallback, SMTP config) — infra
      for 9/11/12
- [ ] 9. Share links: revocable read-only public URLs (dashboard/document/
      artifact)
- [ ] 10. Dashboard subscriptions: scheduled snapshot delivery
- [ ] 11. Outbound webhooks + API tokens (trigger workflow / post to thread;
      event push)
- [ ] 12. Inbound email → thread (provider-webhook endpoint, pairs with Rules)
- [ ] 13. Notification digests: daily pending-approvals email per member
- [ ] Full gate: make lint, pytest, pnpm test, pnpm build, e2e

## Later in line (explicitly deferred by user 2026-08-22)

- [ ] Agent eval suites: save real threads as fixtures, replay against an
      edited agent, diff transcripts (productize the scripted/hermetic
      harness + memory-eval gate patterns). Do AFTER the feature sweep above.

# Frontend redesign: "Foyer"

Full spec: https://claude.ai/code/artifact/547d1b83-5525-4537-afe9-aa8b42a272bb
(produced 2026-08-19 by a 17-agent workflow: 6 UX audits of apps/web, 5 market
studies, 3 competing proposals, 2 judges — Foyer won unanimously 9/8/9).

Shape: permanent chrome shrinks to a 4-item icon rail (Chat, Inbox, Library,
Automations) + settings gear; everything else summoned via Cmd+K, one unified
right split pane, and composer chips (+ Attach, @ Agent, / Skills). One badge
in the whole app (Inbox). Every existing view has a mapped home; ~40 views are
kept or reshuffled, few net-new builds (Inbox feed, palette, Datasets view,
attach popover, Rules/Policies, new shell).

## Phases (each independently shippable) — approved 2026-08-19

- [x] Phase 1 — Kill the worst lies (chrome only, no backend): dissolved
      Settings menu ("Workspace settings" gear: Connections + Admin only),
      Inbox on the rail with the only badge (approvals), Files→Library with
      Documents tab (no more Files/Files), Workflows group→Automations with
      crons tab→Schedules, Apps split from Dashboards (new view + Create says
      App not Dashboard), in-place attach popover (upload handler no longer
      teleports to Sources), composer chips (+Attach / @agent / /Skills),
      empty-chat starter cards + approval-loop line, workspace-switcher
      failure state with retry, delete confirms (agents, boards), Cmd+S in
      the document editor
- [x] Phase 2 — The real Inbox (core; snooze/Later + Rules deferred to Phase
      4's trust work): GET /api/inbox — unbounded waiting set (approvals from
      all origins via run_activity_predicate + member-visible budget holds) +
      windowed workflow outcomes; composite index 0043 on agent_tool_calls;
      InboxView (tabs, inline ProposalDiff decide, J/K + A/D keys, deep links,
      History=audit) replacing ActivityView; feed-backed rail badge;
      waiting-on-you sidebar strip (inline decide, cap 2 + "N more");
      sticky "Waiting for your approval — Jump to request" composer banner;
      workflows view's 25×20 client scan retired (feed-sourced strip, runs
      loaded per selection); Admin read-only approvals panel deleted
- [ ] Phase 3 — Library + Data (data half DONE 2026-08-19): ✔ Datasets view
      (Library tab: list/columns/typed-query preview, create from tabular
      source, new immutable version, no delete on purpose), ✔ attach popover
      "also create a dataset" chip preselected for tabular files (upload
      handler returns the Source), ✔ "Chart this" → prefilled chat composer,
      ✔ Dashboards page "Ask the agent for a chart" button; ✔ SHELL (commit
      31f860a): icon rail of four doors + contextual sidebars over a two-level
      NAV registry (sections), Knowledge folded into Library, Databases moved
      to Library→Data, collapse hides the sidebar and keeps the doors, mobile
      drawer carries labelled destinations; ✔ Dashboards real list page
      (83aa547: catalog popover → on-page shelf under the pinned grid);
      REMAINING: finish-the-job pin chooser on chat chart artifacts,
      Boards&Todos glyph/toast merge, Knowledge cross-links
- [ ] Phase 4 — Trust surfaces: Rules/Policies (grant enumeration + revoke),
      member-readable org policy, scope labels, per-thread setting
      persistence, review banner (editor never replaced), status popover
- [ ] Phase 5 — Power tier (started 2026-08-19, reframed as Claude-desktop
      parity per user): ✔ Cmd+K palette (76bad8d — every view incl. settings,
      the six creates with in-place naming, thread search by title);
      ✔ thread rename (PUT /conversations/{id}/title, inline rail edit,
      subject threads refused); ✔ transcript deep search in the palette
      (b4993d8 — GET /conversations/search over the conversation index, same
      visibility chokepoint as the agent tool); REMAINING for desktop parity:
      artifacts-style right split pane (the big one), message edit,
      G-chords + universal Favorites + sidebar pruning + agent live try-chat
      + English-first Schedule composer
- [ ] Phase 6 — Layouts & polish: saved named layouts, per-collection open
      behavior, graph legend/selection, onboarding walkthrough, mobile
      bottom-tab shell

## Review

Phase 1 (2026-08-19): tsc clean; 537/537 web unit tests (navigation.test.ts
rewritten to pin the new IA); full Playwright suite 63 passed / 0 failed / 1
skipped. Two long-standing e2e defects found and fixed along the way:
(a) workspace.spec's 45s expect margin sat inside Playwright's default 30s
test timeout, so the margin could never apply — fixed with a 180s per-file
budget; (b) the real "agent-write flake" was a race: specs typed into the
composer before the async "New thread" switch landed, so prompts fell into
the previous thread (two specs' tool cards in one transcript). Fixed with a
shared e2e `newThread()` helper that waits for the empty-thread starter
heading (which Phase 1 added) before returning.

Phase 2 (2026-08-19): GET /api/inbox with 8 API tests (incl. the no-scan-bound
regression test and the WorkflowRun-only budget hold); full backend suite exit
0; tsc clean; 563/563 web unit tests; full Playwright suite 67 passed / 0
failed / 1 skipped — with the Spaces, plan-mode and conversation-index
features from the three parallel sessions all landed in the same tree. Two
defects found by peer review (dashbored-0e) and fixed: the feed's Run-anchored
holds query missed a workflow held between nodes with no backing run (second
WorkflowRun sweep added), and the strip's copy drifted from the product's
"Held by the spend limit" vocabulary (now one exported constant). The e2e
newThread helper went through two more iterations — count-based switch
signals all race the rail's first load because the "No conversations."
placeholder also shows while fetching; the stable signal is the conjunction
(active rail row is "New conversation") AND (starter heading visible).

# Plan mode + slash commands (/plan, /btw) — approved 2026-08-19

Design: plan mode is a fourth `ApprovalMode` value (`plan`) — stricter than
`ask_all` for writes (deny, not ask), read-only tools still run, exits via an
`exit_plan_mode` tool that parks on the existing approval machinery. Exiting
restores `ask_writes` (no new column). `/btw` sends an aside: a user Message
with no Run — lands in the transcript, no agent turn. Slash commands are
frontend-resolved (like skills), never parsed server-side from text.

## Backend

- [x] `agent_loop.py`: add `PLAN` to `ApprovalMode`/`APPROVAL_MODES`
- [x] `evaluate_policy`: plan branch — read-only keeps base verdict,
      `exit_plan_mode` asks unconditionally, writes deny; org clamp unchanged
- [x] `approval_mode_for_run`: pass `plan` through; plan outranks the dev
      bypass; injection flag still escalates to `ask_all` (documented tradeoff)
- [x] Plan instructions block spliced per loop entry (`_plan_narrowed`, called
      from both entries — not `resolve_directives`, left alone for the
      concurrent Spaces work)
- [x] `exit_plan_mode` tool (llm_tools): read-only spec whose preview IS the
      plan; spliced into the registry only in plan mode; approving it at the
      decision endpoint restores `ask_writes` before the resume, audited
      `via: exit_plan_mode`; `remember` is ignored for it
- [x] `PUT /conversations/{id}/approval-mode`: accepts `plan` (schema literal)
- [x] Asides: `aside: true` on send_message → Message without Run, run
      nullable in the response, own idempotency operation, audited
- [x] Tests: test_plan_mode.py (12), test_asides.py (5), PLAN added to
      test_approval_mode's no-mode-clears-a-deny table

## Frontend

- [x] api-client: `plan` in the mode type, `sendAside`, `run: Run | null`
- [x] `approval-format.ts`: "Plan first" entry (control renders it via
      APPROVAL_MODES)
- [x] `chat.tsx`: built-in commands merged into the slash picker (Terminal
      icon, listed first, Enter picks command before skill); /plan toggles the
      mode in place, /btw completes the token and the send path records an
      aside; plan-review card renders the plan as markdown and hides
      "always allow"; asides render dashed/muted
- [x] `thread.ts`: aside branch in submitPrompt (no followRun; bare "/btw"
      refused), regenerate skips asides
- [x] Pure module `views/commands.ts` + tests/commands.test.ts
- [x] Verify: api suites green, web tsc clean, 545/545 vitest, next build ok

## Review

Design change made while implementing: the mode restore moved from the exit
tool's executor to the decision endpoint. Executor-side restore left the
resumed turn on the plan-narrowed registry (registry/instructions resolve per
loop entry, before the queue drains), so approving a plan could not implement
until the next message. Endpoint-side restore re-enters the loop under the
full registry; the parked exit call keeps its spec via a `setdefault` in
`_continue`. `force_ask` ended up unused — the exit's unconditional `ask`
lives in `evaluate_policy`'s plan branch so no standing row can pre-answer it.
One behavior pinned by test rather than planned: a write approved before the
thread switched into plan mode does NOT run on resume (mode switches are
safety switches; same rule as auto_writes mid-turn). Full-suite runs also
surfaced other-session noise (Spaces migration double-head, conversation_index
db.get audit, GET /api/inbox route case) — not addressed here.

E2e addendum (2026-08-19, second pass): plan-mode.spec.ts (4 tests — /plan
toggle + plan card approve lifts the mode, deny keeps planning, /btw aside
persists) green. Writing it flushed out and fixed two real bugs plus one
scripted scenario:
- handlers/chat.ts: setApprovalMode silently no-oped while a fresh thread's
  createConversation was in flight — hoisted ensureConversation and shared it
  with the send path, so picking /plan on a brand-new thread conjures the
  thread like typing does;
- use-workspace.ts loadWorkspace: the auto-select's listMessages could resolve
  AFTER the user opened a new thread and stomp its empty transcript with the
  old thread's messages — now guarded on activeConversationRef, same pattern
  as the stream loop;
- e2e/shell.ts newThread(): the starter heading is already visible on a fresh
  load with nothing selected, so it never observed the switch — now also waits
  for the new rail row.
Full e2e: 64 passed, 3 failed — two workspace.spec failures pass in isolation
(suite-order flakes), and budget.spec:299 is the Phase-2 feed rework dropping
budget-held workflow runs from feedWaiting plus a ceiling/limit copy drift;
reported to the Foyer session.

# Conversation index — quote past conversations to agents (approved 2026-08-19)

Design: full transcripts already persist (`Message`), but cross-thread history
is unreachable — the only channel is extracted MemoryItems (≤5/run, 6 recalled).
Add a `conversation_chunks` table (chunked transcript windows + one LLM summary
row per thread), indexed with the existing embedding machinery, searched by a
new read-only `search_conversations` core tool that returns verbatim quotes
with provenance (thread title + date). Retrieval is memory.py-style hybrid
(LIKE lexical + dense cosine) fused with RRF. Visibility mirrors
`conversations.resolve_visible` through a single `_visible` chokepoint —
private threads never leak (the ffa0608 rule). Tool output is already screened
(`kind="tool_output"`), so quotes inherit injection screening. No memory.py
changes (avoids the in-flight Spaces collision); scoping joins Conversation,
which already carries `space_id`.

- [x] 1. Model: `ConversationChunk` (kind chunk|summary, ordinal, content,
      message_ids_json, message_count, last_message_at, embedding) + migration
      0044 (guarded create_table; renumbered from 0043 and re-pointed onto the
      concurrent session's 0043_inbox_call_index; round-tripped up/down/up on
      a scratch DB)
- [x] 2. Settings: conversation_index_enabled, conversation_search_limit,
      conversation_lexical_candidate_limit, conversation_vector_candidate_cap
      (+ .env.example entry)
- [x] 3. model.py: `summarize_conversation` (LLM, minimal effort, "" on any
      failure → caller falls back to naive topics line); usage op
      CONVERSATION_SUMMARY
- [x] 4. services/conversation_index.py: incremental chunking (pack whole
      messages ~1200 chars, split long ones via make_chunks, watermark =
      covered message ids), summary refresh every 10 messages, best-effort
      embedding, `reconcile` (bounded self-heal at search time),
      `search_conversation_chunks` with `_visible` chokepoint
- [x] 5. Tool: `search_conversations` in the core family (auto-granted to
      subject threads via SHARED_FAMILIES)
- [x] 6. Hook: `update_conversation_index(run.id)` in `_finish_run` after
      write_conversation_memory, same best-effort contract
- [x] 7. scripts/backfill_conversation_index.py
- [x] 8. Tests: 13 in test_conversation_index.py — incremental chunking,
      summary fallback + cadence, visibility (private / shared / subject /
      cross-workspace / chokepoint structure), dense arm honours the gate,
      reconcile self-heal, off switch, tool registration + attributed quotes;
      plus two reviewed entries in test_tenant_isolation's DB_GET_ALLOWLIST
- [x] 9. Verify: full pytest suite green, exit 0, zero failures. Along the
      way, cleared 4 failures from the concurrent sessions' in-flight work:
      the 0040 migration trio died replaying the chain on a partial DB (0042's
      `_add_space_column("sources")` — the spaces session guarded it itself;
      0043_inbox_call_index's `get_indexes` had the same hole — I added a
      `_table_missing()` guard), and test_route_table_matches_the_app needed
      an isolation case for the new GET /api/inbox (added: SCOPED). Both
      sessions notified.

## Review

Implemented 2026-08-19. Migration 0044_conversation_index (renumbered onto the
concurrent session's 0043_inbox_call_index head; up/down/up round-trip on a
scratch DB). New `conversation_chunks` table is derived data: verbatim
transcript windows (~1200 chars, whole messages, `make_chunks` splits
oversized ones; coverage tracked by message id so indexing is incremental and
never repacks) plus one rolling summary per thread (LLM via
`summarize_conversation` on the cheap context model, refreshed every 10
messages, naive topics-line fallback offline — so scripted mode stays
hermetic). Search is memory-recall-shaped: LIKE lexical arm + cosine dense arm
fused with RRF, both arms behind the `_visible` chokepoint that mirrors
`resolve_visible` clause for clause (structural test pins the columns to one
occurrence). `search_conversations` ships in the core family → reaches subject
threads automatically; results are quoted verbatim with thread title + date
and inherit the existing tool_output injection screen. Post-run hook in
`_finish_run` + bounded search-time reconcile (self-heals cron/tool-path
threads) + bulk backfill script. 13 new tests pass; two `db.get` call sites
reviewed into DB_GET_ALLOWLIST. Verification was twice disrupted by the
concurrent session (shared test DB corruption — they patched conftest to
per-pid DB files — and a transient memory.py syntax error mid-edit).

# Spaces — Claude-Projects-style grouping (approved 2026-08-19)

Plan: ~/.claude/plans/resilient-fluttering-lake.md. Space = workspace-shared
grouping of rail threads + per-space instructions + scoped knowledge +
space-scoped memory. Delete is destructive (threads/sources/memories die).

- [x] 1. Model + migration 0042 (Space table; space_id on Conversation/Source/MemoryItem; widen memory unique key) — round-tripped on scratch DB
- [x] 2. services/spaces.py + api/spaces.py CRUD (+ extract purge_source; register in main.py)
- [x] 3. Conversations: create with space_id, ?space_id= filter, emit in ConversationOut
- [x] 4. Directives: append space instructions in resolve_directives
- [x] 5. Retrieval scoping: search_evidence + _search_sources; sources upload/list space_id; ToolContext.space_id
- [x] 6. Memory space axis: _active/_upsert_item/shadowing/write path/tools; eval unchanged (93.3%/0%)
- [x] 7. api-client: Space types + CRUD, createConversation/uploadSource space params
- [x] 8. Nav + state: spaces View, use-workspace state, newConversation(spaceId)
- [x] 9. SpacesView + space-threads.ts pure module + rail chip + CSS
- [x] 10. Tests: isolation harness (+5 route cases), test_spaces/directives/retrieval/memory (28), web unit/CSS/RTL (18), e2e spaces.spec.ts
- [x] 11. Verify: full pytest exit 0; memory eval 93.3%/0% unchanged; retrieval eval floors ok; 563 web units; spaces e2e green (full e2e run queued behind the Foyer session's port hold)


## Review (Spaces, 2026-08-19)

Landed end to end on feat/agentic-workspace. Notable decisions vs the plan:
delete cascade confirmed destructive (D6) and asserted in e2e; memory recall
derives its space from the conversation inside recall()/remember_memory()
(mirroring memory_owner) instead of a threaded param, so read/write scope
cannot disagree; GET /api/memory widens via an explicit ALL_SPACES constant
so space rows stay administrable. 0042_spaces gained has_table guards after
the migration-replay tests caught partial-DB builds. Detail rename input is
aria-label "Rename space" (create input owns "Space name").

## Merge notes (feature-sweep, 2026-08-23)

- MIGRATION RENUMBERING (QA finding #1, do at merge time): this branch's
  migration chain 0045–0055 must be renumbered onto the then-current head at
  merge. Last common revision is 0044_conversation_index; mainline
  feat/agentic-workspace and bg/marketplace-todo both claimed 0045+
  independently (mainline: 0045_conversation_defaults → 0046_model_usage_agent
  → 0047_favorites; marketplace: 0045_marketplace → 0046_listing_installs).
  Whoever merges second re-parents their whole chain (rename files, update
  revision/down_revision pairs) and coordinates with the marketplace session so
  both don't take the same slots.

Known follow-ups deferred from the F3 QA review:

- QA #5: no caps/pagination on comment POST volume (50 mentions x 10KB bodies)
  or GET /api/inbox — member-DoS surface; fold into the F13 digests/inbox work.
- QA #9: comment-create TOCTOU between resolve_visible and commit on a
  concurrent un-share, and an N+1 resolve_visible per mention (bounded at 50)
  — revisit if mention fan-out grows.
- QA #10: mention chips in comment-format.ts re-derive from current member
  names, so a rename leaves stale chip text — cosmetic only.

Design consideration from the F5 QA review, for the F13 digests agent:

- F5 QA #10: monitor evaluation (workflows.py tick, monitors step) runs inline
  DuckDB inside the shared tick request — slow monitors delay workflow/cron
  dispatch behind them. F13 must not add more heavy inline work to the tick:
  digests should render/send via the background-enqueue path (or keep per-tick
  work strictly bounded), and a future fix could move monitor evaluation there
  too.
