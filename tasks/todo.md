# Agent Creator — custom agents (system prompt + provisioned tools) in chat and workflows

Plan: ~/.claude/plans/steady-shimmying-walrus.md (approved 2026-08-11)

## Todo

- [ ] Baseline: record pytest + vitest state (guards work is in-flight, uncommitted)
- [ ] Phase 1: migration 0031_agent_profiles + Agent model columns + schemas (AgentOut/Create/Update, ToolInfoOut)
- [ ] Phase 2: llm_tools registry_families + build_registry(allowed=); agent_loop AgentDirectives/resolve_directives wired at run_agent_turn/_continue/_advance
- [ ] Phase 3: app/api/agents.py CRUD (409 guards: last-enabled, delete-with-runs) + GET /api/tools + main.py registration
- [ ] Phase 4: NodeSpec.agent, validate_graph(agents=) + _check_agents, compiler/workflows-router threading, executor per-node agent resolution + _default_agent
- [ ] Backend tests: test_agents_api.py, test_agent_directives.py, tools endpoint, workflow compiler/executor cases
- [ ] Phase 5: api-client types + methods (+ WorkflowGraphNode.agent and missing .when)
- [ ] Phase 6: shared.ts View, navigation.ts entry, agents.tsx view, workspace.tsx block
- [ ] Phase 7: chat composer agent selector (use-workspace + handlers/chat + chat.tsx); workflow node agent picker (workflow-graph.tsx + workflows.tsx + workflow-format.ts)
- [ ] Gates: make lint, pytest, pnpm test, pnpm build, alembic upgrade x2 on scratch DB
- [ ] Boot make dev and verify at the seam (create agent → chat → workflow node → run)

## Review

(to be filled at completion)

---

# Chat approval modes + Todo lists (API only)

