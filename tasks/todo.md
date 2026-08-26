# Security audit + rate limiting (bg/security-audit, 2026-08-25)

## Plan
- [x] Recon: auth architecture, existing limiter (auth-only, per-IP fixed window)
- [ ] Parallel audit: public surface, rate-limit gaps, injection/SSRF, secrets/crypto, frontend, IDOR
- [ ] Triage findings by severity; confirm each against code before fixing
- [ ] Implement general rate limiting:
  - [ ] Reuse `services/auth/ratelimit.RateLimiter` as the engine
  - [ ] Tiered limits (public token endpoints, expensive/LLM endpoints, general write, bearer-token guessing)
  - [ ] Settings knobs following `auth_rate_limit_*` naming; 429 + Retry-After
  - [ ] Tests; ensure existing suite unaffected (reset between tests)
- [ ] Fix confirmed high-severity audit findings (minimal-impact changes)
- [ ] Run api tests + web tests + lint; commit and push

## Review

Six parallel audits ran (public surface, rate-limit gaps, injection/SSRF, secrets/crypto,
frontend, IDOR). IDOR came back clean; SSRF/SQLi/command-exec are well-guarded. Confirmed
findings and what was done:

### Fixed
- **HIGH — LaTeX path traversal → arbitrary host file write** (`services/projects/compile.py`).
  Per-file `path` was staged as `tmpdir / path` with no containment check. Now every path is
  normalized through `store.normalize_path` (rejects absolute/`..`/backslash) and `_write_files`
  re-asserts containment against the resolved root. Tests added.
- **Rate limiting (the ask)** — new `app/api/ratelimit.py`: reuses the auth `RateLimiter` engine,
  adds per-identity (`rate_limit`), per-token (`token_rate_limit`), and per-IP (`public_rate_limit`)
  dependencies with three tiers (heavy / mint / public) configured in `config.py`. Applied to
  20 high-cost routes: chat send+edit, workflow run/compile/tick, graph rebuild, app generate,
  sandbox create/run, source ingest, latex compile, api-token + share-link mint, and the public
  doors (shared/{token}, published apps, hooks, MCP, inbound email). Coverage tripwire test
  (`test_rate_limit.py`) fails if any of them loses its limiter. 429 carries Retry-After.
  Autouse conftest fixture resets the limiter between tests.
- **MEDIUM — sandbox secrets on docker argv** (`services/sandbox/container_provider.py`).
  Was `-e KEY=value` (world-readable via `ps`/`/proc/cmdline`); now name-only `-e KEY` with the
  value supplied through the docker CLI's own environment (owner-readable only). Test updated.
- **MEDIUM/HIGH — stored XSS via "Open original"** (`web/components/views/sources.tsx`).
  Blob nav opened SVG/HTML as same-origin active documents. New `viewableBlob` allowlists inert
  types (PDF, raster) and re-wraps everything else as octet-stream. Test added.
- **MEDIUM — published app frame CSP** (`api/generated_apps.py`). Added `sandbox allow-scripts`
  so a direct top-level visit gets an opaque origin, not the API origin.
- **LOW — localhost CORS survives to prod** (`config.py`). New `_guard_web_origin` refuses a
  localhost-only WEB_ORIGIN outside dev/test. Test added.
- **LOW — non-ASCII Authorization 500s** (workflows tick, inbound email). Compare as bytes.

### Noted, not changed (larger scope, recommend as follow-ups)
- Share links never expire (`expires_at` supported by service, not passed by route).
- Inbound email has no per-message provider signature / replay protection (only a static bearer).
- Fernet key has no rotation path (single key; consider MultiFernet).
- Outbound webhook HMAC signs body only (no timestamp → replayable). `/health` unauthenticated DB hit.
- Pre-existing: `api_tokens.router` is included twice in `main.py`; `services/harness/__init__.py`
  has a pre-existing mypy dict-item error (both untouched by this branch).

### Verification
- Full API suite: exit 0, ~2500 tests, 0 failures. Web suite: 742 passed. Web typecheck + lint clean.
- ruff clean on all changed files; mypy clean on the 3 changed non-test modules.

# Finish-the-todo run (started 2026-08-22)

Goal: complete Phases 3–6 of Foyer + loose ends, production grade.

