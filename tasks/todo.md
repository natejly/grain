# Surface the spend ceiling (ADR 0008's open consequence)

The API parks a run on the ceiling and says so on `RunOut.paused_reason`,
`WorkflowRunOut.paused_reason` and a `run.waiting_for_budget` event. The web
reads none of it, so a parked run renders as parked with no card.

## 1. Contracts (packages/api-client, additions only)

- [x] `Run.paused_reason`, `WorkflowRun.paused_reason` — both already on the wire.
- [x] `AdminBudget*` types mirroring `AdminBudgetOut` / `AdminBudgetRequest`.
- [x] `getAdminBudget()` / `setAdminBudget()` — the PUT also releases parked runs.

## 2. Formatting rules, where a test can hold them still

- [x] `views/budget-format.ts` — parse the event payload (network data, so
      validate), and say the numbers through `usage-format`'s `readSpend`, never
      a second money formatter. Unpriced spend must not claim a precise figure.
- [x] `views/workflow-format.ts` — `runStatusLabel(status, pausedReason)` labels
      a budget park distinctly from an approval.

## 3. The surfaces

- [x] Chat: a `BudgetHold` panel, not a tool card — a budget park writes no
      `AgentToolCall`, so there is nothing to approve.
- [x] The way forward, reachable from the wall: raise the ceiling in a
      disclosure popover. Owner → the form; 403 → "ask an owner".
- [x] Workflows: budget label in the inbox, the run banner and the parked node,
      and the hold panel instead of an approval card with no call in it.
- [x] Admin: the ceiling beside the usage panel, editable, with what it holds.

## 4. Gates

- [x] vitest, tsc, lint, build, playwright — all pasted in the report.
- [x] Screenshots looked at, including day one: no ceiling, no usage.

## Review

Green: tsc, lint, vitest 233 (was 206), build, playwright 39 (was 36).

Three things worth carrying forward.

**The workflow half of this seam was blocked by an API bug, not by the web.**
`_run_out` in `apps/api/app/api/workflows.py` builds `WorkflowRunOut` field by
field and never copies `paused_reason`, so every workflow run on the wire
reports `""` — the schema declares the field with a default of `""`, which is
exactly why nothing noticed. Proven against the running API: the DB row says
`budget`, `GET /api/workflows/runs/{id}` says `""`. The web reads the field
first and falls back to `GET /api/admin/budget`'s `runs_parked_on_budget`,
which reports the truth. One line in the API retires the fallback.

**A cached "held" answer goes stale in the one moment that matters.** The first
fallback memoised per run id; a released run re-parks on an approval between
two 1.2s polls, so the memo never expired and the graph claimed to be held
forever. The fix is evidence, not caching: a budget park writes no
`AgentToolCall`, so a run *with* a proposed one is parked on a person, whatever
a list fetched a moment ago said.

**Known limit, deliberately not papered over.** The chat hold card lives as long
as the run's event stream. A reload loses it — there is no non-owner endpoint
that lists a conversation's parked runs, and inventing one is an API change this
task does not own. Admin lists them for owners.

## Follow-up: the three e2e failures the `_run_out` fix uncovered

Populating `paused_reason` on the wire ran the primary path for the first time
and exposed a real bug behind it. One root cause, three failures.

- [x] **Product bug.** `WorkflowRun.paused_reason` mirrors the backing run's,
      and `executor.resume_after_agent_turn` returned early on `Paused` without
      re-mirroring — true of the `Run`, false of the mirror. A graph released
      from the ceiling that walked on to a write kept `budget`, so the surface
      rendered the spend panel *instead of* the approval card and the proposal
      was on screen with no way to decide it. Taken from the outcome type, which
      already distinguishes the two pauses. Covered by
      `test_a_released_graph_that_parks_on_a_write_stops_saying_it_is_held_by_money`,
      mutation-checked: reverting the fix fails it with `'budget' == 'approval'`.
- [x] **The UI rule now outranks the field.** `resolvePausedReason` moved into
      `workflow-format.ts` with its precedence pinned by four unit tests: a
      proposed `AgentToolCall` is decisive, because a budget park writes none
      and a mirrored column can lag. Previously that check sat *below* the
      field, so it guarded only against a stale list.
- [x] **The other two failures were debris, not bugs.** budget.spec's third test
      failed before its inline cleanup, leaving a duplicate workflow
      (workflows.spec:136) and a workspace-wide `create_document` proposal
      (workspace.spec:191). `sweep` in an `afterAll` now undoes the ceiling, the
      proposal, the workflow and the conversation. Proven by injecting a
      deliberate failure: only budget.spec fails, 38 others pass. That injection
      also caught the sweep half-working — `DELETE /api/conversations` requires
      an `Idempotency-Key` the other two routes do not.

Gates: ruff clean, mypy 108 files, pytest `1395 passed, 1 skipped, 3 xfailed`,
vitest 237, eslint/tsc/build green, playwright `39 passed` three runs consecutively.
Screenshots read, not just captured: the released workflow now says "Waiting for
approval" on all four surfaces with a real Approve/Deny, and the Documents
pending panel holds exactly one card.
