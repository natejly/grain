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