Migration landed as **0032_approval_modes_and_todo_items** (rebased onto the
Agent Creator's 0031 while this was in flight; single head confirmed).

## Todo

- [x] models: `Conversation.approval_mode`, `BoardCard.done_at`
- [x] alembic 0032_approval_modes_and_todo_items (down_revision 0031_agent_profiles)
- [x] agent_loop: `ApprovalMode`, `Verdict`, `evaluate_policy` (the decision) + `resolve_policy` (str face),
      `approval_mode_for_run` (returns ask_writes for anything but chat scope), `mode_decider`,
      `execute_agent_tool_call(decided_by=)`
- [x] chat.py: `PUT /api/conversations/{id}/approval-mode`, audited
- [x] schemas: ConversationOut.approval_mode, ApprovalModeRequest, BoardCardOut.done, Todo* models
- [x] services/artifacts/todos.py: the checkbox view over a one-column board
- [x] api/todos.py + main.py registration
- [x] artifacts/tools.py: `list_todos`, `add_todo`, `todo_check`
- [x] tests/test_approval_mode.py (20), tests/test_todos.py (16), 5 isolation route cases
- [x] Gates: ruff, mypy 122 files, pytest 1568 passed, export_openapi, alembic round trip

## Review

**Approval modes.** One decision point still: `evaluate_policy` holds the whole
rule and `resolve_policy` is its string-returning face, so `services/workflows/
executor.py` was not touched. The mode is applied last, on top of the scope
resolution, under two locks — it is ignored unless the scope is `chat`, and a
`deny` survives every mode. `AgentToolCall.decided_by` is stamped `mode:auto_writes`
only where the mode *changed* the answer, so a tool a policy row already allowed
is not falsely attributed to the bypass either.

**Todo lists.** A view over boards, as argued for: a list is a board with exactly
one column, derived rather than stored. Only three routes do anything the board
routes could not — `GET /api/todos`, `POST /api/todos`, and
`PATCH /api/todos/items/{id}`, which ticks an item off with no board id at all.
Three agent tools (`list_todos`, `add_todo`, `todo_check`) for the same reason.

**Verification.** Ten deliberate mutations, each caught by exactly the intended
test: the workflow-scope lock (twice, separately, plus the composed case that
needs both broken), the deny survival, the over-attribution of a standing allow,
the `done_at` rewrite, and three dropped workspace filters. Migration upgraded,
downgraded, re-upgraded and re-run on a scratch DB.

**Known, not mine:** `test_route_table_matches_the_app` fails on the four
`/api/agents` routes the Agent Creator work added and has not yet given isolation
cases. Every route in this change has one.

## Document chat panel + inline per-hunk review (frontend)

- [ ] `createThreadHandlers` extracted from `handlers/chat.ts`; ~8 deps collapse into `onRunSettled`
- [ ] `useDocumentThread` hook: get-or-create the document's thread, own run/message state
- [ ] Compact `ChatView` panel beside the document (reused, not reimplemented)
- [ ] Inline per-hunk Accept/Reject over the document body, staged into ONE `accepted_hunks` decision
- [ ] api-client: `segments` on `PendingDocumentEdit`, `documentConversation`, decision amendment
- [ ] Playwright: kinds render per their promise, side-chat edit arrives, 1-of-2 hunks applied

## Todo lists and approval modes — UI phase (plan)

Built on the API phase (routes, tools, `Conversation.approval_mode`, `board_cards.done_at`).

### Todo lists
- [ ] `views/todo-format.ts` — a list is a board with one column (the server's own rule,
      stated once client-side); `listForTodoCall` resolves a call's list the way
      `todos.resolve_list` does. Unit tested.
- [ ] `views/todos.tsx` — `TodoChecklist` (one list as checkboxes) + `TodosView` (the page).
      One component, two mounts: the Lists tab and the chat embed.
- [ ] `handlers/todos.ts` — create/add/tick/delete over the existing `boards` state, since a
      list *is* a board and `GET /api/boards` already carries `done`.
- [ ] Nav: `todos` view, "Lists" tab in the Files group (not "Boards" — anchored names).
- [ ] Chat: a checklist under any `add_todo`/`todo_check` card, checkable in place.

### Approval modes
- [ ] API: `AgentToolCallOut.approved_by_mode` — the *mode* that decided a call, never the
      user id (AuditEventOut deliberately exposes no actor). Derived from `decided_by`.
- [ ] `views/approval-format.ts` — the three modes' labels/descriptions; unit tested.
- [ ] `views/approval-mode.tsx` — picker (disclosure-menu) + the bypass indicator: persistent,
      in the composer zone so scroll cannot hide it, naming the thread, listing what it has
      auto-approved, with a one-click way off.
- [ ] Tool cards: an "Auto-approved" badge where the mode decided; `denied` reads as denied.
- [ ] Playwright: a write parks under ask_writes and does not under auto_writes; the indicator
      is up the whole time; a denied tool stays denied under bypass; a tick survives reload.

## Reconciliation and verification pass (integration)

Appended, not overwritten — the plans above belong to the tracks that wrote them.

This pass built no features. It reconciled three tracks that landed in one tree,
verified every gate personally, and fixed what verification found.

### Fixed

- [x] **`alembic downgrade base` aborted at 0020.** `0001_initial` builds the schema
      with `Base.metadata.create_all()`, so a from-empty database already holds every
      current column by revision 0001 and 0020's guarded upgrade adds neither
      `workflows.last_dispatched_at` nor `ix_workflows_schedule` — while its downgrade
      dropped both unconditionally. The downgrade now mirrors the upgrade's guard.
      Round trip `empty → head → base → head` passes; single head `0032`.
- [x] **Four `/api/agents` routes had no tenant-isolation case**, failing
      `test_route_table_matches_the_app`. Added `GET`/`POST /api/agents` (scoped) and
      `PATCH`/`DELETE /api/agents/{agent_id}` (deny, 404 not 409). Verified by removing
      the workspace filter from `_load`: exactly the two deny cases fail.
- [x] **`dashboards.spec.ts` leaked its CSV into every later spec.**
      `DELETE /api/sources/{id}` is behind an idempotency key its neighbours do not
      need; the cleanup sent none, the helper reported the rejection to nobody, and
      `workspace.spec.ts`'s `getByTitle("Delete source")` then matched two buttons.
      Key added, cleanup now asserts the source is gone, and the loose locator is
      anchored to its own filename. Verified by re-injecting the leak: dashboards'
      own cleanup fails, and no innocent spec does.
- [x] **`workspace.spec.ts` asserted the pre-redesign approval markup.** The Files
      view now renders `DocumentReview` (per-hunk accept/reject) instead of
      `.document-pending .tool-card`. Test updated to the new UI and screenshotted.
- [x] **Three composer placeholders were blanked** when `aria-label`s were added
      (chat, dashboard editor, workflow composer). The accessible names were the
      right change; deleting the visible hint was not. All three restored, both
      properties kept. The workflow one was the only example of a workflow
      description anywhere in the product.
- [x] Docs: `docs/ARCHITECTURE.md` (rail now lists Lists; new sections for approval
      modes, authored agents, todo lists, and what the migration chain does *not*
      prove), `README.md` (rail + three new capabilities).

### Not done, deliberately

- The `0001_initial` `create_all()` shortcut is left in place. Rewriting it into
  real DDL would make the chain genuinely testable from empty, but it rewrites
  committed migration history for every existing deployment. Documented instead.
- `DashboardGrid.release()`/`nudge()` still call `commit()` from inside a
  `setTiles` updater — an impure updater, double-invoked under StrictMode.
  Harmless today because `commit` is debounced. Flagged by the dashboards track,
  still unfixed, still not mine to change blind.
Branch: feat/agent-creator (worktree). Contracts + workflows/agents-router landed on
feat/agentic-workspace as 24ab394 + 71941ee; everything since lives here.

## Todo

- [x] Baseline recorded; coordinated shared-tree split with peer sessions, then moved to this worktree
- [x] Phase 1: migration 0031_agent_profiles + Agent model columns + schemas (committed 24ab394)
- [x] Phase 2: llm_tools registry_families + build_registry(allowed=) (24ab394); agent_loop AgentDirectives/resolve_directives wired at run_agent_turn/_continue/_advance (worktree)
- [x] Phase 3: app/api/agents.py CRUD + 409 guards (71941ee); GET /api/tools; main.py registration
- [x] Phase 4: NodeSpec.agent, validate_graph(agents=) + _check_agents, compiler/router threading, executor per-node agent + _default_agent (71941ee)
- [x] Backend tests: test_agents_api.py, test_agent_directives.py, workflow compiler/executor cases, tenant-isolation route cases + db.get review
- [x] Phase 5: api-client AgentInfo/ToolInfo types + methods; WorkflowGraphNode.agent (`when` already existed)
- [x] Phase 6: shared.ts View, navigation.ts Chat-group entry, agents.tsx view, workspace.tsx block, CSS
- [x] Phase 7: chat composer AgentSelect (+ reset on "Agent is not available"); workflow node "Runs as" picker via updateWorkflow({graph})
- [x] Gates: ruff, mypy, tsc, eslint, vitest (302), pnpm build, alembic 0031 upgrade x2 on scratch DB, openapi.json regenerated
- [x] Full pytest re-run after final edits: 1558 passed, 1 skipped, 3 xfailed
- [x] Boot dev stack (worktree ports 8020/3020) and verify at the seam
- [x] Commit worktree work on feat/agent-creator (b3e9161)
- [ ] Merge feat/agent-creator into feat/agentic-workspace once the fieldnote session's
      approval-modes work lands (expected conflicts: models.py, schemas.py, main.py,
      agent_loop.py, api-client index.ts — all additive on both sides; alembic must end
      0031_agent_profiles → 0032_approval_modes with one head)

## Review

Landed across three commits: 24ab394 (contracts: Agent columns, migration 0031,
registry_families + build_registry(allowed=)), 71941ee (workflow NodeSpec.agent +
validate/executor + /api/agents router), b3e9161 (loop directives, GET /api/tools,
api-client, Agents view, chat selector, workflow Runs-as picker, tests).

Verified end to end against a real model (gpt-5.5): an authored "Haiku Bot" with
instructions + allowed_tools=["search_sources"] answered chat in haiku with run.agent_id
set; a workflow agent node assigned to it succeeded with arguments {"agent": "Haiku Bot"}
and a haiku output; a graph naming a bogus agent id was refused at save with
agent_unknown; screenshots confirm the Agents view, the composer select (Default /
Research partner / Haiku Bot), and the canvas "Runs as" picker.

Design notes for future sessions: allowed_tools_json "" = all tools, "[]" = none (repo
"" = unset convention); the subset is a pure intersection under ToolPolicy, applied at
build_registry; resolve_directives ignores Agent.enabled so parked runs resume with the
directives they started with; per-node agent is persisted onto the backing run before
each turn (that is what makes park/resume agree); the compiler never emits agent ids.
Deliberate v1 exclusions: per-agent model override, per-conversation sticky selection.

Multi-session coordination: this session shared the tree with two others; the split was
negotiated by message, contracts were committed early to make clobbers recoverable, and
the tree moved to per-session worktrees mid-build (this one: Dashbored-agent-creator).

## Knowledge graph empty for a memory-only workspace (2026-08-12)

Reported as "rebuild_graph never reads MemoryItem". That is not the defect — the
projection has read MemoryItem since 88f0588 (entity_memories / memory_ids_json /
"from memory" in the entity row). Verified against a copy of data/workspace.db:
rebuilding Lyn's workspace (14 memories, 0 sources) yields 25 entities / 48 edges.

Real defect: nothing ever triggers a rebuild for a workspace with no sources.
`mark_graph_stale` is called on every memory write, and *nothing consumes* the
"stale" status. `rebuild_graph` runs only from source ingest, source delete, and
POST /api/graph/rebuild. Lyn's projection: status=stale, built_at=NULL, never built.

- [ ] Graph page acts on `stale`: rebuild once on open (not on GET, which
      refreshSecondary calls after every chat turn — that would be a full LLM
      rebuild per turn)
- [ ] Empty-state copy stops saying sources are the only input
- [ ] rebuild_graph's memory liveness goes through memory._active()
- [ ] Tests: memory-only graph is non-empty; superseded contributes nothing
      (mutation-checked); idempotent; cross-tenant isolation; chokepoint assertion

### Review

The reported diagnosis did not hold. `rebuild_graph` has projected memories since
88f0588: it reads `MemoryItem`, keeps `entity_memories` / `relation_memories`,
writes `memory_ids_json` on both nodes and edges, folds curated `entity_names`
through the same article-alias pass as chunk names, and the entity row already
renders "· from memory". Proved on a copy of data/workspace.db: Lyn's workspace
(14 memories, 0 sources) rebuilds to 25 entities / 48 edges, every one of them
memory-cited and none chunk-cited, and a second rebuild reproduces the version
hash exactly.

What was actually broken is the trigger. `mark_graph_stale` runs on every memory
write and *nothing consumed the status it wrote* — `rebuild_graph` ran only from
source ingest, source delete, and the page's own button. Lyn's projection was
`status=stale, built_at=NULL`: never built, and nothing in the product would ever
have built it.

- Graph page rebuilds a `stale` projection on open. Not on GET /api/graph:
  `refreshSecondary` re-reads the graph after every chat turn, so read-repair
  there would spend a full rebuild (up to 60 extraction calls) per turn to serve
  a page nobody opened. This view mounts only when its tab is open.
- `stale` now reads as "about to build" in the empty state but *not* on the
  button, so a failed auto-rebuild leaves the manual retry reachable.
- `rebuild_graph` takes memory liveness from `memory._active()` (function-level
  import; memory.py imports graph.py, so the cycle closes at call time only).
- Six API tests + five web tests. Mutation-checked: replacing `_active(...)` with
  a bare workspace filter fails `test_a_superseded_memory_contributes_nothing`
  (both the retired *and* the forgotten name reappear as nodes) and
  `test_graph_takes_memory_liveness_from_memorys_own_chokepoint`; restored, green.

Deferred: `api/memory.py:45` still spells out `status == "active"` for the list
endpoint, so `_active()` is the chokepoint for recall and the projection but not
literally every reader. Out of scope here and separately tested.

Not run: Playwright (ports coordinated with another session). The graph page's
stale-open path is covered by vitest against the real component, not e2e.
