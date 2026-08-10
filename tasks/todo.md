# Plan: frontend fix → memory → integrations → sandbox

Full plan: ~/.claude/plans/first-fix-frontend-and-keen-wadler.md

## Phase 1 — Frontend fix / DX ✅
- [x] scripts/dev.sh + `make dev` + README quickstart
- [x] api-client `health()` + API-unreachable banner in Workspace
- [x] Fix tool-approval resume (conversation_id on ToolCallOut end-to-end)
- [x] CSP localhost/127.0.0.1 alignment (next.config.ts + published page default)
- [x] Extract snapshot-renderer.tsx, wire previewApp() "Preview" drawer
- [x] Verify: ruff/mypy/pytest ✓, tsc/eslint/vitest ✓, build ✓, dev.sh smoke ✓, e2e 2/2 ✓

## Phase 2 — Graph long-term memory ✅
- [x] memory_items model + 0003 migration (+ graph provenance columns) — verified on scratch DB
- [x] services/embeddings.py (OpenAI or offline None)
- [x] services/memory.py: write path (extraction, summaries, forget-tombstones) + recall
- [x] Inject transcript+memory+graph digest into model call; memory.recalled event
- [x] Graph rebuild consumes memory items (memory_ids provenance on entities/edges)
- [x] api/memory.py (list/forget) + Memory panel in Graph view + recall status in chat
- [x] tests/test_memory.py (5 tests) — full suite 24/24, e2e 2/2

## Phase 3 — Tool-use loop + integrations ✅
- [x] llm_tools registry (search_sources/list_datasets/query_dataset/graph_lookup/recall_memory)
- [x] agent_loop.py (Responses function calling, 6-iter cap, injectable model_step) + agent_tool_calls
- [x] tests/test_agent_loop.py (3 tests, scripted fake model)
- [x] OAuth models + 0004 migration + Fernet token encryption (cryptography dep)
- [x] api/integrations.py (connect/callback/sync/disconnect/jobs, owner-gated)
- [x] connectors: gmail.py + strava.py → Sources/Chunks/Datasets via existing ingestion
- [x] integration LLM tools (gmail_search/get_message, strava_list/get_activity)
- [x] Integrations view + client methods; .env.example documented; Garmin flagged (Strava-first)
- [x] tests/test_integrations.py (5 tests, MockTransport) — suite 32/32, e2e 2/2

## Phase 4 — Sandboxed vibecoding ✅
- [x] Code-app manifest v2 (kind:"code", html, data_bindings, baked snapshots) + 0005 migration
- [x] Frame routes (preview + published) with default-src 'none' / connect-src 'none' CSP
- [x] sandbox-frame.tsx postMessage host → binding-validated queryDataset; published-code-frame.tsx
- [x] app_codegen.py (LLM via generate_code + deterministic template fallback, lint, 256KB cap)
- [x] POST /api/apps/{id}/generate; App Studio drawer (prompt → generate → live preview → publish)
- [x] THREAT_MODEL.md updated + docs/adr/0004-sandboxed-generated-apps.md
- [x] tests/test_app_codegen.py (7 tests) — suite 40/40, e2e 2/2, build ✓

## Bonus (user request mid-build)
- [x] Garmin via python-garminconnect (0.2.8, py3.9-compatible): credentials endpoint
      (password never stored; encrypted garth session token), sync → garmin-activities dataset,
      garmin_list_activities LLM tool, Integrations-view credential form, MFA rejected cleanly.

## Phase 5 — NLP dashboards + frontend cleanup ✅
Full plan: ~/.claude/plans/squishy-sprouting-cherny.md
- [x] Merge Dashboards + Apps into one Dashboards nav entry (`View` loses analytics/apps)
- [x] Delete AnalyticsView / DashboardResultView / AppsView / AppStudio + their state
- [x] DashboardsView: grid of live sandboxed tiles, publish / Open / versions+rollback
- [x] DashboardEditor: chat left, live preview right; transcript rebuilt from release prompts
- [x] Auto-create a dataset when a CSV/JSON source reaches ready (replaces the builder form)
- [x] Remove AI slop: fake Connected pill, ⌘K badge, starter prompts, trust badges,
      GET pill + safety note, sandbox blurbs, page sub-blurbs, "projection state"
- [x] api-client: fetch failures become ApiError(status 0) — no more raw "Failed to fetch";
      workspace.tsx routes every catch through describeError() so offline shows only the banner
- [x] Fixed a real handshake race the new e2e caught (see lessons.md)
- [x] Verify: make lint ✓, pytest 40 ✓, vitest 4 ✓, pnpm build ✓, e2e 2/2 ✓

## Review
- All four phases + Garmin landed. Verification at each phase: ruff, mypy, pytest
  (40 tests), tsc, eslint, vitest (3), `pnpm build`, Playwright e2e (2), migration
  chain 0001→0005 on a scratch DB, and a live smoke run (API+web boot, chat
  roundtrip against real OpenAI: agent loop answered, memory extracted with
  entities + provenance).
- Frontend "won't build/run" did not reproduce anywhere; root cause judged to be
  startup ergonomics — fixed with `make dev` (preflight, health wait, prefixed
  logs) and an API-unreachable banner with auto-recovery.
- Found and fixed a real regression during e2e: uploadFiles() called
  setView("sources") after its awaits, clobbering navigation (see lessons.md).
- Not done (follow-ups): write-capable agent tools behind the approval/resume
  hook; Calendar/Drive connectors; FIT/TCX upload parsing; sqlite-vec/pgvector
  embedding backend; e2e coverage for the App Studio flow; workspace.tsx is now
  ~2600 lines and due for the view-file split.

---

# Plan: agentic chat app (MCP + DB connectors + sandboxes + documents)

Full plan: ~/.claude/plans/ticklish-frolicking-stream.md

Decisions: browser-only sandboxes (no server exec) · keep multi-view nav, deepen
chat · OpenAI only · chat spine first.

## M1 — Chat spine ✅
- [x] Migration 0006: `runs.agent_state_json`, `agent_tool_calls.call_id/decided_by/
      decided_at`, `tool_policies` table — 0001→0006 verified on a scratch DB
- [x] Real token streaming: `stream_agent_response()` over the Responses API;
      `ModelStep` widened to yield ("delta", text) then ("completed", response)
- [x] `DeltaBuffer` batches deltas into `message.delta` events (48 chars / 100ms)
      instead of one row per token; SSE transport unchanged
- [x] Agent loop is now a resumable state machine: `LoopState` (input_items,
      pending_calls, iteration, text_so_far, evidence) persisted on pause
- [x] Per-tool policy `ask|allow|deny` from `ToolSpec.read_only` + workspace
      overrides; parking emits `tool.proposed` + `run.waiting_for_approval`
- [x] `POST /api/agent-tool-calls/{id}/decision` (+`remember`), `GET/PUT
      /api/tool-policies`, `GET /api/agent-tool-calls`; `resume_run()` continues
      the turn — denials resume too, so the model answers around the refusal
- [x] Cancel is honoured between iterations and between tool calls, not just
      during the fake word stream
- [x] Extracted `components/views/chat.tsx`: markdown with per-code-block copy,
      inline tool-call cards (args/result/timing), inline approve/deny with
      "always allow", Stop wired to cancelRun, Regenerate
- [x] Fixed conftest resolving the test DB against the cwd while settings anchor
      to the repo root — a stale schema had been surviving between runs
- [x] Verify: make lint ✓, pytest 52 ✓, vitest 4 ✓, pnpm build ✓, e2e 2/2 ✓,
      alembic 0001→0006 on scratch DB ✓, live OpenAI smoke ✓ (deltas arrive,
      function calls land in .output, loop state is JSON-safe)

## M1 follow-up
- [ ] Finish splitting `workspace.tsx` (~2000 lines): sources/graph/dashboards/
      integrations/activity into `components/views/*` + a `use-workspace` hook.
      Only chat was extracted.
- [ ] e2e coverage for the approval card (current e2e runs the deterministic
      provider, which never enters the agent loop)

## Python 3.10+ bump ✅
- [x] `requires-python = ">=3.10"`, ruff `py310`, mypy `3.10`; README + Makefile
      (`PYTHON ?= python3.12`, overridable) — local venv rebuilt on 3.12.11 to
      match what CI already used
- [x] New py310 lint findings resolved deliberately: `zip(..., strict=True)`
      where lengths are provably equal (analytics x2, embeddings), `strict=False`
      in memory.py where the vector count comes from an external API
- [x] Re-verified on 3.12: make lint ✓, pytest 52 ✓, eval 100%/100% ✓,
      build ✓, e2e 2/2 ✓, alembic 0001→0006 ✓, live OpenAI smoke ✓
- [ ] `datetime.utcnow()` is deprecated on 3.12 (~1100 warnings/run) — needs a
      deliberate naive-vs-aware decision, not a find-and-replace

## M2 — MCP connectors ✅
- [x] Python bumped to >=3.10, so the official `mcp` SDK (1.29) is used
- [x] Migration 0007: `mcp_servers`, `mcp_tools`; env/headers Fernet-encrypted
      through the existing `services/crypto.py`, never returned by the API
