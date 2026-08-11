# ADR 0007 deferred half: policy scope + the workflow executor

## 1. Policy scope narrower than a workspace (do first)

- [ ] `ToolPolicy.scope` column ("chat" | "workflow"); unique key becomes
      (workspace_id, tool_name, scope). Existing rows are `chat`.
- [ ] Migration `0020_tool_policy_scope` — rename/copy/drop, portable across
      sqlite and postgres (the old unique constraint is unnamed).
- [ ] `resolve_policy(..., scope=)` — scope is a **required** keyword, because a
      default would have to be the wide one.
- [ ] Rule: a workflow-scoped row decides; absent one, a chat-scoped `deny` still
      denies (a prohibition is not a grant); any other chat row is ignored.
- [ ] `_upsert_policy` in api/tools.py filters on scope (else the new unique key
      lets a chat "always allow" overwrite a workflow grant).
- [ ] Tests: chat allow does not authorise an unattended node, and that is the
      default; chat deny carries; workflow allow does not leak back into chat.

## 2. The executor

- [ ] `services/workflows/executor.py` — topological walk over `graph_json`.
- [ ] Resume skips committed nodes (UNIQUE(workflow_run_id, node_key)).
- [ ] A node interrupted mid-write is NOT re-run: the run fails loudly.
- [ ] Every tool call: substitute refs -> re-check against the tool's schema ->
      `resolve_policy(scope="workflow")` -> `_execute_call`. No bypass.
- [ ] `ask` parks: AgentToolCall(proposed) on the backing Run, resumed by the
      existing `POST /api/agent-tool-calls/{id}/decision`.
- [ ] Agent nodes run through `run_agent_turn` on the same backing Run.
- [ ] Failure isolation: one node fails -> downstream skipped, run `failed`.
- [ ] Re-validate the stored graph against the live registry at every advance.

## 3. Routes + ticker

- [ ] `api/workflows.py`: compile, CRUD, run, list runs, run detail, tick.
- [ ] `POST /api/workflows/tick` — claim-based, shared-secret, inert when the
      secret is unset. Migration adds `workflows.last_dispatched_at`.
- [ ] `services/workflows/schedule.py` — cron matcher + timezone + claim.
- [ ] A `RouteCase` per route in `tests/isolation.py`, plus tenant rows.
- [ ] Update ADR 0007's "what is not built".

## Review

All three landed. Every box above is done except the two deferrals recorded in
the ADR postscript.

**Policy scope.** `tool_policies.scope` (`chat` | `workflow`), unique key widened
to include it, migration `0020_tool_policy_scope` rebuilding the table because
the old constraint was declared inline and has no portable name to drop. Every
pre-existing row backfills to `chat`, verified against a seeded database:
upgrade preserves the id and the verdict, two scopes coexist, the new key bites,
downgrade keeps the chat row and drops the workflow one.

`resolve_policy` keeps a single decision point and takes a `scope` with no
default. The subtle part was not the column: it was that an **agent node inside a
workflow** would otherwise resolve at chat scope and inherit the exact standing
grant the split exists to withhold — ADR 0007's injection scenario lands on that
node. `policy_scope_for_run` reads the scope off the run rather than off which
loop is executing, which fixes it without threading a parameter through
`services/runs.py`.

**Executor.** `services/workflows/executor.py` plus `refs.py`. Resume is proved
by killing a run with a BaseException mid-DAG and restarting it; the completed
node's tool records one call across both executions. One rule the ADR did not
state was added: a node found `running` on resume is retried only if its tool is
read-only, because re-sending an email that may already have gone is the failure
"at least once is the wrong number" was warning about.

**Ticker.** Implemented rather than left undispatched — `POST /api/workflows/tick`
with a shared secret, a compare-and-swap claim, and 503 when unconfigured, so the
ADR's warning stays true of any deployment that has not turned it on.

Gates: ruff clean, mypy 105 files, 1243 passed + 1 skipped + 10 xfailed (baseline
1059 + 10 isolation cases + 58 new workflow tests + the other agent's stress
suite), openapi exports with 7 new paths and 10 new schemas and nothing removed,
alembic upgrades to 0020 from empty.

Two things worth knowing that are written up in the ADR postscript rather than
hidden here: nothing recovers a workflow run whose process died mid-node (the
guard added to `run_agent_turn` stops the chat recovery sweep from mangling one,
but does not resume it), and scope narrows *where* a standing allow applies, not
how long it lasts.

---

## Workflow run recovery, and a confirm() on document deletion

Two independent jobs, run alongside another agent's token-accounting work.

