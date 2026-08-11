# Session review — five tracks landed, reconciled and verified

Five tracks were built in parallel against baseline `0fe566c`, then reconciled.
Everything below was re-run serially in the shared tree by the reconciling agent;
the per-track numbers reported mid-session are superseded by these.

## Gates (real output, all personally run)

```
.venv/bin/ruff check apps/api                        → All checks passed!
.venv/bin/mypy apps/api/app                          → Success: no issues found in 118 source files
PYTHONPATH=apps/api pytest apps/api/tests            → 1510 passed, 1 skipped, 3 xfailed in 29.64s
PYTHONPATH=apps/api python scripts/export_openapi.py → packages/api-client/openapi.json
apps/web  pnpm tsc --noEmit                          → clean
apps/web  pnpm lint                                  → clean, 0 problems
apps/web  pnpm test                                  → 290 passed (13 files)
apps/web  pnpm build                                 → Compiled successfully, 8/8 static pages
npx playwright test                                  → 44 passed (1.7m)   run 1
npx playwright test                                  → 44 passed          run 2
```

Baseline was 1398 pytest / 253 vitest / 42 playwright. Deltas: +112 pytest,
+37 vitest, +2 playwright (+3 folders, −1 sandbox-create, deleted with its
destination).

## What landed

- [x] **Dashboards are reachable** — `services/dashboards/` (store, binding,
      agent tools), `api/dashboards.py` (12 routes), parameterised templates
      validated at *both* authoring and binding time, per-user pins as
      twelve-column grid tiles, and the catalog/grid/tile UI. No pin tool for
      the agent, on purpose: the agent authors, the user curates.
- [x] **Typed workflow inputs** — `services/workflows/inputs.py`, declared on
      `graph["inputs"]` with no migration, bound at run start before the run
      goes `running`, rejected rather than coerced, rendered as a form by the
      canvas and refused with the field named.
- [x] **The human-in-the-loop `manual` node** — parks a run for a person on the
      existing `AgentToolCall` park/resume seam. No new park mechanism, no new
      grammar, no migration. 13 tests.
- [x] **Files and folders** — a document folder tree, delete refused (409) never
      cascaded, plus the document editor's `text`/`markdown` kinds, inline
      proposal review, and one chat thread per document.
- [x] **The workflow canvas** — React Flow graph with hover-expand chips, a
      decision that leads the panel instead of trailing it, and measured framing.
- [x] **ADR 0009: generated apps get no backend** — the frame cannot call one
      without relaxing the renderer sandbox. Python, where genuinely required, is
      a precompute whose output becomes a dataset version, approved by code hash.

## Reconciliation performed this pass

- [x] **Migrations** — one head (`0030_document_folders`), chain `0001 → 0030`
      linear across three agents' additions. Applied from an *empty* database:
      57 tables, and a metadata diff against `models.py` shows zero missing
      tables, zero extra tables, zero column drift.
- [x] **Document editor ↔ folders fit** — `documents.tsx` composes `FileTree`
      (folders) with `PendingEditList` (inline review); `workspace.tsx` wires
      `folderOps`, `pendingEdits` and `decidePendingEdit` together. Both e2e
      suites pass in the same run.
- [x] **Regression found and fixed** — a stray trim pass had deleted the "Live"
      pill and three pieces of empty-state copy from `activity.tsx`, and the
      model-provider `agent-pill` from the `workspace.tsx` header, leaving their
      CSS and their destructured props behind. Three separate track reports
      dismissed the resulting lint warnings as "pre-existing in another agent's
      file"; they were neither. Restored, and `pnpm lint` is now fully clean.
- [x] **Docs** — `docs/ARCHITECTURE.md` gains a "The shell" section describing
      the rail/settings split that NAV_GROUPS now encodes, plus dashboard
      templates and pins, typed workflow inputs, document kinds/folders/threads,
      and ADR 0009. `README.md`'s feature list no longer advertises the removed
      Apps and Sandbox destinations.

## Open — not done, not attempted

- [ ] **The document side chat has no UI.** `POST /api/documents/{id}/conversation`
      exists, is tested, and is in `openapi.json`, but `packages/api-client` has
      no method for it and nothing in `apps/web` calls it. The server half of
      that feature is complete and the browser half is absent.
- [ ] **Dashboard templates are agent-only.** The template routes and tools work;
      there is no template UI. Acceptable under "the agent authors", but it means
      a template cannot be inspected or corrected by hand.
- [ ] `services/dashboards/binding.py` imports `CompileError`/`CompileReport`
      from `..workflows.validate`. That is the one line to re-point if the
      workflow validation module is ever reorganised.
- [ ] Nothing here is committed. The tree carries all five tracks as working
      changes.