- [x] `services/mcp/client.py`: stdio + streamable HTTP, per-call timeout and an
      8KB result cap. The SDK is async and this API is sync, so every operation
      runs to completion on a private loop in a private thread — `asyncio.run()`
      alone would refuse to run inside the event loop.
- [x] One connection per operation (connect → initialize → call → close). A
      pooled session would avoid respawning stdio servers per call but must
      outlive the request on a background loop; revisit if latency bites.
- [x] Discovery is cached in `mcp_tools` and read from the DB when building the
      registry — the agent loop is sync and runs on every turn
- [x] `mcp__<server>__<tool>` injection with `read_only=False`, so MCP tools
      default to the M1 approval prompt; a ToolPolicy row can promote to allow
- [x] `/api/mcp/servers` CRUD + refresh + per-tool enable; unreachable servers
      return status "error" with the reason instead of a 500
- [x] MCP view: add stdio/HTTP server, refresh tools, enable/disable server and
      individual tools, delete
- [x] 10 tests against a real stdio MCP server subprocess (`tests/fixtures/
      echo_mcp_server.py`): discovery, round-trip args, env secrets reaching the
      process, tool errors as text, truncation, unreachable binary, timeout,
      namespacing, registry integration, error recording
- [x] Verify: make lint ✓, pytest 62 ✓, vitest 4 ✓, build ✓, e2e 2/2 ✓,
      alembic 0001→0007 ✓, OpenAPI contract regenerated for CI drift check

## M2 follow-up
- [ ] Remote MCP OAuth (the SDK supports it; today only static headers)
- [ ] Consider a pooled/persistent session if per-call stdio spawn latency shows
- [ ] MCP prompts and resources — only tools are surfaced today

## M3 + M4 — built in parallel via a workflow (in progress)
Contracts landed first so parallel agents could not clobber each other: models
(`DbConnection`, `Project`, `ProjectFile`) and migration 0009 are mine; each agent
track owns only its own new files and reports the wiring it needs.

## M3 + M4 — built by a 6-agent workflow ✅
Contracts (models + migration 0009) landed serially first so parallel agents
could not clobber each other; each track owned only its own new files and
reported wiring back for the orchestrator to apply.

### M3 — Database connectors ✅
- [x] `services/dbconnect/` — engines for postgres/mysql/sqlite/duckdb, cached
      with pool_pre_ping, schema introspection, guarded query execution
- [x] Read-only in depth: single-statement check with comments stripped, forced
      LIMIT (wrapped as a subquery), rolled-back read-only transaction,
      statement timeout, row + byte caps
- [x] Tools: list_connections, describe_schema, sql_query (auto), sql_execute
      (ask + preview, refused unless the connection is marked writable)
- [x] `/api/db/connections` CRUD + test + schema; DSN encrypted, never returned
      (only a redacted summary), driver extras added for mysql/duckdb
- [x] Data view: connection cards, add form, test, schema browser

### M4 — Project sandboxes (browser-only) ✅
- [x] `services/projects/` — virtual FS with path normalization/validation and
      256KB-per-file / 5MB-per-project / 200-file limits
- [x] Tools: list_projects, fs_list, fs_read (auto); create_project, fs_write,
      fs_edit, fs_delete (ask + diff previews). fs_edit reuses
      `artifacts.documents.render_diff`, so file edits and document edits show
      the identical inline diff on the approval card.
- [x] `/api/projects` CRUD; esbuild-wasm bundles in the browser from a local
      asset (no CDN), rendered in the existing `default-src 'none'` iframe
- [x] Projects view: file tree, editor, live preview
- [x] Host CSP gains `'wasm-unsafe-eval'` — WebAssembly compilation only, host
      page only; the sandbox frame's own CSP is unchanged

### What the adversarial reviewers caught (all fixed)
- **critical** dollar-quoted strings (`$q$…$q$`) masked a stacked `DELETE` past
  all four SQL defence layers — proven to permanently delete rows on real DuckDB
- **critical** the projects router was never mounted (every endpoint 404) and the
  seven project tools were never merged into `build_registry`, so the model was
  never offered them
- **major** byte cap exempted the first row: a single 4MB cell returned in full
- **major** bundler used plain objects for shim/loader lookup, so `import
  "toString"` hit Object.prototype and crashed esbuild internally
- **major** sandbox assets were generated by a script nothing ran, so previews
  were dead on a fresh checkout
- **major** `create_project` crashed on `files: ["path.tsx"]`, and the preview
  crash rendered a *blank* approval card for a write tool

### What I found afterwards that the reviewers missed
- **MySQL executable comments**: `/*! … */` is executed by MySQL, not ignored,
  and the guard stripped it as an ordinary comment — `SELECT 1 /*! ; DROP TABLE
  t */` reached a mysql connection intact. Now refused on the raw text before
  masking; ordinary `/* … */` still works.
- **Absolute file DSNs silently became relative**: `sqlite:///` + `/tmp/a.db`
  gives three slashes, which SQLAlchemy reads as the *relative* `tmp/a.db`, so a
  connection opened or created the wrong file with no error. Same class as the
  repo-root lesson already in lessons.md.

- [x] Verify: make lint ✓, pytest 194 ✓, vitest 9 ✓, build ✓, e2e 2/2 ✓,
      alembic 0001→0009 ✓, OpenAPI regenerated, plus my own probes of the SQL
      guard, the tool registry, and a real sqlite round trip

## M3 follow-up (superseded)
- [ ] Migration 0008: `db_connections` (engine, encrypted DSN, read_only)
- [ ] `services/dbconnect.py`: cached engines, `sqlalchemy.inspect` introspection
- [ ] Read-only defence in depth: reject non-SELECT, read-only transaction,
      statement timeout, forced LIMIT, row/byte caps
- [ ] Tools: `list_connections`, `describe_schema`, `sql_query`
- [ ] Data view: connections CRUD, test, schema browser