### 1. Recovery of a workflow run whose process died

Closed the deferral the ADR 0007 postscript recorded. The old note called it "a
small change to `services/recovery.py`", and the interesting part is why it is
not: a chat run recovers by *replaying its turn*, and a workflow run has to
resume from its first incomplete node. So recovery claims a run and then calls
`advance_run` — the ordinary walk, started from the ordinary place — which is
what makes every rule the executor already enforces keep applying to a recovered
run without being restated.

- `executor.recover_workflow_runs()` claims and resumes; `claim_orphaned_runs()`
  claims only, so `POST /api/workflows/tick` can claim on the request's session
  and run the graphs on a background task. Called from `recover_durable_work` at
  boot **and** from the tick, because recovering only at process start leaves a
  run orphaned on a long-lived box waiting for the next deploy.
- Parked runs are excluded by status. A park clears the backing run's lease on
  purpose, so a lease-only sweep would have resumed every approval in the inbox.
- The read-only/write rule is inherited, not re-implemented. An interrupted
  write still ends the run with `node_interrupted`.
- Bounded twice: `MAX_NODE_ATTEMPTS = 3` on the `attempt` column that already
  counted interruptions, and `RECOVERY_MAX_AGE = 12h` as the bound of last
  resort for a crash in `_prepare` that leaves no node row at all.
- The claim is a conditional UPDATE on the backing run's `lease_expires_at` —
  the same lease `runs.run_lease_seconds` describes for chat — renewed per node
  so a long graph does not outlive its own claim. A run with no backing row is
  claimed on status behind one lease of quiet, so the sweep cannot race the
  `BackgroundTask` that `POST /run` just scheduled.
- `recover_durable_work` now excludes workflow-backed runs from the chat sweep.
  `run_agent_turn`'s guard stopped a stray assistant message but raised, and
  `process_run` records exceptions by failing the run — the guard was protecting
  the conversation by destroying the record of the automation.

Eight tests: a run killed mid-DAG recovers with the completed node's tool
recording exactly one call across both executions; recovery refuses to repeat an
interrupted write; a parked run is untouched; a second claim returns nothing; a
fresh queued run is not stolen; both bounds fire; the chat sweep leaves the
backing run alone; and the tick picks an orphan up.

### 2. `confirm()` on document deletion

Added in `handlers/documents.ts`, matching `removeSource`. Then the hygiene
sweep the job asked for. Three `page.once("dialog", …)` handlers were armed for
dialogs that never appear — `features.spec.ts` (board, project) and
`navigation.spec.ts` (board) — because **boards and projects are not
confirm()-gated either**, contrary to the brief. Left the arms off with a
comment rather than changing two more views' UX unasked; flagged for a decision.
`workflows.spec.ts` gained the arm it now needs.

### Gates

