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

# Marketplace — publish & share skills, workflows, agents (planned 2026-08-22)

Design: Grain Gallery won (judge 1: 8.5 — workspace-tier-first sequencing,
native fit with the GeneratedApp/AppRelease + SkillVersion patterns), hardened
with both judges' grafts from the two Granary proposals. Two new tables in
migration 0045 (next free at planning time; 0044_conversation_index is head),
zero columns added to existing tables. Publishing snapshots a
Skill/Workflow/Agent into an immutable ListingVersion (append-only,
content_hash, changelog); installing copies the payload into the caller's
workspace as an ordinary local row — copy, never reference, so a remote author
can never mutate what runs in your workspace and delisting never breaks
installs. Trust rules are load-bearing: per-kind allowlist serializers
(pydantic extra="forbid", nothing serializes by default), a secret/token-shape
lint at publish, full-body preview as a hard gate before the Install button
enables, installs land inert (skill shared=false, agent enabled=false,
workflow draft + manual trigger), an agent scope-review sheet with
write-capable tools unchecked by default, and local ToolPolicy/OrgToolPolicy
ceilings untouched — no marketplace bypass path. Integer versions + changelog
(house SkillVersion/AppRelease convention; semver, publisher handles, public
tier, and import/export consciously deferred — /published/apps is the
ready-made PUBLIC template when wanted). The status enum reserves 'taken_down'
now so the report→takedown→banner flow (required before any public tier) needs
no migration revisit. Feature flag `marketplace_enabled` in config.py. Crons
are never publishable.

## Phase 1 — Skills gallery, in-workspace (publish → browse → install)

- [x] 1. Model + migration 0045_marketplace.py: `Listing` (organization_id FK,
      workspace_id FK publisher, kind 'skill'|'workflow'|'agent', slug unique
      per org `^[a-z0-9-]+$`, title, description, visibility
      'workspace'|'org' default 'workspace', status
      'published'|'delisted'|'taken_down' — 'taken_down' reserved now, wired
      in the pre-public follow-up — author_name byline captured at publish,
      created_by, install_count, latest_version, timestamps) and
      `ListingVersion` (listing_id FK, version int UNIQUE with listing_id,
      payload_json, content_hash per the skills.py sha256 convention,
      changelog text, source_id plain string NOT an FK (Run.skill_id
      rationale: survives source deletion), source_version, created_by,
      created_at). Round-trip up/down/up on a scratch DB
- [x] 2. Config: `marketplace_enabled` (default False) in
      apps/api/app/config.py, memory_enabled pattern; gates the router
- [x] 3. services/marketplace.py: `SkillPayload` allowlist serializer —
      exactly title/description/body/args_json, extra="forbid" — plus
      snapshot_skill() reading the current SkillVersion, content_hash, and a
      `resolve_visible` chokepoint mirroring conversations.resolve_visible.
      Secret lint over every payload string (token-shape regexes + entropy +
      tokened-URL/workspace-id detection): publish 422s with findings.
      STRIP TEST: assert the payload key set is exactly the allowlist —
      workspace_id/created_by/secrets can never ride along by construction
- [x] 4. apps/api/app/api/marketplace.py: POST /api/marketplace/listings
      (kind='skill' + skill_id, idempotency-keyed, 409 on duplicate slug,
      audit `listing.published`; republishing a slug appends a ListingVersion
      and bumps latest_version, 409 on identical content_hash, changelog
      required), GET /api/marketplace/listings (via resolve_visible), GET
      /api/marketplace/listings/{id} (detail incl. FULL payload + version
      history), POST /api/marketplace/listings/{id}/install (copies payload
      into a new Skill: shared=false, version 1, deterministic '-2' suffix on
      name collision — never a 409; bumps install_count; audit
      `listing.installed`). Any member may publish at workspace visibility
- [x] 5. Schemas ListingOut/ListingCreate/ListingDetailOut/InstallOut in
      schemas.py; api-client listListings/getListing/publishListing/
      installListing in packages/api-client/src/index.ts
- [x] 6. Isolation: ROUTE_CASES entries in apps/api/tests/isolation.py for
      all four routes — SCOPED same-workspace, cross-tenant DENY 404 — so
      test_route_table_matches_the_app cannot pass without verdicts
