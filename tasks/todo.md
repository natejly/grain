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