- [x] Loose end: budget.spec:299 — re-ran in isolation, passes (the Phase-2
      session's feedWaiting second-sweep + copy constant landed); nothing to do
- [x] Phase 3 remainder: pin chooser on chat chart artifacts (2026-08-22:
      finish-the-job pin bar on succeeded create/update dashboard cards —
      id-parse only, no name fallback after review; "Make this a dashboard"
      seed on image charts, pane-aware after re-review; server copy reworded)
- [x] Phase 3 remainder: Boards & Todos glyph/toast merge (2026-08-22: one nav
      item, merged listing with data-shape glyphs, graduation notice toast
      with nonce timer + multi-flip join, confirm-gated deletes both shapes,
      keyboard moves: card up/down + two-step Move-to + column chevrons with
      busy guards; e2e specs updated, teleport assertion inverted)
- [x] Phase 3 remainder: Knowledge cross-links (2026-08-22 wf): purpose
      subtitles on all three views; six link directions (sources↔graph↔memory,
      memory→thread provenance); space chips on source+memory rows;
      use-focus-reveal.ts extracted from the dashboards recipe;
      13 RTL tests in knowledge-crosslinks.test.ts
- [x] Phase 4: grants API → Inbox Rules tab + Policies page (2026-08-22 wf:
      views/policies.tsx RulesTable — descriptions from the registry, plain
      scope words, You/Everyone + grantor, revoke with owner gate — mounted in
      Inbox as a Rules tab AND as the "Rules & policies" view under the Inbox
      group; the two "always allow" checkboxes' copy fixed to "for me";
      organization.spec re-pointed; policies-rules.test.ts)
- [x] Phase 4: member-readable org policy (OrganizationPanel moved out of
      AdminView's 403 wall onto the Policies page, unmodified)
- [x] Phase 4: scope labels on composer controls (2026-08-23: "Agent/Model/
      Reasoning effort · this thread" + remembered-on-thread titles;
      composer-scope.test.ts)
- [x] Phase 4: per-thread persistence of model/effort/agent (2026-08-23 web
      half: shell seeds pickers once per thread switch from Conversation
      defaults with a stomp-guard ref, pick* wrappers write through
      setConversationDefaults and patch the rail row; extra panes seed from
      their row and write back through the onApprovalChanged channel; fast
      stays per-turn)
- [x] Phase 4: approval-mode control in subject chats (2026-08-23:
      use-subject-thread keeps the row's mode + setApprovalMode with a
      stale-reply guard; panels pass the approval bundle, starter cards
      opt-out via ChatView.showStarter)
- [x] Phase 4: review banner (2026-08-23 wf + fixes): review DEFAULT-OPENS per
      proposal (render-time state adjustment, no editor flash — keeps the e2e
      no-click contract), "Later" parks it behind a banner over a read-only
      editor; the interlock covers EVERY write path after review (Save, ⌘S,
      History→Restore all disarmed while a proposal pends); tree dots via
      FileTree pendingIds
- [x] Phase 4: system-status popover (2026-08-23 wf): SystemStatus on
      DisclosureMenu replaces the agent/screen pills; one useApiHealth loop
      feeds both the down-banner (unchanged behavior) and the status dot
- [x] Phase 4: Inbox snooze/Later (2026-08-23 wf + fixes): client-side
      grain.inbox-snooze via total-parser module (now with pruneSnoozes
      against the waiting set), Later/Unsnooze + "Later (N)" tab, waiting
      strip filtered and synced via onSnoozesChanged, rail badge deliberately
      unchanged; 12 unit tests
      (also fixed from review: thread-default seeding self-heals a deleted
      agent id through AgentSelect → the thread's remembered default)
- [x] Phase 5: unified right split pane (2026-08-23 wf): split-sizes.ts pure
      module (per-column-count persisted ratios under grain.split-sizes, 21
      tests) seeding chat-split's existing drag; pane maximize (ChatPane head
      button + primary corner button, display:none siblings, Escape restores);
      cap toast in openInNewPane (addPane stays pure per the identity test);
      inline close-confirm on a live-run pane (no scrim, auto-dismisses when
      the run settles); one --split-width var across the three subject-chat
      grids; palette ⌘Enter/⌘click opens a thread in the split; ⌘\\ cycles
      pane focus. Listed gap: no automated test on the close-confirm (hook
      needs network; e2e needs a live run in a pane)
- [x] Phase 5: message edit (backend 2026-08-22, web 2026-08-23): pencil on
      the viewer's own prompts (senderIsViewer predicate — asides and
      teammates excluded, legacy ""-sender editable on personal threads
      only), inline textarea (Enter/Shift+Enter/Escape, no blur-submit),
      optimistic truncate + followRun, refused edits keep the words on
      screen; wired in primary + extra panes, subject panels deferred;
      message-edit-ui.test.ts (4) + senderIsViewer cases in rail-threads
- [x] Phase 5: G-chords (G C / G I / G L / G A / G D) — views/chords.ts pure
      module + 9 tests, capture-phase listener in workspace.tsx (stops a
      completed chord reaching the Inbox's A/D triage), palette rows show
      their chord as a kbd hint (e2e pass still owed with the phase gate)
- [x] Phase 5: universal Favorites + sidebar pruning (2026-08-23 wf + fixes):
      one useFavorites instance feeding the sidebar FavoritesNav (glyphs
      derived from NAV_GROUPS, keyboard reorder with aria-live announcements,
      serial-queue race guard) + star mounts on ALL eight kinds (thread row,
      document header, dashboard catalog, agent cards, cron rows, board
      headers via TodoChecklist headerExtra, project header, workflow
      header); unpinned favorited dashboard pins-then-focuses; sections got
      stable ids + per-section collapse keys (grain.section.*) with the
      active view auto-expanding its shelf; Pinned-dashboards nav untouched
- [x] Phase 5: agent editor live try-chat (2026-08-23 wf + fixes): "Save &
      try" → scratch "Trying <name>" thread with default_agent_id seeded →
      lands in Chat; both buttons busy through the whole try path (no
      duplicate-agent window), pressed-button busy copy, partial-failure
      honesty (opens the thread even when the default couldn't be set)
- [x] Phase 5: English-first Schedule composer (2026-08-23 wf + fixes):
      sentence input + Compile → compiled-cron chip + "Next: …" line in the
      schedule's zone; raw cron/timezone behind Advanced; editing the
      sentence invalidates the receipt; timezone defaults to the viewer's
      zone; e2e/schedules.spec.ts (offline-compilable sentence)
- [x] Phase 6: saved named layouts (2026-08-23 wf; wiring finished inline
      after a mid-stage connection loss): layouts.ts pure module under
      grain.layouts.<workspaceId>, palette rows (Layout: X applies; ⌘⏎/⌘-click
      /⌘⌫ deletes; "Save layout as…" reuses the generalized NamingTask step),
      applyLayout writes ratios before panes with a ChatSplit resetKey for the
      unchanged-count case
- [x] Phase 6: per-collection open behavior (thread instance per research —
      grain.thread-open preference as a palette toggle, honored by the
      palette's Enter/⌘⏎ inversion AND favorite thread rows; dashboards/
      documents deferred with rationale: their second mode would be an
      invented feature, not a preference)
- [x] Phase 6: graph legend + bidirectional selection (2026-08-23 wf + review
      fixes): entity-type legend derived from the canvas PALETTES (light
      project recolored blue — was a twin of entity green; fallback got its
      own gray + a conditional "other" row), selectedId highlight via refs
      (no scene rebuild), row-click select + canvas→list scroll, stale
      selection cleared on rebuild; graph-legend tests + e2e additions
- [x] Phase 6 debt: G-chord kill-switch (WCAG 2.1.4) — grain.chords palette
      toggle folded into chordEligible, palette hints hidden while off
- [x] Phase 6: onboarding walkthrough (2026-08-23 wf + fixes): one-time
      approval-loop caption at the top of the first proposed call's card,
      grain.seen via the first-run.ts total-parser module; excluded from
      subject panels via the showStarter gate (a narrow panel must not spend
      the caption's one showing); 10 unit tests
- [x] Phase 6: mobile bottom-tab shell (2026-08-23 wf + fixes): four doors +
      badge as a bottom bar under 900px (renderGroupButton reused, drawer's
      duplicate destination list deleted), 100dvh + viewport-fit=cover so the
      bar clears mobile browser chrome and the home pill, scrim demoted to a
      backdrop (duplicate "Close menu" name removed); e2e/mobile-shell.spec
      at a 390×844 viewport; tile chrome hover/focus-within reveal with an
      @media (hover:none) capability fallback
- [x] Full gate (2026-08-23): ruff ✓ mypy ✓ full pytest ✓ eslint 0 errors
      (6 pre-existing warnings verified define-only at HEAD) ✓ tsc ✓
      716 web unit tests ✓ next build ✓ — final full e2e run + commit + PR in
      flight per the user's ask

Merge notes for other branches (the sessions that owned them have ended;
this file is the surviving warning):
- Alembic: this branch's chain is 0044→0045_conversation_defaults→
  0046_model_usage_agent→0047_favorites (merged to main). Templates/comments
  (worktree-feature-sweep) and marketplace (bg/marketplace-todo) each hang
  their own 0045+ off 0044 — re-parent to 0048+ at their merges.
- .thread rail grid: bg/marketplace-todo's 3067408 pins FOUR trailing columns
  and a thread-rail-css test whose regex does not know thread-favorite; this
  branch needs FIVE (the star). At merge take the 5-column union and add
  favorite to their regex — tests/thread-rail-grid.test.ts on this side fails
  loudly if the merge resolves the wrong way.

## Review — finish-the-todo run (2026-08-22 → 2026-08-23)

Everything on the todo shipped, production grade: Phases 3–6 of Foyer complete
plus the message-edit/favorites/schedule-compile stack. Method: ultracode —
read-only research agents mapped every seam first; implementation ran as
serial workflow stages over the shared hub files with adversarial review
panels after every batch; every finding was adjudicated and fixed same-day
(three review rounds caught, among others: a wrong-dashboard pin via name
fallback, a truncation sweep that could delete an overlapping teammate turn,
a review-interlock bypass through Save/⌘S/Restore, an unwired palette feature
shipping as dead code, and a 100vh tab bar sitting under mobile browser
chrome). Two infra hazards shaped the run: workflow agents died to machine
sleep/session limits three times (recovered from journals; the near-finished
message-edit stage was completed inline), and a peer session shared the
checkout throughout — attribution was verified per failure, their broken 0046
migration guard was fixed from here with notice, and cross-session QA traffic
was answered and re-routed. The first full e2e run after the phases caught a
real rail-row grid overflow (four actions in three columns) latent since the
rename feature shipped — the run that "wasn't needed" found the bug.

Review-debt (phase3 panel, 2026-08-22) — ALL FIXED same day:
- [x] truncate_after now sweeps by Run.created_at >= the pivot run's start
      (overlapping earlier turn survives; regression test with staged clocks)
- [x] shared threads: 409 when the sweep would take a teammate's run
- [x] dashboards/tools.py success line names the on-card pin control (kept
      the "(id <uuid>)" clause the pin bar parses)
- Accepted-risk notes: hasChartImage offers "Make this a dashboard" under any
  sandbox image (no client-side chart signal exists); G-chords lack a disable/
  remap setting (WCAG 2.1.4) — fold into Phase 6 polish.

Backend landed 2026-08-22 for Phase 5 (web halves pending):
- [x] English→cron: POST /api/crons/compile-schedule + next_fires preview
      helper (DST-aware minute scan, validator chokepoint shared with the
      ticker), scripted-mode deterministic parser, 17 tests
- [x] Universal Favorites: models.Favorite + migration 0047, service with the
      one resolve chokepoint (conversations via resolve_visible, the rest
      workspace-scoped), 4 routes modelled on dashboard pins, isolation cases,
      test_favorites.py; api-client types + methods landed
- [x] api-client contracts: Conversation.default_*, setConversationDefaults,
      editMessage, ToolPolicy.created_by, MemoryItem.space_id, Favorite CRUD,
      compileSchedule (fixtures updated; tsc + 593 web tests green)
- Concurrent-session note: a peer session shares this tree (steer feature,
  anthropic harness, delegation/guardian, admin scorecard, its migration
  0046); its 0046 upgrade guard and one E501 were fixed from here and the
  session notified. Its remaining red tests (harness registry ×3, delegation
  db.get review) are theirs.
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
- [x] 8. Mail service abstraction (console/dev fallback, SMTP config) — infra
      for 9/11/12
      (HTML support: OutboundEmail.html '' -unset; SMTP sender upgrades to
      multipart/alternative, console sender stays text-only; pure inline-CSS
      helpers services/mail_render.py render_table/render_link_button with
      html.escape on every interpolation — consumed by F10/F13; config
      guards untouched; tests in test_mail_render.py)
- [x] 9. Share links: revocable read-only public URLs (dashboard/document)
      (scoped to dashboards + documents, NOT artifacts: published apps
      already have their own public surface at /published/apps/{slug};
      share_links stores sha256 only, raw token in the 201 exactly once —
      blank on idempotent replay; GET /shared/{token} is PUBLIC and
      fail-closed 404 for unknown/revoked/expired/deleted, dashboards
      re-queried LIVE server-side; web page app/share/[token], modal on
      Dashboards + Documents)
- [x] 10. Dashboard subscriptions: scheduled snapshot delivery
      (0052_dashboard_subscriptions; per-member recipient validated as a
      workspace member — subscribing someone else needs the owner role, and
      the membership is re-checked at send so a departed member stops
      receiving; the tick only CLAIMS (day-wide "not yet fired in this
      period" window, so a late ticker still delivers today's mail once) and
      the live query + HTML render (mail_render) + SMTP run on a background
      task per the F5 QA note; skips audit dashboard.subscription_skipped;
      subscribe modal on Dashboards rows + read-only list on Schedules)
- [x] 11. Outbound webhooks + API tokens (trigger workflow / post to thread;
      event push) — DONE 2026-08-25 (api_tokens mirrors upstream ed7195b for
      the merge, revoke is upstream's DELETE; get_token_actor beside
      get_actor; /api/hooks trigger runs at WORKFLOW scope; deliveries
      claimed in tick, sent signed on background tasks, 3 attempts;
      "API & Webhooks" settings view)
- [x] 12. Inbound email → thread (provider-webhook endpoint, pairs with Rules)
      (0054_inbound_email; POST /api/hooks/email/inbound on the tick's
      bearer posture, hashed inbox+token@domain routing addresses minted
      owner-only, delivery = personal thread + user message with NO agent
      turn, message_id idempotency; "Email in" card in API & Webhooks)
- [x] 13. Notification digests: daily pending-approvals email per member
      (0055_digests membership columns; waiting-set queries extracted to
      services/inbox_feed.py shared by GET /api/inbox and the digest; tick
      claims hourly via sweep_claims + per-member digest_last_sent_at
      period-start UPDATE, render/send on background tasks per the F5 QA
      note; PUT /api/me/digest + bootstrap exposure; settings-menu toggle
      and hour picker)
- [x] Full gate: make lint, pytest, pnpm test, pnpm build, e2e (2026-08-25 on
      sweep-qa-fixes after the QA fix pass: lint/mypy clean, full pytest green,
      749 web unit tests, build clean, e2e 76 passed / 1 skipped / 0 failed —
      the first post-merge e2e run surfaced two pieces of four-branch-merge
      fallout, fixed in their own commit and recorded under "QA fix pass")

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


# Harness gap build — subagents, steering, guardian, observability (2026-08-22)

From the opencode/Codex gap study (artifact 18f3a615). Six features, sequenced
backend-first, agents own only new files, shared-file edits stay serial.

- [x] 0. Contracts: ModelUsage.agent_id column + index, migration 0046,
      usage.Attribution/usage_scope agent_id, ToolContext.run_id,
      config: anthropic provider fields (a peer session added the
      absent-table guard to 0046 — kept)
- [x] 1. Delegation: services/delegation.py — `delegate` tool (new "delegation"
      family), read-only child loop (no parking; budget/cancel abort), depth 1
      by construction, parallel batch execution for consecutive delegate calls
      in _drain_pending (own session + copy_context per thread; concurrency
      proven by a barrier test) — tests/test_delegation.py (8)
- [x] 2. ask_user tool (core family, force_ask) + decision endpoint inputs→
      amendment bridge + web answer card — tests/test_ask_user.py (6)
- [x] 3. Mid-turn steering: POST /api/runs/{id}/steer → steer.requested event
      + transcript Message row, loop pickup via LoopState.last_steer_sequence
      — tests/test_steering_loop.py (4), test_steer_endpoint.py
- [x] 4. Guardian approval mode: evaluate_policy first-arm handling (NOT the
      auto_writes else-branch), _drain_pending consult on "ask" (never for
      force_ask/exit_plan_mode), 5-approvals-per-turn cap on LoopState,
      fail-closed park; services/guardian.py — test_guardian.py (25),
      test_guardian_mode.py (6)
- [x] 5. Anthropic harness: services/harness/anthropic.py + config guard +
      HARNESSES entry + settings.default_model + model.py extract_memories
      degradation + anthropic dep — test_anthropic_harness.py (9)
- [x] 6. SKILL.md interop: services/skill_markdown.py parse/render (73 tests) +
      import/export endpoints + isolation entries — test_skill_interop.py
- [x] 7. Per-agent observability: GET /api/admin/agents scorecard (runs, tool
      calls, denials, mode-approved, screen flags, usage by agent_id) —
      test_admin_agents.py (fixed a select_from join bug it caught)
- [x] 8. Web: ask_user answer card + Answer button, guardian mode option
      (bypass-flagged), SteerStrip in all three chat panes, api-client
      steerRun + ApprovalMode widening
- [x] 9. Full gate + adversarial review: 25-agent review workflow returned
      19 confirmed findings (5 major) — ALL fixed and regression-tested:
      atomic event sequences (scalar subquery in append_event), guardian
      provenance flag (never softens a policy-row/org ask; defers on
      unabsorbed steers, reads absorbed ones), ask_user forged-answer strip,
      child screen-hit evidence carry-back (enforce + shadow), parallel-batch
      failure parity, steer auth tightening + single-txn idempotency +
      run_id-stamped Message, Inbox answer box + remember suppression,
      SteerStrip park gating + draft retention, proposal-note pre-wrap,
      export newline 500, honest effort ladder under anthropic.
      Final gate: ruff ✓ mypy ✓ full pytest exit 0 ✓ alembic up/down/up ✓
      vitest 612/612 ✓ eslint 0 errors ✓ next build ✓ e2e approvals 6/6 ✓;
      symbol-survival check vs 10 concurrent peer sessions ✓

- [x] 10. Browser proof (2026-08-23): e2e/harness-features.spec.ts — ask_user
      question card with options → typed answer round-trips through the
      amendment into the recorded result; delegate runs a real scripted child
      ("Research partner") and its words surface in the parent card; guardian
      mode selectable, bypass banner up, and fails closed to a human park on
      a scripted deployment (no reviewer). 4 script entries added to
      e2e/agent-script.json. Steer-strip visibility extracted to
      views/steer-format.ts (pure module, 7 tests) per repo convention.
      3/3 specs green first run; screenshots inspected by eye (options render
      as lines — pre-wrap fix visible; answer text in RESULT). Full e2e suite
      queued as the phase gate.

- Full-suite e2e gate (2026-08-23, harness session): 65 passed incl. the 3 new
  harness-features specs. 4 red in the full run: palette rename + workflows ×2
  PASS in isolation (ordering/load, not code); dashboards.spec beforeAll fails
  deterministically — the Sources-view CSV upload never becomes a dataset
  (spec:94), 5 dashboard tests skipped. That surface is the in-flight
  Knowledge cross-links work (uncommitted sources.tsx/use-workspace edits), so
  it was flagged to dashbored-2e rather than patched from here.

- [x] 11. Anthropic harness completed (2026-08-23): effort now maps onto the
      Messages API's OWN ladder (output_config.effort is literally
      low/medium/high/xhigh/max; "none" → thinking disabled; unset falls back
      to openai_reasoning_effort as the deployment reasoning default), and
      thinking/redacted_thinking blocks round-trip through LoopState history
      (_ThinkingItem — required for tool-use continuations with thinking on).
      Bootstrap's effort ladder restored for anthropic since every choice now
      does something. 12 harness tests green incl. 3 new; ruff+mypy clean;
      gap-study artifact updated with the shipped-status addendum.

# Harness open-list build (2026-08-23, "implement everything on the list")

The gap study's deliberately-open items. The org agent registry stays with the
marketplace epic (still plan-only, verified — no Listing code in tree); its
non-overlapping org piece, audit export, ships here instead.

- [x] A. Best-of-N: `delegate(attempts=N)` — N parallel children of one agent,
      all answers labelled back to the parent model to judge
- [x] B. Guardian under the Anthropic provider (anthropic_context_model,
      messages.create JSON verdict, billed provider=anthropic)
- [x] C. Audit export: GET /api/admin/audit-events/export — keyset cursor
      (created_at,id) ascending, since/action filters, no offset cap
- [x] D. Grain as an MCP server: workspace API tokens (model + 0048) +
      POST /api/mcp-server JSON-RPC endpoint (initialize/tools list+call,
      read-only registry only) + api-client + minimal UI if uncontested
- [x] E. Gates: ruff/mypy/full pytest/alembic round trip; web gates if touched

## Review (open-list build, 2026-08-25)

All five items (A best-of-N, B guardian-on-Anthropic, C audit export, D
Grain-as-MCP-server, E gates) landed on feat/agentic-workspace.

Adversarial review (a 3-agent lite pass, after the first heavier workflow
stalled on the loaded box) found 3 real defects, all fixed + regression-tested:
- MAJOR: a crafted JSON-RPC `id` of NaN/Infinity 500'd POST /api/mcp — json.loads
  accepts the token, then JSONResponse's allow_nan=False dump raises OUTSIDE the
  body-parse try. Fixed at the parse boundary (parse_constant raiser + scalar-id
  guard); test_a_non_finite_or_non_scalar_id_never_500s.
- MINOR x2: shadow-mode screen excerpts were dropped on a child ABORT (the
  except handler ignored shadow_hits) and could be clipped off the tail in
  best-of-N. Fixed by a shared `_notice_first` that LEADS the delegate result
  with the safety notice (survives clipping) and is used by both the answer and
  abort paths; test_shadow_hits_survive_a_child_abort_and_lead_the_content.

I also self-verified the three highest-risk surfaces before the review returned:
MCP offers ONLY read-only tools (empirical registry probe — zero write-capable),
the audit keyset drains 10 same-timestamp rows exactly once in id order, and the
JSON-RPC handler survives malformed bodies.

Gate: ruff ✓ mypy ✓ alembic 0048 up/down/up ✓ vitest 663/663 ✓ web lint 0
errors ✓ next build ✓ full pytest exit 0 except the one pre-existing
password-timing flake (a constant-time-ratio assertion, load-sensitive, passes
1/1 in isolation — not this build's code).

## Share the knowledge graph over MCP (2026-08-25)

- [x] `graph_export` tool in services/graph_tools.py: whole-graph snapshot
      (projection status/version/built_at, entities by mention_count, edges
      named by entity — never rebuild-volatile ids per ADR 0002 — with
      truncated flags; no provenance id lists, `graph_path` is the citation
      path). read_only=True, so the existing POST /api/mcp offer filter
      exposes it with zero MCP wiring.
- [x] Tests: 3 export tests in test_graph_depth.py + an end-to-end MCP test
      (`test_the_knowledge_graph_is_shared_over_mcp`) asserting graph_export/
      graph_neighbors/graph_path are offered and callable over the bearer
      channel. Updated test_walk_tools_are_read_only's exact-set assertion.
- [x] Gates: ruff ✓ mypy ✓ full pytest (worktree PYTHONPATH override —
      the repo venv's editable install otherwise imports the main checkout).

### Post-review fixes (QA session adversarial review, 2026-08-25)

- [x] HIGH: export payload outgrew bounded_content()'s 4000-char clip (caps
      were sized to GET /api/graph's HTTP ceiling, which never meets the
      clip) — cut mid-JSON with head-of-dict truncated:false surviving.
      Fixed with a budget refit before serializing (`_fit_within`; entities
      get first claim, edges the remainder, flags recomputed, orphaned
      edges dropped with their endpoints). Same refit applied to
      graph_neighbors, whose 50-row cap × long names had the same latent
      overflow. Regression tests round-trip oversized graphs through
      `bounded_content()` and json.loads the result.
- [x] MEDIUM: added the missing two-tenant graph_export case to
      test_tenant_isolation.py (bulk export is the worst tool to leave out
      of that checklist).
- [x] LOW: documented the one-call-bulk exposure of workspace-wide tokens
      and the workflow-scope ToolPolicy deny lever in mcp_server.py's
      docstring; edge ordering got an id tiebreak so equal-weight exports
      are stable across calls.
- [x] Re-verify round 2: my "graph_path is safe by arithmetic" claim was
      wrong at schema bounds (6 hops × long names × 9 provenance ids ≈
      6300 chars; ensure_ascii inflates non-ASCII names ~6x past 14k).
      graph_path now refits like the others — provenance sheds first, the
      chain's steps only after, `truncated` owns up to either loss, `hops`
      keeps naming the real path length. CJK long-name regression test.
      Declined the optional post-assembly assert: read paths must not
      crash, and the exact pricing is regression-tested on all three
      tools; the entities-clip-at-60% remainder is documented as a
      deliberate cosmetic trade in the ENTITY_BUDGET_SHARE comment.

# Agentic sandbox: install stuff, connect stuff, edit files (started 2026-08-22)

Goal: evolve the ADR-0005 execution sandbox from "run pre-baked Python" into a
Claude-Code-like session — the agent can install packages, hold user-provided
credentials, and read/write/edit files, all inside the existing approval and
egress machinery. Default egress stays `none`; nothing widens by default.

- [ ] A. Egress for installs: `services/sandbox/egress_proxy.py` — a filtering
      HTTP/HTTPS CONNECT proxy (stdlib only) enforcing the host allowlist and
      refusing ALWAYS_DENIED_CIDRS by resolved IP (connect to the resolved IP,
      no re-resolve, so DNS rebinding cannot help). Container driver gains real
      `allowlist`/`open`: per-session internal docker network + proxy sidecar
      (reuses grain-sandbox image + bind-mounted proxy script), sandbox joins
      the internal net with HTTP_PROXY/HTTPS_PROXY set; no route out except the
      proxy, so the CIDR denial is structural. `open` = proxy with any host but
      denied CIDRs still refused. Update session.tool_egress's container branch.
- [ ] B. Persistent installs on local drivers: PIP_TARGET/PYTHONPATH and
      npm_config_prefix pointed into the bind-mounted workspace so installs
      survive per-exec containers (e2b untouched — its venv already persists).
- [ ] C. File tools in `sandbox/tools.py`: sandbox_read_file + sandbox_list_files
      (read-only), sandbox_write_file + sandbox_edit (write; previews render
      unified diffs so ProposalDiff shows red/green in chat/Inbox for free).
- [ ] D. Connect stuff: `SandboxSecret` model (Fernet-encrypted values) +
      migration 0049_sandbox_secrets (renumbered from 0045 at merge onto main;
      down_revision 0048_api_tokens) + /api/sandbox/secrets
      (owner-write, member-read names-only) + isolation RouteCases + injection
      into SandboxSpec.env via ensure_session + local drivers persist spec.env
      per session (sibling file outside the mount) + approval preview names the
      secrets present (never values) beside the network line + minimal web UI
      card in Connections + api-client methods.
- [ ] E. Docs: amend ADR 0005 (egress-proxy section), .env.example knobs.
- [ ] F. Gate: ruff/mypy, full pytest, alembic up/down/up on scratch DB,
      tsc/pnpm test/build if web touched. Container e2e is NOT runnable here
      (colima daemon down) — argv construction + proxy protocol are unit-tested
      instead; live docker verification owed when the daemon is up.

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

⚠ Cross-branch migration coordination (convention agreed between the
marketplace, feature-sweep, and agentic-workspace sessions 2026-08-23):
0044_conversation_index is the last revision common to all branches, and
each in-flight branch keeps its own internal numbering while it moves —
this branch has 0045_marketplace → 0046_listing_installs, and the other
two branches use overlapping 0045+ numbers of their own. Resolution is
merge-order re-parenting, not slot reservation: whichever branch merges
second (and third) renumbers its whole chain onto the then-current head
and retargets the down_revisions. If this branch is not first in, re-parent
this branch's whole chain — 0045_marketplace → 0046_listing_installs →
0047_run_thinking — onto the merge-time head; do not pre-claim numbers.

# Steering + graceful incomplete streams (planned 2026-08-23)

The composer disables its textarea while a run is active; steering should
work in the same text box. Separately, `response.incomplete` (output-token
limit) raises "Model stream ended early", discarding everything streamed.

- [x] 1. Backend steer channel: POST /api/runs/{run_id}/steer (content,
      1..8000). Same auth as cancel (workspace + run_activity_visible),
      409 on TERMINAL_RUN_STATES / cancel_requested so the client can fall
      back to a fresh send. Writes a Message (run_id, role user) for the
      transcript and a `run.steer` RunEvent carrying the content — the
      event's per-run `sequence` is the consumption cursor.
- [x] 2. Loop absorption: LoopState gains `steered_sequence` (serialized,
      defaults 0 on old snapshots). `_absorb_steering` folds any newer
      run.steer events into `input_items` as plain user messages at the
      outer-loop checkpoint (after the cancel check, before the model
      call) — never inside _drain_pending, where a user item would split
      a function_call from its output. Parked runs absorb on resume.
- [x] 3. Incomplete streams: `stream_agent_response` records usage and
      yields ("incomplete", response) instead of raising; the loop
      finishes the turn with the streamed text plus a cut-short note
      (steering/"continue" now works for the rest), raising only when
      nothing at all streamed. response.failed still raises.
- [x] 4. Web: textarea always enabled (placeholder says it steers while a
      run is live); Enter/Send during a run calls api.steerRun and appends
      the message; Stop button stays; per-turn controls (skills, model)
      stay disabled — they configure the *next* turn.
- [x] 5. Tests: steer route (message+event, 409 terminal, isolation DENY),
      absorption unit tests via model_step fakes (pre-planted event lands
      in the first call's input; cursor is idempotent; park/resume keeps
      it), incomplete unit tests (partial text survives with note; empty
      still errors). Web: api-client steerRun contract test.
- [x] 6. Verify: ruff, mypy, full pytest, tsc, vitest, eslint, build, e2e.
      Note: the live composer removed the disabled-textarea sync the specs
      leaned on — every double-send spec now waits for Regenerate (turn
      settled) before its second send, or it would steer the closing run.

# Thinking trails (built 2026-08-25)

"Show thinking trails, as a setting that can be enabled." Design mirrors
the per-turn model/effort controls end to end:

- [x] 1. `Run.show_thinking` (migration 0047_run_thinking, guarded incl.
      no-runs-table case) — persisted per turn so a park/resume keeps the
      choice; `SendMessageRequest.thinking` (default False) rides the send.
- [x] 2. Harness protocol gains `thinking: bool`; OpenAI harness asks the
      provider for reasoning summaries (`reasoning.summary = "auto"`, only
      when on — the default request stays byte-identical) and yields
      ("thinking", text) events; scripted double accepts and ignores it.
- [x] 3. Loop streams the trail through a second DeltaBuffer as
      `thinking.delta` run events — its own lane, never part of the
      transcript or the answer.
- [x] 4. Web: Thinking toggle beside Fast in the composer (a *setting*:
      persists in localStorage under grain.thinking-trails); thread
      handler accumulates thinking.delta into a live collapsible
      "Thinking" panel above the run status, cleared when the run settles.
- [x] 5. Tests: thinking lane events land as thinking.delta with the
      answer untouched; the toggle rides the send onto the run row; both
      harness-forwarding spies extended to pin the new argument.

# The "flaky" e2e trio was two real product bugs (fixed 2026-08-25)

palette:56 + workflows:135/281 failed full runs but passed alone. Neither
was timing:

- [x] Thread rail: the open row renders four trailing actions (rename +
      share + split + delete) but `.thread` declared three `auto` columns,
      so the delete button grid-wrapped onto an implicit row OVER the next
      thread — unclickable whenever any thread sat below (i.e. exactly in
      crowded full runs). Fifth column added; thread-rail-css.test.ts pins
      column-count == action-count so the next added button can't repeat it.
- [x] Workflows list: a settled run triggers a background `load()`; a
      delete landing while that fetch is in flight was resurrected by the
      pre-delete snapshot resolving last, with the poll already torn down
      so nothing ever corrected it. `listEpoch` ref now invalidates
      in-flight snapshots on delete/save/status-change, and delete
      reconverges the waiting strip from fresh truth.
- [x] Verify: full playwright suite 72 passed / 0 failed (first fully
      green run; 3.0m, down from 7.4m of timeout stalls).
## QA fix pass (branch sweep-qa-fixes, 2026-08-25)

- Monitors duplicate-alert race closed at the database: migration
  0064_open_alert_unique adds a partial unique index (one OPEN monitor_alert
  row per monitor). RIDE-ALONG SEMANTIC, deliberate: an open alert nobody has
  acknowledged now suppresses re-alerting even after a genuine
  recover-and-recross — the monitor recovers to ok, crosses again, and the
  insert of the second alert loses to the still-open first one, landing as a
  skip. One un-acked Inbox card per monitor is the contract; ack (resolve) the
  alert to re-arm the page.
- F6 undo fixed (clobber guard from after-state snapshots; creation
  attribution from ToolResult.created_ids; per-row conditional-UPDATE
  consumption). NOTE, cosmetic (QA F6 LOW c): the undo's newest-first ordering
  tie-breaks on id.desc(), which is random-uuid order for two checkpoints in
  the same created_at tick — harmless today because same-tick rows come from
  one sequential run.
- F7 accepted designs, recorded per QA: monitor/digest claim commits ride the
  tick's shared session (any unrelated pending state commits along — the same
  accepted pattern as the monitor sweep), and resolve-re-arms means a
  sustained spike re-pages after each resolve; both deliberate.
- Assign races: FIXED (a) remove_member's release sweep can no longer be
  undone by an in-flight assign — the membership EXISTS now rides the assign's
  CAS WHERE, and (b) _claim_decision refuses assignee_gate for any model but
  AgentToolCall instead of silently gating on the wrong table. RECORDED, not
  fixed: assign's run-visibility check for the assignee stays pre-CAS, so a
  concurrent un-share can still route a park to a member who just lost sight
  of the thread — same accepted TOCTOU class as catalogued QA #9.
- RECORDED, revisit only if a surface appears: (1) mentions resolved before a
  comment delete keep their 10KB body snapshots forever (feed lists open-only,
  so invisible today) — do a redaction sweep if a history view ever shows
  resolved rows; (2) resolved alerts keep dangling monitor_id deep-links after
  monitor delete (cosmetic 404 on click).
- Full-stack audit (13 features, standing reachable-UI rule): PASSED. The one
  confirmed gap — Monitors UI could not edit an existing monitor's definition
  — is fixed (Edit affordance pre-filling the create form, changed-fields-only
  PUT; API-authored filter/grouping queries kept verbatim with scalar fields
  editable). F8's mail helpers remain backend by design, surfaced through the
  F10 subscription and F13 digest mails.
- packages/api-client/openapi.json on main was stale (predated the four-branch
  merge; missing 19 paths / 23 schemas) — regenerated on this branch.
- F9 share links (QA MEDIUM + LOWs): the create request and share modal now
  take an optional expiry (`expires_at`; modal offers never/1/7/30 days), so
  the leaked-link mitigation the schema advertised is actually mintable;
  `GET /shared/{token}` is rate limited per source address through the
  existing `auth_rate_limiter` (own `shared:` bucket, same knobs, same
  per-process caveat — a shared dashboard is a live DuckDB query per hit, and
  the budget also prices token guessing). AUTHZ DECISION, recorded in the
  router docstring: share-link authority stays FLAT — any member mints or
  revokes any of the workspace's links; minting mirrors edit rights, and
  revoke-open-to-all is the safety valve for a leaked link. Deliberately
  beside F10's owner-gate for third-party subscriptions (routing a
  colleague's attention needs the owner role; widening a resource the member
  already edits does not).
- F10 subscriptions (QA LOWs): `remove_member` now disables the departed
  member's subscriptions in the same transaction (audited as
  `subscriptions_disabled`) — no eternal skip-audit noise, no silent resume
  on re-invite; `deliver` audits honestly (`send_quietly` reports its
  outcome, a refused mail is a `subscription_skipped: delivery failed`, never
  a `subscription_sent`); dashboard names are collapsed to one line in the
  Subject header (CR/LF would make MIME assembly raise on every fire). SCALE
  NOTE (QA F10 #6, note only): `_firing_minute` walks up to 1441
  `cron_matches` per stale subscription per tick — fine at current fleet
  sizes; add a precomputed next-fire-at column if subscription counts grow.
- F8 mail helpers (QA LOW d/e): `render_link_button` now allowlists http/https
  and raises on any other scheme (javascript:, data:, relative) — both
  callers (subscriptions, digests) pass the server-built
  `settings.primary_web_origin`, so a raise is a programming error surfacing;
  text/HTML parity stays a caller obligation, now stated in the module
  docstring.
- F11 webhooks (QA MEDIUMs 3-4): deliveries are now signed Stripe-style —
  `X-Grain-Signature: t=<unix>,v1=HMAC-SHA256("t.body")` — so receivers can
  verify origin AND refuse replays (verification recipe in the
  services/webhooks docstring and the view copy); retry got a real horizon:
  MAX_ATTEMPTS 6 over an exponential `next_attempt_at` spread (1/5/15/60/240
  min ≈ 5.6h, migration 0065_delivery_hardening) plus an owner-gated
  Redeliver affordance on failed rows in the deliveries panel. LOWs
  RECORDED, not built: (QA 5) the caller-supplied signing secret has no min
  length and no rotation path (PUT lacks a secret field; delete+recreate
  loses the trail) — consider server-minted show-once secrets to match the
  ApiToken posture; (QA 6) no endpoint auto-disable after sustained failure
  and no webhook_deliveries retention — retention-sweep candidate; (QA 7)
  the tick's claim is global FIFO 25/tick with no per-workspace fairness.
- F12 inbound email (QA 8-12): per-address daily cap
  (services/inbound_email.DAILY_CAP = 200/UTC day; beyond it the same quiet
  200 as an unknown token, landing nothing, audited exactly once at the
  trip); message-id dedup now scoped per address (the address id salts the
  idempotency hash — pre-burning an id cannot suppress a sibling address's
  mail; keys recorded before this change are simply orphaned, worst case one
  historical mail could land again once). QA 9 (remote images / phishing
  links) VERIFIED CLOSED at the renderer: inbound mail lands as user-role
  messages and chat.tsx renders those as plain text, never markdown — no
  image loads, no clickable links; now annotated as a security boundary in
  chat.tsx and in strip_html's docstring (QA 12 — the "cannot re-become a
  tag" claim is honestly the renderer's guarantee, stated as such).
  Attachments-dropped note added to the Email-in panel copy (QA 10). NOT
  built: SPF/DKIM verdict surfacing (QA 8 half) — the generic provider
  payload carries no verdict field yet; add one when a concrete provider is
  wired.
- Full-gate rerun (2026-08-25, after the fix commits): lint/mypy/typecheck
  clean, full pytest green, vitest 749, build clean, e2e 76/1 skipped/0
  failed. The FIRST post-merge e2e run found two pieces of merge fallout
  (pre-existing on main at 462f03c, not from the fix commits), both fixed
  here: (a) the thread row's pinned four-column grid met a fifth action
  (mainline rename + sweep comments on one row) and Delete grid-wrapped onto
  the row below, eating clicks in three specs — .thread now auto-flows
  implicit columns and thread-rail-css.test.ts pins the mechanism, not a
  count; (b) navigation.spec's Library count kept the losing side's 12 past
  the Boards&todos fusion, and its Connections block never learned Sandbox
  secrets (now 11 and 5, entries named).
- Cross-cutting (QA 13, RECORDED): neither machine door (hooks trigger,
  inbound mail beyond the new per-address cap) is rate limited per
  credential — a leaked API token can queue workflow runs bounded only by
  spend ceilings. Fold into a shared door-throttle (the auth_rate_limiter
  pattern F9 reused) if abuse appears.
- F13 digests (QA 7-9): both mailers (digests, dashboard subscriptions) now
  require `User.status == "active"` on recipient resolution — deactivation
  keeps the membership rows, but a deactivated account no longer receives
  workspace-internal mail (regression-tested per mailer; the subscription
  fire audits it as the same honest skip a departed member gets). Digest
  delivery mirrors the F10 honesty branch: `digest.sent` is only recorded
  when `send_quietly` reports the sender accepted the message. CONTENT-BAR
  DECISION (QA F13 #8), recorded in the digests.py module docstring: digest
  mail is TITLES-ONLY — `Notification.body` quotes comment/message content
  and stays in-app behind the deep link; the render test now asserts the
  body never reaches either mail body. LOWs RECORDED, not built (QA F13 #9):
  (a) the digest claim is claim-before-send with no retry — an SMTP failure
  after the claim costs the member the whole day (asymmetric with webhook
  retries; a retry column would need to not re-render stale content); (b) a
  member whose digest_hour_utc is 23 gets no same-day recovery from ticker
  downtime — the period-start comparison only forgives lateness within the
  same UTC day; (c) use-workspace.ts's updateDigest captures `previous`
  inside the setDigest updater closure — an unmount mid-PUT can roll the
  local value back to null and rapid toggles can interleave echoes; it
  self-heals on the next bootstrap, so cosmetic. (d) UTC-only scheduling is
  honestly labeled in the UI — UX choice, no action.

## Merge notes (feature-sweep, 2026-08-23)

- MIGRATION RENUMBERING (QA finding #1): SETTLED at the four-branch merge
  (e6b01f6, gates fixed in 462f03c). Our 0045_templates…0055_digests became
  0053_templates…0063_digests on main, re-parented past mainline's
  0045_conversation_defaults…0048_api_tokens and marketplace's
  0050_marketplace/0051_listing_installs; single head confirmed. The QA fix
  pass added 0064_open_alert_unique on that head. Fixes now land via branch
  sweep-qa-fixes + PR onto main — no further renumbering expected.
- TWO TOKEN UIs (QA F11 LOW 2): SETTLED on sweep-qa-fixes — post-merge both
  mcp.tsx's token panel and webhooks.tsx's TokensSection manage the same
  api_tokens table; kept both on purpose (each page is where its audience
  already is) and cross-linked the copy in each. Fold into one shared
  component only if a third surface appears.
- SHARE-LINK AUTHZ (QA F9 LOW 2): decided and documented on sweep-qa-fixes —
  the flat model stands (any member mints/revokes any link; see the
  api/share_links.py router docstring for the grounds), intentionally beside
  F10's owner-gate for third-party subscriptions. Not a merge conflict, a
  recorded asymmetry.

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

# Harness parity run (planned 2026-08-25, awaiting sign-off)

Goal: encapsulate Claude-Code-class harness functionality (Claude Code /
Grok bot). Full plan + inventory baseline: tasks/harness-parity-plan.md;
presentation detail: tasks/visual-harness-plan.md.

- [ ] Phase 1: file & search surface — fs_glob/fs_grep/multi-read on the
      project VFS + the same typed fs_* tools targeting the sandbox FS
      (grep/edit a real tree, diff previews kept)
- [ ] Phase 2: web_fetch ToolSpec over the existing SSRF-hardened fetcher,
      injection-screened, default-ask
- [ ] Phase 3: execution — background run_command + task_output/task_kill,
      Node in the sandbox image, sandbox default-on in dev only
- [ ] Phase 4: orchestration — delegate(model?, effort?) validated above the
      model_step seam; parallel drain for all read-only tool batches
      (4.3 write-capable children: DECISION PENDING)
- [ ] Phase 5: session state — in-turn compaction (iterations 6 → ~24),
      digest-aware cross-turn context, checkpoint rows + revert endpoint
      (snapshot depth beyond documents: DECISION PENDING)
- [ ] Phase 6: extensibility — list_skills/use_skill tools + allowed-tools
      honored; sandbox-executed tighten-only hooks; per-agent
      model/effort/approval-mode
- [ ] Phase 7: presentation — typed event union, thinking/guardian/usage
      events, child-event envelopes, turn-tree reducer, shiki, per-tool
      renderers, activity timeline, pinned plan panel, nested delegate
      transcripts, usage meter (screenshot-review every visual change)
- [ ] Full gate per phase: make lint, pytest, pnpm test, pnpm build, e2e,
      alembic up/down base on scratch DB for schema phases