ruff clean, mypy 107 files, pytest 1333 passed / 1 skipped / 3 xfailed with one
failure that reproduces with these changes stashed (`test_doc_pending.py::
test_approving_a_listed_edit_applies_it_and_finishes_the_run`, the other agent's),
vitest 181, playwright 32.

---

## Spend ceiling (ADR 0008)

### Plan

- [ ] `config.py`: BUDGET_WINDOW_HOURS / BUDGET_USD_PER_WINDOW / BUDGET_TOKENS_PER_WINDOW /
      UNATTENDED_BUDGET_FRACTION; boot guard refusing a USD ceiling with no prices and no
      token ceiling.
- [ ] `models.py`: `workspace_budgets` (owner-settable per-workspace ceiling), and
      `paused_reason` on `runs` + `workflow_runs`.
- [ ] alembic 0023.
- [ ] `services/budget.py`: the single enforcement predicate. Never raises.
- [ ] `agent_loop`: check immediately before the model step; park (not kill) with
      `paused_reason="budget"`; `resume_after_budget`.
- [ ] `workflows/executor.py`: mirror the reason onto the workflow run; handle the new outcome.
- [ ] `services/runs.py`: `resume_run_after_budget`.
- [ ] `api/admin.py`: GET/PUT `/api/admin/budget`, releasing runs the raise unblocks.
- [ ] schemas: `paused_reason` on RunOut / WorkflowRunOut; `.env.example`.
- [ ] `docs/adr/0008-spend-limits.md`.
- [ ] tests + isolation RouteCases + mutation check.

---

## Model spend panel (web) — done

Reaches `GET /api/admin/usage`, which nothing read until now.

- [x] `packages/api-client`: `AdminUsage*` types + `getAdminUsage(days)` (additions only).
- [x] `components/views/usage-format.ts`: the honesty rules and every number format,
      away from JSX so a test can hold them still. 25 vitest cases.
- [x] `components/views/admin-usage.tsx`: banner → totals → costliest runs → three
      breakdowns, full width at the top of the admin grid.
- [x] `admin.tsx`: usage joins the existing `Promise.all`, so the window selector and
      Refresh share one fetch path and one 403 handler. Costly runs are named by
      joining `top_runs.run_id` against the Runs panel's prompt previews — the ledger
      stores no text by design.
- [x] `globals.css`: `.usage-*`, tokens only.
- [x] `e2e/usage.spec.ts`: 4 specs. Creates nothing, deletes nothing, arms no dialog.

The rule, which is the point: `MODEL_PRICES` ships empty, so `pricing_configured` is
false and every `cost_usd` sums to 0 while tokens are real. No component formats money
itself; `readSpend` answers "Not priced" (unknown), "$x+" (floor) or "$x" (exact), and
the panel never prints `$0.00` for a figure it did not measure. Bars are drawn from
tokens unless every call in the window is priced, and the panel says which.

### Gates

tsc clean, eslint clean, vitest 206 passed (181 → +25), next build clean,
playwright 36 passed (32 → +4). Screenshots reviewed in light and dark, empty and
populated: `test-results/usage-*.png`.

### What landed

The design question the job asked to be decided deliberately — kill or park —
is answered **park**, and the ADR argues it from state rather than from
politeness: an agent three tool calls into a turn has created a document, moved
a card and sent a message, and raising on the fourth leaves all three performed
with the turn recorded as `failed`. `process_run` records exceptions by failing
the run, so a budget exception would destroy the record of the automation in
order to protect the invoice — the same shape of mistake the workflow-recovery
work already found and fixed once.

- **The park is the existing park.** `waiting_for_approval` with a new
  `runs.paused_reason` column, *not* a new status. Six guards already read
  `waiting_for_approval` as "waiting on a person" and are already right about a
  budget park — `RECOVERABLE`, `TERMINAL_RUN_STATES`, the recovery sweep, the
  SSE close condition, the cancel fast-path, the memory writer. A new status
  makes correctness opt-in at seven sites; a column makes it inherited.
  `workflow_runs` carries the reason too, because the workflow surface reads
  that table.
- **One predicate.** `budget.exceeds(ceiling, spend)` is pure. `_advance` calls
  `budget.evaluate` as the last statement before `step(...)`, every iteration —
  a runaway loop is a turn whose *sixth* step is the one worth refusing.
- **Unpriced spend is never waved through.** A dollar ceiling with unpriced
  calls in the window and no token ceiling beside it evaluates to *stop*. Not
  because the workspace is over, but because it was asked to tell and cannot.
  `Settings._guard_budget` refuses to boot the common shape of that mistake.
- **Unattended work is checked twice**, against its own spend and half the
  ceiling. Relative, so a deployment that configured nothing still has nothing.
- **`PUT /api/admin/budget` raises and releases in one gesture**, re-asking the
  same predicate per run, so a raise that is still not enough releases nothing.
- Enforcement is at the agent loop and nowhere else. Embeddings, memory/graph
  extraction, compile and codegen still *count* toward the ceiling and are not
  gated by it — each is one call caused by one human action, and failing one
  destroys work rather than deferring it. Stated in the ADR, not left to be found.

### Mutation check

Replaced the body of `budget.exceeds` with `return ""` (always permit) and ran
the suite: **15 failed, 1352 passed** — six predicate/evaluate tests, seven
in-loop enforcement tests, and both workflow tests. Restored; back to 1368
passed. The predicate is load-bearing in every test that claims to exercise it.

### Gates

ruff clean, mypy 108 files, pytest 1368 passed / 1 skipped / 3 xfailed (was
1335), openapi exports with 1 new path and 5 new schemas, alembic 0001->0023
from empty and 0023 down/up clean.

### Deferred, deliberately

- **No web surface.** `paused_reason` is on `RunOut`/`WorkflowRunOut` and the
  park emits `run.waiting_for_budget`, but nothing renders it. apps/web and
  packages/api-client belong to another agent this session; a budget-parked run
  currently shows as parked with no card.
- **No alerting.** A 3am ceiling writes an event and stops a run; it emails
  nobody.
- **Tool spend is not bounded**, only model tokens.
- **The workspace row replaces the deployment ceiling rather than clamping it.**
  Correct for a workspace whose owner pays the bill; a hosted multi-tenant
  deployment wants `min()` in `budget.effective_ceiling`, and the ADR says so.