- [x] 7. apps/api/tests/test_marketplace.py: publish snapshots the current
      version (later skill edits don't mutate the listing), the strip test
      from item 3, secret lint blocks a fake token in a body, install yields
      an independent editable Skill, delisted/deleted source leaves installs
      intact, duplicate-slug 409, name-collision suffix
- [x] 8. Web: 'gallery' view in views/navigation.ts (Library group) +
      "Browse gallery" entry in views/commands.ts; views/gallery.tsx card
      grid reusing the apps.tsx 'dashboard-gallery' styles (kind badge,
      byline, install count, client-side substring search) with a detail
      drawer; Install button stays DISABLED until the full body has been
      rendered to the installer — hard gate, not an optional preview.
      "Publish to Gallery" modal (slug/title/description/byline/changelog)
      from the SkillsView kebab in views/skills.tsx
- [x] 9. Verify: make lint, pytest (full suite exit 0), pnpm test, pnpm
      build, e2e (gallery publish→install spec)

## Phase 2 — Org tier: cross-workspace sharing

- [x] 1. Extend services/marketplace.resolve_visible: 'workspace' rows filter
      workspace_id; 'org' rows filter organization_id joined through the
      caller's Membership→Workspace. Single chokepoint — no marketplace query
      may bypass it (any bypass is a cross-tenant leak)
- [x] 2. Gates in api/marketplace.py: publishing or PATCHing to
      visibility='org' requires require_owner (auth.py:296 — same gate as
      the Skill.shared flip and GeneratedApp publish). PATCH
      /api/marketplace/listings/{id}: title/description/visibility/
      status='delisted' only (payload stays immutable), author-or-owner
- [x] 3. Isolation (the novel query shape — named, not implied): add a
      same-org-second-workspace fixture to tests/isolation.py; ROUTE_CASES
      proving an org listing is SCOPED-readable and installable from a
      sibling workspace, workspace-tier listings stay invisible to siblings,
      and cross-org is DENY 404
- [x] 4. test_marketplace.py: installing an org listing lands the copy in
      the installer's workspace_id
- [x] 5. Web: visibility selector in the publish modal (Workspace /
      Organization), 'Org' badge on cards, publisher workspace name in the
      detail drawer
- [x] 6. Verify: make lint, pytest, pnpm test, pnpm build, e2e

## Phase 3 — Workflows and agents as listable kinds

- [x] 1. services/marketplace.py `AgentPayload`: name/description/
      instructions + allowed_tools INTERSECTED with the universal
      registry_families names (llm_tools.py); workspace-specific MCP/sandbox/
      custom tool names DROPPED and recorded as `unresolved_tools` in the
      payload (degrade-with-warning, not 422). Strip test: payload never
      contains a workspace MCP/sandbox tool name
- [x] 2. `WorkflowPayload`: name/description/source_prompt/graph_json with
      trigger FORCED to manual; schedule_cron/timezone/last_dispatched_at/
      workspace ids excluded by construction; node arguments_json strings run
      through the Phase-1 secret lint. Strip test: no schedule trigger or
      dispatch state in the payload. Crons rejected as a publishable kind
      (documented in the router)
- [x] 3. Install paths in api/marketplace.py: agent lands enabled=false
      ALWAYS — even after the scope sheet is confirmed (judge-2 graft:
      re-arming is a separate deliberate act in the agent editor; adding a
      row never trips the last-enabled-agent 409). Workflow lands
      status='draft' after revalidation via
      services/workflows/compiler.compile_document against the installing
      workspace's registry; missing tools returned as warnings in InstallOut,
      not errors
- [x] 4. Agent scope-review sheet in gallery.tsx (trust-first graft):
      requested tools grouped by family via GET /api/tools (same source as
      the agents.tsx editor), read/write badges, write-capable tools
      UNCHECKED by default; the confirmed subset ∩ local registry becomes the
      new agent's allowed_tools_json. Test: the installed agent still routes
      through resolve_policy with ToolPolicy/OrgToolPolicy ceilings intact —
      no marketplace bypass
- [x] 5. Requires-vs-installed checklist on the detail drawer BEFORE install
      (registry-Granary graft): render unresolved_tools / workflow
      required-tools against GET /api/tools with present/missing badges —
      the main mitigation for the installs-broken-as-designed trust risk
- [x] 6. Publish kebabs in views/agents.tsx and views/workflows.tsx; gallery
      kind filter tabs (Skills / Workflows / Agents); per-kind detail
      rendering (instructions view, read-only workflow-graph.tsx preview)
- [x] 7. Tests: installed agent is disabled, installed workflow is
      draft+manual and revalidated, cross-workspace install with a missing
      tool surfaces the warning; ROUTE_CASES verdicts for any new route
      shapes
- [x] 8. Verify: make lint, pytest, pnpm test, pnpm build, e2e

## Phase 4 — Installs, updates, divergence, lineage

- [x] 1. Model + migration 0046_listing_installs.py: `ListingInstall`
      (listing_id FK, listing_version_id FK, workspace_id FK, target_kind,
      target_id string, content_hash_at_install, pinned bool default false,
      created_by, created_at, UNIQUE(workspace_id, listing_id) — no
      duplicate-install rows, unambiguous update tracking). The install
      endpoint writes a row; install_count derived from it going forward
- [x] 2. Update detection: per-install state current | update_available
      (installed version < latest_version) | diverged (local content_hash !=
      content_hash_at_install). Pinned installs excluded from update prompts
      (teams that froze a known-good copy aren't nagged)
- [x] 3. POST /api/marketplace/listings/{id}/update: applies the newest
      payload onto the tracked target — skills as a NEW SkillVersion (the
      existing restore route becomes one-click rollback from a bad update),
      workflows via the recompile-bump PATCH path, agents overwrite with an
      audit row. Diverged targets require an explicit confirm-overwrite flag
      (else 409) — never silently clobber local edits. Never automatic.
      Audit `listing.republished` / `listing.updated`
- [x] 4. GET /api/marketplace/listings/{id}/versions; ROUTE_CASES verdicts
      for the versions/update routes; tests: hash/version update detection,
      diverged blocks silent update, pinned suppression, skill update lands
      as a restorable SkillVersion
- [x] 5. Web: version history + changelogs in the detail drawer, update
      dialog rendering the target version's changelog AND a body diff before
      applying (the prompt-injection review surface), 'Installed' /
      'Update available' / 'Pinned' badges on cards, pin toggle
- [ ] 6. Spec item (designated follow-up BEFORE any public tier — no build
      yet): report → org-admin takedown (status='taken_down') → banners on
      installed copies via the content_hash/ListingInstall provenance join;
      the public tier itself reuses the /published/apps PUBLIC-verdict
      pattern and requires revisiting the free-text author_name byline
      (verified publisher identity) first
- [x] 7. Verify: make lint, pytest, pnpm test, pnpm build, e2e

## Review (built 2026-08-22, branch bg/marketplace-todo)

All four phases shipped: 00e7a9d (Phase 1), 4cee3bf (Phase 2), a2bbd16
(Phase 3), 79087a1 (QA fixes), 776e6e3 (Phase 4). 33 marketplace tests +
isolation-sweep coverage for every route; gallery e2e covers
publish→install and publish→install→republish→update.

Deviations from the plan, all deliberate:

- `marketplace_enabled` defaults **True**, not False — the house pattern is
  a kill switch, not a launch gate, and a default-off flag would make the
  e2e and the feature itself dead on arrival. The off state still answers
  404 everywhere (tested).
- Republishing a slug is bound to the **source's lineage** (same publisher +
  same kind + same source_id); anything else reusing the slug answers 409
  "not available" — wording that never confirms an invisible listing exists.
- `install_count` stayed an event counter (each install bumps it) rather
  than being derived from ListingInstall rows — re-installs re-point the
  one lineage row, so deriving would silently change Phase 1 semantics.
- No separate GET /listings/{id}/versions route: the detail payload already
  carries the full version list, and a second read path would double the
  isolation surface for no reader.
- The update dialog shows the new version's changelog and full body (the
  drawer always renders the head payload) but not a line-diff; the diff is
  a follow-up if reviewing long bodies proves painful.
- Updates rewrite **content fields only** — a skill's shared flag, an
  agent's enabled + local tool grant, a workflow's status/trigger all
  survive. New tool requests surface as warnings, never as grants
  ("new words, never wider reach"). Skill updates land as ordinary
  restorable SkillVersions. A pinned install stops being offered updates
  server-side (install_state says "installed"), so every surface agrees.
- Delist/restore is API-only (PATCH status); no UI affordance yet.

Adversarial QA (continuous session) findings closed in 79087a1 + 776e6e3:
org-listing republish is now owner-gated like widening; republish honors
visibility="org" and never silently narrows; duplicate-slug/taken-down
conflicts answer non-confirming "not available"; whitespace-only titles
422.

⚠ Cross-branch migration hazard: 0045_marketplace + 0046_listing_installs
revise 0044_conversation_index, and so do 0045_templates
(worktree-feature-sweep) and 0045_conversation_defaults
(feat/agentic-workspace). Whichever branch merges second needs re-parenting
or an alembic merge revision.