## M4 — Multi-file project sandboxes (browser-only)
- [ ] Migration 0009: `projects`, `project_files` (256KB/file, 5MB/project, 200 files)
- [ ] `fs_list` / `fs_read` / `fs_write` / `fs_edit` / `fs_delete`, writes on `ask`
- [ ] esbuild-wasm bundling in the host page (adds `'wasm-unsafe-eval'` to the
      host script-src; the frame's `default-src 'none'` is unchanged)
- [ ] Manifest schema_version 3 `kind:"project"` reusing app releases/rollback
- [ ] ADR + THREAT_MODEL update

## M-features — everything in features.txt, agentic ✅
The unifying idea: "inline writing diff" is not a separate feature, it is how
every agentic write is presented. So `ToolSpec` gained an optional `preview`,
the M1 approval card renders it, and documents + boards are just artifacts the
agent edits through that path.

- [x] `ToolSpec.preview(db, context, args) -> str` — renders what a call *would*
      do, computed at park time and stored on `agent_tool_calls.proposal_preview`
      (migration 0008). Preview failures are swallowed: a missing courtesy must
      not block an approval the user is waiting on.
- [x] Documents: markdown + LaTeX, `documents` / `document_versions`, a snapshot
      before every edit so any agent change is undoable
- [x] `edit_document` is an exact-string replace that refuses ambiguity — a
      `find` matching twice errors unless `replace_all` is set, the same contract
      a code editor's patch tool uses
- [x] Documents and boards are addressable by title/name, not just id, because
      that is what a model reliably remembers across turns; a lone board needs
      no name at all
- [x] Kanban: `boards` / `board_columns` / `board_cards`, defaulting to
      Todo / In progress / Done; tools for create, add, move, update, delete
- [x] All 7 write tools are `read_only=False` (so they prompt) and all carry a
      preview: a unified diff for documents, a sentence for board operations
- [x] Chat renders the diff inline on the approval card with +/- colouring;
      non-diff previews fall through as plain text
- [x] Documents view: source pane + live preview, KaTeX math via remark-math /
      rehype-katex bundled locally so the CSP needs no change. Version history
      with restore. Board view: drag-and-drop kanban.
- [x] The editor re-syncs after a run, since the agent edits underneath the user
- [x] 17 tests, including that previewing does not mutate and that a denied edit
      leaves the document untouched
- [x] Verify: make lint ✓, pytest 79 ✓, vitest 4 ✓, build ✓, e2e 2/2 ✓,
      alembic 0001→0008 ✓, OpenAPI regenerated, live model smoke ✓ (a real model
      picked edit_document, gave enough context to disambiguate `find`, parked
      with a correct diff, and applied only after approval)

### Scope call on LaTeX
Documents render markdown + LaTeX *math* (`$…$`, `$$…$$`) through KaTeX. A
`latex` document is prose with math, not a TeX compilation target —
`\documentclass` renders verbatim. Full TeX → PDF needs an engine (texlive or a
multi-MB wasm build) and does not fit the browser-only decision cheaply.

## M-features follow-up
- [ ] Full LaTeX → PDF, if wanted (needs a TeX engine; see the scope call above)
- [ ] Board column add/rename/reorder — columns are fixed at creation today
- [ ] Card ordering within a column (position exists; no UI to reorder)
- [ ] e2e for the diff-approval flow (e2e runs the deterministic provider)

## M5 (superseded by M-features)
- [x] Migration: `documents`, `document_versions`
- [ ] Tools: `create_document`, `read_document`, `edit_document` (exact-string
      replace so approval cards can render a diff), `list_documents`
- [ ] Documents view: editor + live preview; markdown via react-markdown, math
      via KaTeX + remark-math/rehype-katex bundled locally (no CSP change)
- [ ] Decide scope: markdown + LaTeX math (default) vs full `\documentclass` →
      PDF, which needs a TeX engine and does not fit browser-only cheaply


---

# Overleaf-style LaTeX + kanban depth + inline doc diffs ✅
Built by an 8-agent workflow (research → 3 build tracks → adversarial verify each
→ completeness critic), then integrated serially.

## The research phase earned its place
The load-bearing unknown was whether ANY wasm TeX engine compiles offline. Most
fetch .sty/.cls from a remote server mid-compile, which would break the
browser-only decision. Result: **wasmtex 0.1.1** (TeX Live 2026 pdfTeX/XeTeX via
busytex), the only candidate that compiled `\documentclass{article}` + amsmath +
bibtex to a real PDF in headless Chromium under this repo's exact production CSP
with the network hard-blocked. SwiftLaTeX was disqualified twice — it fetches
packages at compile time AND is AGPL. tectonic-wasm does not exist as a usable
artifact; latex.js is not a TeX engine and emits no PDF.

## Product decisions this forces — read before shipping
- **79 MB vendored** (≈48 MB gzip): busytex.wasm 27.5 MB + core.data 53.6 MB.
  Script-generated, gitignored, lazy-loaded — a user who never opens a LaTeX
  project pays **0 bytes** (main bundle stayed 149 KB).
- **Core tier only: no tikz, beamer, biblatex, siunitx, booktabs, fontspec,
  pgfplots.** The `academic` tier that carries them is 506 MB and is not
  vendorable. This is a genuine gap versus Overleaf. Escape hatch (verified): a
  hand-written .sty dropped into the project's files is found and used, which
  covers small self-contained packages but not tikz.
- **Supply chain**: wasmtex is 0.1.1, first published 2026-07-24, one maintainer,
  0 stars. Pinned to an exact version, assets vendored locally, all 8 files
  SHA256-verified against manifest.json at sync time.
- **Licensing**: wasmtex's own code is MIT, but busytex.wasm aggregates TeX Live
  binaries (pdftex/xetex are GPL, packages mostly LPPL). Attribution and a
  source offer are required — "MIT and done" is wrong.

## What the critic caught: all three features were INVISIBLE
Every track built and reviewed its own half and left the join to the
orchestrator, so a user opening the app found nothing. Two routers 404'd, the
api-client was missing six methods, and three components were rendered without
the optional props that switch them on. Integrated:
- [x] Mounted `board_ops` and `doc_pending` routers
- [x] Threaded `kind` through six layers that lacked it (store signature, REST
      schema + handlers, agent ToolSpec, client type, view branch) — routed
      through `latex.resolve_seed`, making its "every path routes through here"
      docstring true instead of aspirational
- [x] Six api-client methods; `columnOps` and `pendingEdits` props passed
- [x] Preview branches on `kind`: LatexPreview vs the esbuild ProjectPreview
- [x] Create form picks web vs LaTeX, with a hint naming the package limits

## Defects I fixed on top of the reviewers'
- **The starter did not compile.** `starter_files()` emitted
  `\bibliography{paper/refs}` for a subdirectory entry, but this engine runs
  bibtex from the entry file's own directory — bibtex exits 2. Now the bare
  stem. A test asserted the broken form; it now asserts the one that compiles.
- **Zero coverage on the ordering functions.** `cardSlot`/`reindex`/`slotFrom`
  decide where every dragged card lands; the fuzzing the board track claimed
  left no artifact. Added 11 exhaustive tests over every (size ≤ 6, item, slot)
  including negatives and out-of-range, checking permutation invariants and that
  client and server agree on the landing index.
- **Type drift**: views redeclared `ProjectSummary`/`WorkspaceProject` locally,
  which is exactly how `kind` went missing. They now re-export the client types.

- [x] Verify: make lint ✓, pytest 255 ✓, vitest 38 ✓, build ✓, e2e 2/2 ✓,
      alembic 0001→0010 ✓, OpenAPI regenerated. Plus live probes against the
      running app: board-ops inserts a column at the right index, documents-
      pending returns 200, and a kind=latex project seeds main.tex + refs.bib
      with a real \documentclass and the corrected \bibliography.

## Still open
- [ ] Split workspace.tsx (~2600 lines, nine views) — overdue
- [ ] `datetime.utcnow()` deprecation pass (naive-vs-aware decision)
- [ ] e2e for the approval/diff flow (e2e runs the deterministic provider)
- [ ] Decide tikz/beamer: accept the gap, or find a middle asset tier


---

# Debt paydown ✅ (workflow: split + clock + e2e)

## workspace.tsx split ✅  2623 -> 550 lines
- [x] Views moved to components/views/: sources, graph, dashboards (+ tile, editor),
      integrations, activity — joining chat, mcp, data, projects, documents, board
- [x] Shared helpers to views/shared.ts; state to components/use-workspace.ts;
      51 handlers regrouped into components/handlers/*.ts as plain factories
- [x] `api` moved to components/api.ts — keeping it in workspace.tsx made
      workspace -> use-workspace -> workspace a cycle, and passing it as a hook
      argument turned it reactive and produced 9 exhaustive-deps warnings, which
      would have meant editing dependency arrays the brief forbade
- [x] Verified by me (the split reviewer died mid-run): no hooks in handlers, no
      conditionally-nested hooks, setState-before-await preserved in uploadFiles,
      no import cycle, all 11 view branches present, 4 e2e green

## utcnow deprecation ✅  ~2223 warnings -> 1 (unrelated)
- [x] app/clock.py owns the single definition:
      `datetime.now(timezone.utc).replace(tzinfo=None)` — still naive UTC, so no
      column semantics change and no data migration
- [x] models.py re-exports it, so existing `from .models import utcnow` still works
- [x] Reviewer proved the catastrophic failure mode is absent by running under
      UTC / New_York / Kolkata / Auckland: matches epoch-derived UTC to 0.000s in
      every zone, differs from local by exactly the offset, tzinfo always None
- [x] Reviewer also found a `datetime.utcfromtimestamp()` still live on the Strava
      OAuth path, and — the better catch — that the regression test guarding the
      local-time bug PASSED under TZ=UTC even when the helper was sabotaged, so it
      would never have caught the bug on CI. Both fixed.

## e2e for the approval/diff flow ✅  2 -> 4 tests
- [x] The blocker was structural: e2e ran the deterministic provider, which never
      enters the agent loop, so no tool call was ever proposed. Solved with a
      scripted provider selected by env, not a test-only endpoint — and
      MODEL_PROVIDER=scripted with APP_ENV=production refuses at import, verified.
- [x] Covers: the card appears with the tool name, the diff renders with add/del
      lines distinguished, approve resumes the run and applies the write, deny
      leaves the document untouched, and the Documents-view path decides too
- [x] Mutation-tested: six deliberate breakages (empty preview, approve no-op,
      deny ignored, deny-but-still-write, policy always-allow, identical diff
      colours) EACH made the new tests fail, then were restored byte-identically
- [x] Found two real bugs in the Documents approval path: the editor refetched
      before the FastAPI BackgroundTask had applied the write (a genuine race,
      proven by injecting latency), and a failed decision froze the card forever

## A real bug in M1 chat, found by the e2e agent
`liveCalls` filtered out any call whose run already had a message — but the
user's own message carries the same run_id, so every approval card was hidden
for the whole turn. The feature was only visible because the Activity view had
its own list. Fixed and covered by both new e2e tests.

- [x] Gate: make lint ✓, pytest 268 ✓, vitest 42 ✓, build ✓, e2e 4/4 ✓,
      mypy 69 files ✓

## Known gaps
- [ ] IntegrationsView, McpView, DataView, ProjectsView, BoardView have no e2e
- [ ] Moving e2e to the scripted provider means the deterministic provider is no
      longer exercised in a browser (it is still covered by pytest)
- [ ] The Python suite is intermittently flaky under repeated full runs — flagged
      by a reviewer as pre-existing and out of scope; worth a look
- [ ] tikz/beamer remain unavailable in LaTeX (core tier only)


---

# Memory + knowledge graph, deepened (workflow: research -> 2 tracks -> critic)

Both subsystems already existed. This deepened them; it did not add them.

## The research finding that reframed the work
The recall path was not slow *at scale* — it was already slow **today**: 294ms of
blocking work per chat turn at only 1,000 memories, 1583ms at 10,000. Capping
alone does not fix it (596ms at cap=2000) because the pure-Python cosine loop,
not the row count, is the bottleneck. Chosen: numpy over a SQL-bounded candidate
set. Rejected sqlite-vec with measurements rather than taste — v0.1.x KNN is a
brute-force scan, not an index (1.9/12.5/115.8ms at 1k/10k/100k, i.e. linear), it
ships no musllinux wheel and no sdist so Alpine cannot install it at all, and it
would force a Postgres twin. A capped numpy scan beat it anyway (21ms vs 116ms
at 100k).

## Memory ✅ — 20.3x faster, and now agentic
- [x] recall: 1583ms -> 78ms at 10k memories, measured against HEAD's own code
- [x] `remember`, `forget`, `search_memory` — all in build_registry (verified),
      writes carry previews; `forget` previews exactly which memories it will
      tombstone, and never hard-deletes
- [x] Round-trip proven: remembered in one conversation, recalled in a new one,
      forgotten, then absent

### A regression the track reviewer missed, and I fixed
The 5000-row candidate cap is a **recency window**, so a memory older than it is
invisible to semantic scoring however relevant. Measured by the critic: five
memories at cosine 0.97 went from 5/5 recalled (HEAD) to **0/5**. "The assistant
forgot something it knew" is the worst failure this feature has. The reviewer's
harness topped out at 2000 rows — entirely below the cap — so the bug was
structurally invisible to it, and its "every divergence is a float tie, worst
2.2e-16" claim was false above 5k (real worst gap 4.6e-02).
- [x] Cap raised 5000 -> 20000: reproduces the unbounded ordering exactly and is
      still ~14x faster. ~35ms buys back correctness.
- [x] recall()'s docstring claimed "every row that could clear the gate is still
      fetched" — untrue. Rewritten to state the trade plainly.
- [x] Added the regression test that was missing: a memory sharing an embedding
      with the query but *no words* is dropped by a small cap and recovered by a
      large one, plus a guard asserting the default cap stays >= 20000.

## Knowledge graph — partly shipped, honestly
- [x] LLM entity + typed-relation extraction, regex kept as the deterministic
      fallback; `graph_neighbors` and `graph_path` added, both bounded
- [x] `GraphEdge.confidence` now reaches clients (schema + api-client), and the
      graph view draws typed relations solid with the relation named in the
      tooltip, co-occurrence dashed — they were previously pixel-identical
- **On the default deterministic provider the graph is byte-identical to before**
  — same 11 entities, same 37 co_occurs edges. Every improvement is gated behind
  `active_model_provider == "openai"`. A user cloning this repo sees no change.
- With a key and a perfect extractor: entity recall 58% -> 100%, precision
  64% -> 75%, correctly finding lowercase entities a capitalization regex cannot
  see ('retrieval service', 'postgres', 'redis'). But typed edges are only 13 of
  87, co-occurrence noise doubled 37 -> 74, and the four noise entities
  ('october', 'september', 'rfc', 'the atlas') are untouched.

- [x] Gate: make lint ✓, pytest 313 ✓, vitest 42 ✓, e2e 4/4 ✓, build ✓,
      retrieval eval 100%/100% ✓, alembic 0001->0012 ✓, 32 tools in the registry
      with every write tool carrying a preview

## Follow-ups this raised
- [ ] Cache `embed_texts([query])` per (query-hash, model): after the numpy fix,
      the OpenAI embedding round-trip (~50-200ms) dominates recall by 5-20x
- [ ] Graph noise: bare month names, `_entity_type`'s isupper() rule typing 'rfc'
      as an organization, and 'the atlas' never merging with 'atlas'
- [ ] The relation vocabulary is seven words; 27% of correctly extracted
      relations get coerced to the null 'related_to'
- [ ] No HTTP surface for remember/search/neighbors/path (tools only)
- [ ] Memory is visible in a panel inside the Graph view, not its own nav item


---

# 3D knowledge graph + full-feature browser coverage ✅

## Obsidian-style graph, in three.js
The old view was not a graph: it sliced to 24 entities and placed them on a
hand-computed **circle**. Replaced with a real force-directed 3D graph.
- [x] `components/graph-3d.tsx` — d3-force-3d simulation, three.js WebGL render,
      orbit/dolly camera, fog for depth, InstancedMesh nodes, hover-to-isolate
      (dims everything outside the hovered node's neighbourhood), click to select
- [x] Nodes coloured by entity_type and sized by mention_count; typed relations
      solid, co-occurrence dashed and dim — consuming the `relation`/`confidence`
      the earlier graph work added
- [x] No 24-node slice and no hand-placed ring: the simulation lays out whatever
      the API returns, so the cap belongs to the query
- [x] three.js is dynamically imported — main route went 149 -> 152 kB (+3 kB),
      confirming it lands in a lazy chunk, not the bundle everyone downloads
- [x] Camera frames the graph from its own bounds, so 5 nodes and 500 both fill
      the view; labels scale with that span
- [x] Graceful failure paths: no WebGL context, failed dynamic import, and
      `webglcontextlost` each show a message instead of a blank box; full
      disposal (geometries, materials, textures, forceContextLoss) on unmount
- [x] `types/d3-force-3d.d.ts` — the package ships no types, and a narrow
      declaration beats `declare module` making the simulation `any`

### Three defects found by *looking at a screenshot*, not by tests passing
1. Every node rendered **black**: `vertexColors: true` makes the shader read a
   per-vertex `color` attribute the sphere geometry lacks, defeating
   `instanceColor`, which InstancedMesh applies on its own.
2. **No edges at all**: d3-force's `forceLink` rewrites `link.source`/`target`
   from indices into resolved node objects, so `nodes[link.source]` was indexing
   by an object and returning undefined — every edge silently skipped. The same
   bug emptied the hover-neighbour map. Fixed by keeping our own numeric copies.
3. Labels sized in world units dwarfed a small graph; they now scale with the
   framed span.
None of these would have failed a "the canvas exists" assertion.

## Browser coverage for every view: 4 -> 11 e2e tests
Playwright MCP is not connected to this session, so this uses the repo's own
Playwright setup — which is better here anyway: it boots API and web together
and the result is permanent regression coverage rather than a one-off session.
- [x] `e2e/features.spec.ts` — documents (markdown + KaTeX actually rendering in
      the preview, save, history), boards (columns, add card, delete), projects,
      databases, MCP (transport switch changes the required fields), integrations
      and activity. Every test also fails on any console error or pageerror.
- [x] `e2e/graph3d.spec.ts` — seeds an entity-rich source, then screenshots the
      composited canvas. A WebGL canvas is unreadable via `drawImage` once
      composited (no `preserveDrawingBuffer`), so the first attempt reported a
      blank canvas that was in fact rendering fine — the screenshot is the honest
      check.
- [x] The sandbox is verified to actually RUN: esbuild-wasm compiles the starter
      TSX in-browser, React renders inside the `default-src 'none'` iframe, and
      clicking its button increments the counter to "Clicked 1 times"
- [x] Every test cleans up what it creates. Without that, the source uploaded by
      the graph test made the existing spec's "Delete source" lookup ambiguous —
      a failure I introduced and then fixed.

- [x] Gate: make lint ✓, pytest 335 ✓, vitest 42 ✓, tsc ✓, eslint ✓, build ✓,
      e2e 11/11 ✓


---

# Embedding cache + graph noise (workflow), and what the critic caught

## Track 1 — query embedding cache: honest, and a structural no-op by default
- [x] Bounded in-process LRU (256 entries), keyed on sha256(model + casefolded,
      whitespace-collapsed query). No migration: the only thing a table buys is
      sharing between processes, and this app runs one uvicorn with no workers.
- [x] Real bug fixed on the way: a blank/whitespace prompt paid a full embedding
      round-trip and then raised, because OpenAI rejects empty input.
- **The critic's finding: the cache cannot hold an entry without an API key.**
  `embed_texts` returns None for every non-openai provider, so `put()` is never
  reached. Measured 0 entries after 12 real recalls on a 10k-memory workspace.
  Not a low hit rate — a structural no-op on the configuration users run.
- Where it does apply it is worth **1.9-2.2x**, not the 3.1x claimed: warm
  126-135ms vs a 259-282ms pre-cache baseline. And once a corpus is genuinely
  embedded the local cosine scan (~50-60ms) is the new floor, so a warm OpenAI
  turn is *slower* than a no-key turn, which skips the vector scan entirely.
- Builder's own caveat, which is the right one: most turns have novel prompt
  text, so the win is per repeated query, not per average turn.

## Track 2 — graph noise: this one landed
On a real 16-document workspace, deterministic provider:
  **18 -> 12 entities, 105 -> 34 edges.** october, september, q3, 'the atlas'
  and 'the rfc' gone; main component is 10 nodes and fully path-connected
  (45/45 in-component pairs answerable).
Two corrections to the track's claims, from the critic:
  - 'rfc' is NOT gone — 4 mentions, passes the count>=2 rule. Only 'the rfc'
    merged into it.
  - 'SLA' is collateral damage from silently removing the ALL-CAPS acceptance
    branch. A legitimate entity lost with the noise.

## Two fixes I made on top

### The calendar filter was applied to questions, not just to the projection
`_graph_digest` ran `extract_entities()` over the *question*, so "Tell me about
Friday" extracted only "Tell" and returned an empty digest while the Friday node
sat in the graph. Reproduced, then fixed: `extract_entities(..., drop_calendar=
False)` for lookup. The guess is defensible when deciding whether to *create* a
node from capitalization alone; on a question it can only lose a match against a
node that already exists, never invent one. The projection side still filters.

### Memory was computed every turn and thrown away offline
`generate_grounded_answer` built a memory context — vector scan, graph digest and
all — then called `_deterministic_answer(evidence)`, whose signature could not
accept it. Proven by the critic: answers were byte-identical with and without a
memory line. So a no-API-key user paid the full cost of remembering and saw none
of it. `_deterministic_answer` now surfaces recalled memory, and answers from
memory alone when no passage matches.

- [x] Regression tests for both, including one asserting the projection still
      refuses calendar words so the noise cannot come back
- [x] Gate: make lint ✓, pytest 338 ✓, mypy 71 files ✓, retrieval eval
      100%/100% ✓, build ✓, e2e 11/11 ✓, OpenAPI regenerated

## The pattern worth naming
Three rounds in a row, an improvement was gated behind `active_model_provider ==
"openai"` and shipped nothing to a default clone. The agent loop itself is too:
`runs.py` only enters it for a non-deterministic provider, so **every agent tool**
— remember, forget, search_memory, graph_lookup, graph_neighbors, graph_path, and
all the document/board/project/SQL/MCP tools — is unreachable without a key.
- [ ] Decide this deliberately: either the no-key path is a real product mode and
      deserves a deterministic agent loop, or it is a demo and the README should
      say so. Right now it is neither.


---

# One product mode: the no-key path removed ✅

Owner decision: there should be no no-key path. Executed by a 3-agent workflow
(remove -> adversarial verify -> critic), then integrated.

## What changed
- [x] `MODEL_PROVIDER: Literal["openai", "scripted"]`, default openai. Missing key
      fails at **Settings construction**, not on the first chat turn:
      "MODEL_PROVIDER=openai requires OPENAI_API_KEY. Set it in .env (see
      .env.example) — there is no offline mode."
- [x] **`runs.py` no longer branches — the agent loop always runs.** This is the
      real payoff: HEAD had `if active_model_provider == "openai": run_agent_turn
      else: generate_grounded_answer`, so on a keyless clone the entire tool
      surface was dead code. **32 tools** are now reachable that were not, and the
      safety invariant holds: 18 write tools, 0 without a preview.
- [x] Deleted: `generate_grounded_answer`, `_deterministic_answer`,
      `_offline_no_evidence_answer`, `_deterministic_memories`, the `use_llm` gate
      in rebuild_graph. The regex entity backbone was correctly left alone — it is
      the candidate generator the LLM pass types, not a fallback.
- [x] Test suite migrated to the `scripted` double: **341 pass with no real key**
- [x] README, .env.example, ARCHITECTURE and THREAT_MODEL updated

## The builder pushed back on my brief, correctly
I claimed `stream_words` only served the deterministic path. It verified that was
false — `runs._complete_with_message` uses it for all three `/tool` replies, which
compose text with no model behind them, and `scripted_model._speak` uses it too.
It updated the comment instead of deleting the function. The right call.

## Two tests deleted, both correctly
Including `test_offline_answers_surface_recalled_memory`, which **I added one turn
earlier** to surface memory in the deterministic answer. That fix was right for the
world that existed then; the owner's decision deleted the function it tested. Worth
recording rather than hiding: the work was not wasted, it was overtaken.

## What the critic proved, beyond the gate
- Ran the real uvicorn command with the key blanked: exit code 1, refuses before
  accepting traffic. With a fake key it boots and serves 66 routes.
- **Socket-blocked the entire test suite with the real OPENAI_API_KEY present:
  0 outbound connections across 341 tests.** Far stronger than reading a gate.
- Mutation-tested the suite: 12 mutations, 11 killed.
- Replaced `build_registry` with `{}` and watched live tests fail, proving the
  registry is genuinely built and consumed on every turn.

## Two real bugs the removal introduced, which I found and fixed
1. **`make migrate` was broken.** `env_file=".env"` resolves against the cwd and
   `make migrate` cds into apps/api, where no .env exists — so the new key
   requirement fired and the documented deploy step failed. Anchored to
   `project_root()` like the sqlite and objects paths. Same failure mode already
   in lessons.md; the key requirement turned a latent inconsistency fatal.
2. **An existing `.env` with `MODEL_PROVIDER=auto` fails to boot** with a raw
   pydantic literal error that never says the mode was removed. Fixed locally.

## Leftovers I cleaned up after the critic
- `packages/api-client/src/index.ts` typed `provider: "deterministic" | "openai"` —
  naming the dead mode and omitting the live one. Fixed.
- `packages/api-client/openapi.json` still carried `"deterministic"` in the enum.
  Regenerated; the CI drift check now passes.
- Correction to the critic: CI **does** have an OpenAPI drift check
  (.github/workflows, lines 34-38), so this would have failed the build rather
  than rotting silently. And the web app's remaining "offline" strings are about
  the LaTeX bundle and network-unreachable errors, not the removed mode.

- [x] Gate: make lint ✓, pytest 341 ✓, vitest 42 ✓, e2e 11/11 ✓, build ✓,
      retrieval eval 100%/100% ✓, OpenAPI drift ✓

## Known rough edge
The startup refusal is correct but lands on line 54 of a 55-line traceback. An
operator sees `Traceback (most recent call last)` first. Folding a clean
one-line diagnostic into the deployment work rather than bolting it on.

---

# Auth + deployment (in progress)
Decisions: **multi-user SaaS**, **Google + email/password**, **Vercel web + separate
API host**.

- [x] Migration 0013 + models: `User.password_hash` (nullable — a federated-only
      account has none, and "no password set" is one bug from "any password
      matches"), `email_verified_at`, `status`, `failed_logins`, `locked_until`;
      new `UserIdentity`, `UserSession`, `EmailToken`, `WorkspaceInvite`.
      Verified 0001->0013 on a scratch DB.
- Design properties encoded deliberately:
  - Only token **hashes** are stored, never raw — a DB leak yields nothing
    replayable as a login.
  - `UserIdentity` keys on the provider **subject**, not email: an email can be
    reassigned inside a Workspace domain, and matching on it would hand the new
    holder the old account.
  - `UserSession.csrf_secret` exists because Vercel -> API forces
    `SameSite=None`, which re-opens the CSRF that `SameSite=Lax` closes for free.
- [ ] Replace `get_actor`'s trusted `X-User-Id`/`X-Workspace-Id` headers. **This is
      the actual vulnerability**: any client can currently claim any identity —
      untidy single-user, a data breach multi-tenant.
- [ ] Google OAuth + email/password (argon2, verification, reset, lockout)
- [ ] Cross-tenant isolation audit: an automated test proving every
      workspace-scoped query rejects a foreign workspace
- [ ] Deployment: API Dockerfile, Vercel config, cross-origin cookies/CORS,
      S3/R2 objects (Vercel has no local disk), alembic as a release command,
      SSE verified through the proxy, clean startup diagnostics


---

# RAG: step 0, fix the ruler ✅

## The old eval could not fail
2 documents, 4 questions, `limit=5` — recall@5 returned the entire corpus and
scored 100%/100% by construction. It also checked only the *filename*, so
retrieving the wrong passage from the right document counted as a hit, and every
question echoed its document's own keywords ("What color is the Juniper
deployment **ring**?"), which is exactly the case where a lexical matcher looks
perfect and dense retrieval looks unnecessary. A saturated benchmark cannot tell
you whether a change helped, which is the only thing a benchmark is for.

## What replaced it
- **22 documents** describing one fictional company, so vocabulary genuinely
  overlaps and distractors are real rather than contrived
- **28 questions in three labelled categories**, reported separately:
  - `lexical` — repeats the document's words; any keyword matcher should win
  - `paraphrase` — same meaning, different vocabulary
  - `indirect` — requires understanding ("A cancelled account still has access to
    a paid feature. Which system is authoritative?")
- **Passage-level ground truth** via `must_contain`: the right document for the
  wrong reason is a miss
- **MRR alongside recall**, because ranking quality is what reranking moves
- **Per-category regression floors** just under the measured baseline. A single
  global floor would either be carried by the lexical set or fail everything.

## Baseline — purely lexical retrieval, measured now
```
lexical     recall@5=100.0%  mrr=1.000  n= 8
paraphrase  recall@5= 70.0%  mrr=0.470  n=10
indirect    recall@5= 80.0%  mrr=0.667  n=10
overall     recall@5= 82.1%  questions=28
```
The five misses are precisely the semantically distant ones — e.g. "invoices not
matching what the bank sent us" against a document that says "settlement files
from our processors against ledger entries". These are the questions hybrid
retrieval exists to rescue.

Note `paraphrase mrr=0.470` against `recall=70%`: even when the right passage is
found it is often ranked second or third, so there is ranking headroom as well as
recall headroom.

- [x] Verified the harness has teeth: with `search_evidence` returning nothing,
      all three categories drop below their floors and it exits 1.

## Next: hybrid retrieval
Evidence-backed plan, in order:
1. `Chunk.embedding` + embed at ingest — the structural gap; memory has vectors,
   documents never did
2. RRF fusion of the existing lexical scorer with dense — rank-only, so it
   sidesteps the score-incompatibility that breaks weighted blends
3. Contextual Retrieval (a one-sentence document summary prefixed to each chunk
   at ingest) — the largest measured lift, one-time cost, no per-query latency
4. Reranking deliberately deferred: a cross-encoder needs a new vendor or a heavy
   local model, and LLM reranking adds 2-5s, which is disqualifying for chat

**Blocked on the auth workflow**, which may still touch `models.py` and the
migration chain. Doing hybrid as one clean piece once it lands rather than
risking a clobber mid-flight.

## Phase 6 — Login experience (web) ✅
- [x] api-client owns the session: `credentials: "include"`, `X-CSRF-Token` on
      unsafe methods, `X-Workspace-Id`, one-shot re-fetch-and-retry on a stale
      CSRF token, `onUnauthorized` hook for every 401 outside /api/auth
- [x] `streamRun` carries the cookie and reports its own 401
- [x] `sendMessage` omits `agent_id` when bootstrap returns "" (per-workspace agent)
- [x] SessionProvider / AuthGate: loading → authenticated | anonymous | offline;
      no chrome and no requests before the session resolves
- [x] AuthPanel: Google (hidden on 503), email+password, signup → "check your
      email", password reset request; /auth/login, /auth/verify, /auth/reset, /login
- [x] `?auth_error=` from the Google callback rendered and stripped
- [x] Sign out in the sidebar identity block
- [x] e2e: DEV_AUTO_LOGIN off, setup project signs in and shares the cookie jar;
      auth.spec covers signup → workspace → CSRF write → sign out → sign back in
- [x] Fixed a load/create race the new spec caught (see lessons.md)
- [x] Verify: ruff ✓, mypy 78 ✓, pytest 388 ✓, tsc ✓, eslint ✓, vitest 45 ✓,
      build ✓, e2e 14 ✓ (11 existing + 2 auth + 1 setup)

## Hybrid retrieval (RESEARCH #1/#2/#3) — in progress

- [ ] 0015 migration: `chunk_terms` + `chunks.lexical_length` (NULL = not indexed)
- [ ] BM25 with IDF over the portable term table, ablatable via `RETRIEVAL_BM25`
- [ ] Dense arm at ingest (`Chunk.embedding`), best-effort, ablatable via `RETRIEVAL_HYBRID`
- [ ] RRF fusion of the two rankings (rank-only, k=60)
- [ ] Contextual Retrieval blurb at ingest, ablatable via `RETRIEVAL_CONTEXTUAL`
- [ ] Backfill path for chunks written before any of this
- [ ] Eval harness: run all four stages, per-arm attribution, latency split

## Memory supersession (RESEARCH #4) ✅ — stale-served 100% → 0%

`_upsert_item` keyed every memory on a hash of its content, so "deploys on
Fly.io" and "moved to Railway" were unrelated rows. Both stayed active, both
matched a deployment question, and both were injected — and because `importance`
accrues to whichever *phrasing* recurs rather than to whichever claim is current,
a stale fact repeated often outranked its own correction. Recall read 93.3%
throughout, which is why this was invisible: recall said the system was healthy
while it handed the model a claim and its contradiction with nothing to tell them
apart.

- [x] `MEMORY_EXTRACTION_INSTRUCTIONS` asks for `normalized_key`, a lowercase
      `subject|relation` claim key; `normalize_claim_key` validates it as
      untrusted model output (charset, both halves present, 64 chars a side) and
      falls back to the content hash when it is missing or malformed — a bad key
      costs supersession, never correctness
- [x] `_upsert_item` retires the older value of a claim: `status="superseded"`,
      `superseded_by` = the replacement's id, no importance carried forward
- [x] "Materially different" = a different significant-token multiset, so
      punctuation, casing, stopwords and word order can move without churning a
      row, while a changed host/name/weekday/version is a new value. The claim
      key has already asserted same subject + same relation, so a similarity
      threshold would have to call Fly.io/Railway a rewording
- [x] The retired row vacates its claim key (`<key>~superseded~<id>`) because the
      unique constraint is `(workspace, kind, normalized_key)` and is worth
      keeping — it is what stops two concurrent runs leaving two live rows for
      one claim. **No new migration:** 0014 already landed `superseded_by`
- [x] Recall unchanged: superseded rows drop out through the existing `_active()`
      chokepoint, and a test asserts that is still the module's *only* status
      filter. Zero scoring changes
- [x] `forget` stays distinct — a tombstone outranks a restatement of the value
      it was written for, and that value is still never resurrected. The
      tombstone gives up the claim key on the way down (`tombstone_key`, back to
      the content hash the key meant before claim keys existed): a tombstone
      names a value, not the slot. **Review fix** — while it kept the claim key
      it blinded the whole slot, and forgetting each corpus item's first fact
      took overall recall 93.3% → 60.0% with all five `knowledge_update` answers
      permanently unlearnable
- [x] `kind` cannot defeat supersession. The lookup was `(workspace, kind, key)`,
      but `kind` is per-turn model output and nothing pins it per claim, so one
      claim arriving as `preference` then `fact` left the claim and its own
      correction side by side. **Review fix** — a claim key is looked up across
      kinds (a content hash keeps its old, kind-scoped meaning). Relabelling only
      the correcting turn had put the stale rate back to 100% (5/5)
- [x] Ablatable: `MEMORY_SUPERSESSION=0` restores the measured baseline exactly
      (content-hash keys *and* no retirement — either half alone is a third
      behaviour nobody measured)
- [x] `evaluate_memory.py` now seeds through `apply_extracted_memories`, the
      write path itself rather than a copy of it, with the corpus supplying the
      claim keys the extractor is asked for. The stale probe is judged per
      memory and ignores rows that also carry the current value: a correction is
      allowed to name what it replaced ("moved the API *from Fly.io* to
      Railway"), and matching the joined blob counted that sentence against
      itself

| MEMORY_SUPERSESSION | stale-served | overall recall | preference | single_session | multi_session | temporal | knowledge_update |
|---|---|---|---|---|---|---|---|
| 0 (baseline) | 100.0% (5/5) | 93.3% | 66.7% | 100% | 100% | 100% | 100% |
| 1 (default)  | **0.0% (0/5)** | 93.3% | 66.7% | 100% | 100% | 100% | 100% |

Not one category moved, which is the number that matters second: retiring facts
too eagerly would show up here as a recall drop. `STALE_CEILING` went from 1.01
(no ceiling) to 0.0.

Follow-ups, not blockers:
- `remember_memory` (the agent's explicit write) still keys on the content hash,
  so a user-stated correction does not retire the extractor's row. It needs a
  claim key of its own before it can. **This is now the largest hole in the 0%
  number, and it is measurable**: routing each corpus item's correcting fact
  through `remember` instead of the extractor puts the stale-served rate back to
  100% (5/5). The row it creates is unreachable by claim key, so no later
  correction can ever retire it — it serves a stale value for the life of the
  workspace. It self-heals only when the same turn's extractor also writes the
  claim (both rows then hold the current value), which is the common case but not
  a guarantee.
- Nothing prunes retired rows. They are invisible to recall and to the graph
  rebuild, but a claim corrected weekly for a year keeps 52 rows.

## Review — hybrid retrieval (RESEARCH.md #1, #2, #3)

- [x] 0015 migration: `chunk_terms` + `chunks.lexical_length` (NULL = not indexed)
- [x] BM25 with IDF over the portable term table (`RETRIEVAL_BM25`)
- [x] Dense arm at ingest, best-effort (`RETRIEVAL_HYBRID`)
- [x] RRF fusion, rank-only, k=60
- [x] Contextual Retrieval blurb at ingest (`RETRIEVAL_CONTEXTUAL`) — **measured, off**
- [x] Backfill: `scripts/backfill_chunks.py` + read-path `reconcile_index`
- [x] Eval harness runs the four stages and reports per-arm attribution

`evaluate_retrieval.py --stages`, 22 documents / 28 questions, recall@5 / MRR:

| stage | lexical | paraphrase | indirect | overall | p50 latency |
|---|---|---|---|---|---|
| lexical-only (baseline) | 100.0% / 1.000 | 70.0% / 0.470 | 80.0% / 0.667 | 82.1% | 0.8ms |
| +bm25 | 100.0% / 1.000 | 90.0% / 0.700 | 80.0% / 0.800 | 89.3% | 1.0ms |
| +dense/rrf | 100.0% / 1.000 | **100.0% / 0.883** | **100.0% / 1.000** | **100.0%** | 259ms |
| +contextual | 100.0% / 1.000 | 100.0% / 0.950 | 90.0% / 0.900 | 96.4% | 247ms |

Re-measured under adversarial review (3 independent index builds per stage). Rows
1-3 reproduce exactly. Row 4 did not: contextual retrieval is a **reproducible
regression** on this corpus, not the no-op first reported — see below.

What each stage bought:

- **BM25/IDF** is the cheapest win in the set: +20pp paraphrase and +0.23 MRR for
  ~0.2ms, no provider, no network. It also removed the O(corpus) Python
  re-tokenization that every query used to pay.
- **Dense + RRF** fixed the remaining vocabulary-mismatch misses ("compute
  representations upfront or when someone asks" against "precompute embeddings")
  and pushed everything the lexical arm already found to rank 1 — indirect MRR
  0.800 -> 1.000. Per-arm attribution: 25 both, 3 dense-only, 0 lexical-only, 0
  neither. Lexical never regressed, at any stage.
- **The dense arm needs a similarity floor**, and that is the one thing here that
  was not in the plan. Cosine is almost never zero, so an ungated dense arm ranks
  the entire workspace for every query: "we have nothing on that" stops being an
  outcome a citation-first product can reach. `retrieval_dense_floor=0.3` (the
  value `memory.recall` already uses) restores it — and improves ranking, because
  RRF weighs a rank rather than a score, so every barely-related chunk admitted at
  rank 6-50 competes on equal footing with a real match. A/B on one embedded
  corpus, floor as the only variable, overall recall@5 / paraphrase MRR:
  0.0 -> 96.4% / 0.900, 0.2 -> 100% / 0.925, 0.3 -> 100% / 0.950,
  0.4 -> 96.4% / 0.900 (and indirect falls to 90%, i.e. the floor starts cutting
  true matches).
- **Contextual Retrieval costs a question on this corpus — it is a regression,
  not a no-op.** Corrected under review. Three independent runs of `+dense/rrf`
  vs `+contextual` on freshly built indexes:

  | run | +dense/rrf overall | +contextual overall | +contextual indirect |
  |---|---|---|---|
  | 1 | 100.0% | 96.4% | 90.0% / 0.900 |
  | 2 | 100.0% | 96.4% | 90.0% / 0.900 |
  | 3 | 100.0% | 96.4% | 90.0% / 0.900 |

  Same question missed all three times ("Which group decides how results are
  ordered?"), and the per-arm attribution shows why it is not fusion noise: the
  blurb moves that passage out of *both* arms' top-5 (`dense only 3, neither 0`
  becomes `dense only 1-2, neither 1`). RESEARCH.md §4.1 predicted no *gain* —
  the eval documents average 246 characters and are already self-contained — but
  the measured effect is a loss: a one-sentence blurb on a two-sentence chunk is
  a third of the embedded text, and it dilutes rather than situates. The code
  ships switched off, and this is now a measured reason to keep it off rather
  than an absence of evidence. It still wants re-measuring on the grown corpus of
  item 13, where chunks are long enough for the blurb to have something to add.
- **MRR does not reproduce to three decimals; recall does.** Paraphrase MRR at
  `+dense/rrf` came back 0.883, 0.950, 0.950 across three runs of an identical
  configuration, so the 0.883 first reported and the 0.950 in the floor A/B are
  the same configuration measured twice, not two configurations. Two causes: the
  embedding API is not bit-deterministic, and RRF ties a dense-only rank-1
  against a lexical rank-1 exactly (1/61 each), leaving the citation order to a
  UUID tiebreak. Quote recall from these tables; treat a sub-0.1 MRR difference
  as noise.

Cost of the dense arm: one embedding round-trip per distinct query, p50 259ms,
which is the entire added latency — scoring is 1.6ms p50 at this corpus. The
round-trip is shared with memory recall through `query_embedding_cache`, so a
turn that retrieves *and* recalls pays it once.

No misses remain at the shipped configuration (28/28). The last one to fall,
"Who should I talk to about invoices not matching what the bank sent us?", was
fixed by the dense floor rather than by the dense arm itself.

Fixed under adversarial review:
- **BM25 kept the *longest* query terms, not the rarest**, and every table above
  was blind to it: 27 of the 28 eval questions have 12 or fewer content terms, so
  the cap never fired. `search_evidence` runs on `run.prompt` — a whole user
  message — which routinely exceeds it. Measured on a 9-chunk corpus, a prompt
  carrying "infrastructure/documentation/authentication" alongside "kestrel" lost
  the proper noun to the boilerplate and ranked the answer nowhere, while the
  uncapped pre-BM25 scorer it replaced ranked it #1 — i.e. BM25 was a strict
  lexical regression outside the tested regime. Term selection now asks the index
  for document frequency and keeps the rarest, dropping terms the corpus does not
  contain; the aggregate it pays for is the one `bm25_ranking` already needed, so
  the capped-postings branch reuses it. Short queries keep the no-extra-query
  fast path. Regression tests: `test_a_long_prompt_keeps_its_rarest_term_not_its_longest`,
  `test_a_query_term_nothing_contains_does_not_squander_a_slot`.
- **A blurb call that raised took the term index and the vectors with it.**
  `prepare_for_search` ran the optional stage first with no isolation, so anything
  escaping `situate_chunk`'s own catch skipped both mandatory stages. The index
  self-heals via `reconcile_index`; a vector does not heal on any read path, so
  the dense arm would have been silently empty until someone ran the backfill.
  Isolated and logged. Regression test:
  `test_a_failing_blurb_call_does_not_take_the_index_and_vectors_with_it`.

Follow-ups, not blockers:
- The floors in `evaluate_retrieval.py` describe the hermetic configuration the
  gate actually runs (no provider ⇒ no dense arm). They cannot describe the
  shipped hybrid until the harness has an offline embedding double.
- Dense-stage tail latency is worse than the p50 suggests: p95 measured at 342,
  373, 741, 1181 and 2481ms across runs against a p50 of ~250ms. RESEARCH.md §4.3
  budgets 50-200ms for the round-trip, so the tail is the number to watch, and it
  is one external call with `max_retries=1` behind it.
- RRF gives a dense-only rank-1 exactly the same fused score as a lexical rank-1
  (1/61 each). Which one is cited first falls through to a `(source_id, ordinal)`
  tiebreak, i.e. a UUID. Deterministic per corpus, arbitrary across corpora, and
  it lands on precisely the questions the hybrid exists to win.
- `reconcile_index` indexes 500 chunks per search. A large pre-existing workspace
  converges over several searches; `scripts/backfill_chunks.py` is the bulk path.
- Corpus item 13 is still open, and it bounds everything above: 28 questions
  across three strata means one question is 10-12.5 points.

## Navigation: eleven top-level items → five task-shaped groups ✅
- [x] `views/navigation.ts`: NAV_GROUPS (Chat · Create · Knowledge · Connections ·
      Activity), `groupForView`, `DEFAULT_GROUP_VIEW`. The sidebar and the tab
      strip are both generated from it, so there is one place a view can go missing.
- [x] Sidebar renders five groups; each group's siblings live in a `.view-tabs`
      strip under the topbar (plain buttons + `aria-current`, not `role=tab`, so
      the e2e suite's `getByRole("button")` navigation still addresses them).
- [x] A group reopens where you left it (`groupHome`, keyed off `view` so the
      OAuth return and an upload's own `setView` are remembered too).
- [x] **Memory got a real home**: `views/memory.tsx` under Knowledge — list,
      search, forget. Removed the panel it used to hang off the bottom of Graph,
      and `GraphView` no longer takes `memories`/`forgetMemory`.
- [x] Group badges sum their tabs' counts. Graph carries no count: it is a
      projection of Sources, so counting it would double-count the same input.
- [x] Verified: tsc ✓, eslint ✓, vitest 144/144 (139 before + 5 in the new
      `tests/navigation.test.ts`), `pnpm build` ✓, e2e 22/22 (18 before + 4 in
      the new `e2e/navigation.spec.ts`).

## Server-side agentic sandbox (ADR 0005) — in progress

Reverses the *browser-only* half of ADR 0004 and keeps its security half. The
principle ADR 0004 actually bought was "generated code runs somewhere with no
workspace authority", and a hosted Firecracker microVM keeps that: execution
still never touches a Jasmine host, holds no session, and cannot reach the
database. What changes is that the boundary now has a kernel, a filesystem and
a socket — which is what "analyse this CSV and plot it" has always needed.

Chosen: managed provider (E2B), behind a `SandboxProvider` Protocol so nothing
above the seam imports the SDK and the whole tool/quota/approval layer is
testable with a fake and no key.

Capabilities in scope, all four confirmed against the installed SDK:
data analysis + charts (`run_code` -> structured `Result.chart`, not just a PNG),
arbitrary package install (`commands.run`), network egress (`allow_internet_access`
+ `network.allow_out/deny_out`), persistent workspaces (`pause(keep_memory=True)`
+ `connect(id)`, and `lifecycle.on_timeout=pause` so an idle gap does not destroy
a session's filesystem).

- [x] ADR 0005 written - states the residual risk rather than burying it.
- [x] Migration 0016 + `SandboxSession` / `SandboxExecution`. Verified against a
      scratch database through the full 0001->0016 chain, not just the new head.
- [x] `sandbox/types.py` - the provider seam. Frozen, JSON-round-trippable, no
      provider objects hiding inside, so a handle survives being rebuilt from a
      row in a different worker process than the one that created it.
- [x] `sandbox/policy.py` - `ALWAYS_DENIED_CIDRS` (metadata + RFC1918 + loopback,
      v4 and v6) denied under every policy including `open`; `sandbox_env` is
      *built*, never filtered from `os.environ`, because a filter is one
      forgotten name away from leaking a key and the forgotten name is always
      the one added last.
- [x] Settings + `_guard_sandbox`, mirroring `_guard_model_provider`: the fake
      provider cannot be reached outside development/test, and e2b without a key
      fails at startup rather than on the turn someone asks for a chart.
- [x] Registered in `llm_tools.build_registry` and `main.py`.
- [x] Providers (subprocess + container + e2b + fake + factory), session
      lifecycle + quotas, agent tools + artifact persistence, HTTP routes + UI.
- [x] Network reversed to `none` by default; packages pre-baked in
      infra/sandbox/Dockerfile, which is now the package policy.
- [x] Security tests: cross-tenant, introspection-driven secret-leak, egress
      floor, fail-closed. Threat model updated with the honest residual risk.
- [x] **Bug found by the hardening pass and fixed**: `ALL_TRAFFIC` was
      `0.0.0.0/0` alone, which says nothing about IPv6. Under `allowlist` an
      unlisted IPv6 destination was reachable unless the driver happened to
      treat `allow_out` as deny-by-default; under `none` the strictest policy
      was leaning on the internet flag rather than its own list. Both families
      are now denied explicitly and each policy's list stands alone. One test
      asserted the old behaviour and was rewritten rather than deleted.
- [x] Gates: ruff clean, mypy 94 files clean, **pytest 883 passed**, tsc clean,
      eslint clean (after ignoring `.next-review/**`, a stray Next build dir
      that was contributing 204 errors from generated code), vitest 144/144.

Still unproven: the container driver has never run against a real Docker daemon
— Docker is not installed here, so its tests assert the argv flag by flag. That
catches a dropped `--network none` in CI, which is the failure that matters, but
the first real `make sandbox-image && docker run` remains to be done.

**The residual risk, stated once so it is not lost in the diff:** a sandbox on
egress `open`, holding documents the user uploaded, can send them anywhere. The
realistic trigger is not a microVM escape - it is prompt injection through a
document the agent was asked to analyse. `allowlist` closes it and costs
`pip install`. `open` is the default because the feature is pointless otherwise,
and that is a judgement call rather than a proof of safety.

---

# MCP OAuth, hosted web search, container proof, AWS deploy — reconciliation

Four tracks landed in parallel, each with an adversarial review. This section is
the reconciliation pass: what was left conflicting or half-done, what the
reviewers found and nobody fixed, and — separately — what is still not proven.

## Reconciled

- [x] **`test_tenant_isolation.py::test_route_table_matches_the_app` was red on
      `main`** for the four new MCP OAuth routes. Added their `RouteCase`s to
      `apps/api/tests/isolation.py`: the three `/servers/{id}/…` routes as DENY
      (they go through the workspace-scoped `_load`, so a foreign id 404s before
      `/connect` can dial the network), and `/oauth/callback` as PUBLIC, which is
      deliberate — the single-use state row is the credential and carries the
      user id itself.
- [x] **`container_provider.py` was left uncompilable** by two agents editing it
      at once: a `_cli_env()` helper using `os`/`shutil`/`Dict` landed without
      its imports (`NameError: shutil` at construction, six sandbox tests
      erroring). Imports reconciled; my `--name`/kill work merged on top and
      routed through the same `_cli_env()`, since a `docker kill` sent to the
      default socket when the run went to a Colima context reports success
      against a container that is still running.

## Real defects fixed (found by reviewers, left unfixed by every track)

- [x] **The sandbox could not write its own workspace.** Three reviewers
      independently confirmed by inspection: `ensure_session_root` created the
      session dir with the API user's umask (0755) and `_docker_argv`
      bind-mounts it under `--user 65534:65534`. A bind mount keeps host
      ownership, so `plt.savefig("chart.png")` — the headline user story — gets
      EACCES while every print-only run passes, which is why no smoke test
      caught it. `ensure_session_root` now takes an explicit `mode`, and only
      the container driver passes it; the subprocess driver runs as the API user
      and is deliberately left alone.
- [x] **A timed-out run leaked a live container.** `_kill_tree` SIGKILLs the
      `docker` CLI, but the daemon owns the container, so `while True: pass` kept
      burning `--cpus 2.0` after the run had already returned its result —
      directly contradicting the module docstring's "a crashed API leaks no
      compute". Containers are now named and killed. **Reproduced against real
      Docker before and after: 1 leaked container without the fix, 0 with it.**
      Two regression tests, mutation-checked.
- [x] **Authorization-server mix-up (migration 0018).** `OAuthState` recorded
      which MCP server a flow was for but not which *issuer*, so the callback
      resolved the registration by "most recently updated wins". A server that
      rotates its advertised authorization server mid-flow would be handed the
      victim's authorization code *and* PKCE verifier — everything needed to
      redeem them at the honest issuer. Retiring old registrations does not close
      this; it is what makes the newer row the only one. `oauth_states.issuer` is
      now set at `begin_authorization` and matched at `complete_authorization`.
      Regression test mutation-checked against the old recency lookup.
- [x] **A revoked grant re-hit the token endpoint on every tool call.**
      `_mark_expired` set `status` but `access_token` never read it, so the row
      stayed "stale" and refreshed again — forever. It now clears the token
      material, which also stops holding a credential the provider revoked.
- [x] **The metadata size cap was decorative.** `_read_json` sliced
      `response.content`, which httpx had already buffered in full — the check
      fired after the damage it existed to prevent. All four hops now stream
      with a chunk loop. Mutation-checked: removing the guard changes the error
      from "oversized" to "malformed", i.e. it had read the whole thing.
- [x] **Refresh raced itself with no row lock.** The leeway window is minutes
      wide by design, so two turns for one (server, user) crossing is ordinary.
      Both would spend the same refresh token; OAuth 2.1 rotates refresh tokens
      for public clients and RFC 6819 has the server treat a replay as a breach
      and revoke the grant family — the user is silently disconnected with
      nothing in our logs pointing at us. Now `SELECT … FOR UPDATE` with a
      staleness re-check inside the lock, so the loser reuses what the winner
      wrote instead of replaying.

## Docs

- [x] ADR 0006 — dynamic registration as a public client, per-user tokens, the
      host-allowlist relaxation stated as the one control given up, and the
      three issuer/audience bindings.
- [x] `THREAT_MODEL.md` — a new section for the two genuinely new surfaces: a
      user-supplied URL reaching an HTTP client, and third-party account tokens
      at rest.

## NOT fixed, and why

- **DNS rebinding on the discovery hops.** `_validate_destination` resolves and
  validates, then httpx resolves again at connect. In `services/tools.py` the
  host allowlist contains this; relaxing the allowlist here removes exactly that
  containment, so it is a real widening rather than an inherited gap. The fix is
  a transport pinned to the validated address — too invasive for this pass, and
  wrong to fake.
- **`disconnect` does not revoke upstream.** No `revocation_endpoint` is
  captured or called. The button promises more than it delivers.
- **`web_search`: the un-anchored fallback.** `agent_loop.py`
  `answer = state.text_so_far or response.output_text` bypasses
  `anchor_citations` when a step produced no deltas. It degrades toward
  *uncited*, never toward a fabricated or misplaced marker, and the string
  validated is still the string stored. Changing it alters what every non-web
  multi-step turn accumulates, which is the loop owner's call, not a reviewer's.
- **`ProtectedResource.resource` is now dead** after the authorize leg moved to
  `canonical_resource(server.url)`. RFC 9728 §3.3 gives it a job; the exploit it
  would guard is already closed by sending the canonical URI on both legs.
- **`main.py` startup recovery is inside `if settings.is_dev_env:`**, so with
  `APP_ENV=production` expired run leases are never swept. `infra/aws/ecs.tf`
  and `ec2.tf` both justify decisions with recovery that will not run. Real, and
  outside this pass.
- **`infra/sandbox/Dockerfile` warms the matplotlib font cache into
  `MPLCONFIGDIR=/tmp/mpl`**, which `_docker_argv` then mounts a tmpfs over, so
  every container rebuilds it. **Confirmed against the real image**: without the
  tmpfs `/tmp/mpl/fontlist-v3.11.0.json` is present; with the driver's actual
  `--tmpfs /tmp` the directory does not exist at all. The reviewer estimated
  "several seconds on every real user plot"; measured, it is ~0.1s (0.33s vs
  0.22s to first savefig), so the comment is wrong but the cost is minor.
  Fixing it means moving `MPLCONFIGDIR` somewhere the tmpfs does not shadow and
  rebuilding a ~2 GB image to verify — not worth doing blind at this size.
- **`aws` CLI presence on the AL2023 ECS AMI is assumed** by
  `user_data.sh.tftpl`. Unverifiable without an account.
