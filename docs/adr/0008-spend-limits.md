# 0008 — A spend ceiling that parks the run instead of killing it

## Status

Accepted, implemented.

## Context

ADR 0007 built an orchestration platform, and the commit that landed
`model_usage` made what it spends visible. Visible is not bounded. Today a
workspace can spend an unbounded amount of money in an unbounded number of ways,
and the only thing standing between a deployment and a five-figure invoice is
somebody happening to look at `GET /api/admin/usage`.

Two shapes of runaway, and they are not the same problem.

**An agent loop that will not stop.** `MAX_ITERATIONS = 6` bounds the steps in
one turn, and bounds nothing about their size: six calls with a 200k-token
context on a reasoning model is real money, and the loop has no idea. It is
attended — a person is watching the stream — so it is the less dangerous of the
two.

**A schedule that fires forever.** `POST /api/workflows/tick` dispatches every
due workflow every minute. A cron that fans out an agent node per row of a
growing table, at 3am, with nobody at the diff, is the case that turns a bug into
a bill. ADR 0007 already treated "nobody is watching" as a first-class property
for *authority* — `policy_scope_for_run`, the `workflow` policy scope, the
refusal to let a chat "always allow" authorise an unattended write. Spend is the
same property viewed through a different lens, and it deserves the same
treatment.

Two facts about this codebase shape everything below.

*`MODEL_PRICES` ships empty.* ADR-adjacent, and it is the single most important
constraint here. The accounting commit refused to guess a rate, deliberately: a
`model_usage` row for a model with no configured price records every token and a
**null** cost. So in the shipped default configuration, a dollar ceiling is a
number that no amount of spending can ever reach.

*There is exactly one mechanism for "stop and ask a human".* The approval park:
`_park_for_approval` writes the loop state to `runs.agent_state_json`, sets
`waiting_for_approval`, clears the lease, emits events, records audit, and
`POST /api/agent-tool-calls/{id}/decision` brings it back. It is durable across a
process death, excluded from the recovery sweep on purpose, and rendered in the
product.

## The decision that had to be made deliberately: park, not kill

What happens when a run crosses the line mid-turn?

**Killing it is the wrong answer, and the reason is state, not politeness.** A
turn is not a request; it is a sequence of side effects with a record at the end.
An agent three tool calls into a turn has created a document, moved a card and
sent a message. Denying its fourth call by raising leaves every one of those
writes performed and the turn recorded as `failed` — the user sees an error, the
model never got to say what it did, and there is no artefact that says "this
half-happened". Worse, `process_run` records exceptions by failing the run, so a
budget exception would destroy the record of the automation in order to protect
the invoice. That exact failure mode was found and fixed once already, in the
workflow-recovery work: a guard that protected the conversation by destroying the
record.

**Parking is the right answer, and almost none of it had to be built.** A run
parked on budget is a run waiting on a person, which is a state this system
already models completely:

- the loop state is serialized, so the turn resumes rather than restarts;
- the lease is cleared, so no recovery sweep steals it;
- `RECOVERABLE` already excludes it, so no tick resumes a graph nobody released;
- `TERMINAL_RUN_STATES` already excludes it, so the SSE stream stays open;
- `chat.py`'s cancel fast-path already handles it;
- `memory.py` already declines to write memory for it.

Every one of those is correct about a budget park *without being told*, because
each was written about the concept "waiting on a person" rather than about
approvals specifically.

**And the question the ceiling asks genuinely is an approval question.** "This
workspace has spent its budget; do you want to spend more?" is not an error. It
is a decision with an owner, and the product already knows how to hold a run
while a human makes one.

### The park is the same park, and the reason is a column

A budget park sets `runs.status = 'waiting_for_approval'` and
`runs.paused_reason = 'budget'`. It does **not** introduce a new status value.

The alternative — a `waiting_for_budget` status — was the obvious design and is
worse. It would mean editing each of the six guards listed above, plus
`WorkflowRun` and `WorkflowNodeRun`'s vocabularies, and it would be wrong at
every one of them that was missed. A new status makes correctness opt-in at
seven sites; a new column makes it inherited at seven sites and explicit at the
two that actually need to tell the parks apart. `paused_reason` is also carried
on `workflow_runs`, because the workflow surface reads that table and a graph
that says "waiting for approval" with no card to click sends its owner hunting
for a button nobody wrote.

The two parks differ in what they leave behind, and that is deliberate: an
approval park writes an `AgentToolCall(status='proposed')`, and a budget park
writes none. There is no proposed call — the model had not been asked yet — so a
card here would approve nothing, and a decision endpoint that accepted one would
resume a run whose ceiling is still exceeded.

## Decision

**A rolling per-workspace ceiling on model spend, evaluated by one predicate,
immediately before every model step in the agent loop. Over the line, the run
parks; an owner raises the limit and it resumes.**

### One predicate, one call site

`services/budget.exceeds(ceiling, spend)` is pure: a limit, a measurement, and
no I/O. `budget.evaluate` wraps it with the two ledger reads. `agent_loop._advance`
calls it as the last statement before `step(...)`, on every iteration — because a
runaway loop is precisely a turn whose *sixth* step is the one worth refusing,
and a check outside the loop would wave it through.

Before, not after. A post-hoc check can only record the overspend it failed to
prevent.

### The ceiling is gates on iteration, not a tax on every call

The check is in the agent loop and nowhere else. That is a line, and it is drawn
where the unboundedness is: the agent loop and the schedule tick that feeds it
are the only things in this app that can decide, by themselves, to spend again.

Not gated, and this is stated plainly rather than left to be discovered:
embeddings during ingestion, memory and graph extraction, `workflow_compile`, and
codegen. Each of them is one model call caused by one human action, bounded by
the action rather than by a loop, and each of them destroys work when it fails
rather than deferring it — an ingest that half-embedded a document is a worse
outcome than the tokens it saved. **They all still count toward the ceiling**;
their spend is in the ledger and it is what stops the next agent turn. The
ceiling counts everything and stops the one thing that can spend without limit.

### Tokens are a first-class ceiling, and unpriced spend is never waved through

Two limits, both optional, both per window: `BUDGET_USD_PER_WINDOW` and
`BUDGET_TOKENS_PER_WINDOW`.

The rule for unpriced models is the sharpest thing in this document:

> If a dollar ceiling is configured and the window contains calls with no
> configured rate, and no token ceiling is configured, the verdict is **stop**.

Not because the workspace is over — nothing here can tell whether it is — but
because it was asked to tell and cannot. **Opting into a ceiling is opting into
being stopped, including when we cannot tell. Silence is not permission.** The
alternative is the failure this whole feature exists to prevent: an operator
configures a $50/day limit, sees it in the panel, stops watching, and the limit
does nothing at all because `cost_usd` is null on every row.

Two things make that livable rather than obnoxious. `Settings._guard_budget`
refuses to boot a deployment with `BUDGET_USD_PER_WINDOW` set, `MODEL_PRICES`
empty and no token ceiling — the same structural gate `_guard_model_provider`
and `_guard_sandbox` already use, and it catches the common shape before any
traffic. And the runtime message names all three ways out: price the models, add
a token ceiling to bound what cannot be priced, or drop the dollar ceiling and
stop believing there is one.

A token ceiling is the honest answer to "what does a budget mean when cost is
unknown", and it is the one to reach for in an unpriced deployment: it works with
no price list at all.

### Unattended work is held to a fraction of the ceiling

A workflow node is checked twice: against the workspace ceiling over all spend,
and against `UNATTENDED_BUDGET_FRACTION` (default 0.5) of that ceiling measured
over unattended spend alone. A chat turn is checked once.

Two ledgers rather than one threshold, because the two failures are independent.
A busy afternoon of human conversation must not stop tonight's report, and a
scheduled DAG in a loop must be stopped even in a workspace where nobody else has
spent a cent.

The tighter default is *relative*, and that is what lets it exist at all. There
is no absolute default cap for automations, because there is no absolute default
cap for anything — a deployment that configured nothing still has no ceiling.
Half of unlimited is unlimited.

Which runs count as unattended is decided by `policy_scope_for_run`, the same
split ADR 0007 made for tool policy and for the same reason: "nobody is watching"
is a property of what started the run, not of which loop is executing inside it.
A manually started DAG counts too — it fans out the same way once it is running.

### Raising the limit and resuming is one gesture

`PUT /api/admin/budget` (owner only) writes the workspace's ceiling and then
releases every run parked on budget that the new ceiling no longer stops,
reporting which. Splitting them would leave an owner who has fixed the problem
staring at a still-parked run, hunting for a second button.

The interesting case is not a special case: `budget.evaluate` is asked again per
run, from the same module the loop enforces from, so a raise that is still not
enough releases nothing. `resume_after_budget` does not re-check either — it
hands the restored state back to `_advance`, which checks the ceiling at the top
of every iteration anyway. One rule, one place.

The workspace row **replaces** the deployment's ceiling rather than narrowing it.
This is the one place the design chooses self-service over operator control, and
it is chosen for a concrete reason: a limit whose only escape hatch is a redeploy
is one nobody can use at the moment it matters, and `require_owner` in this
product is the person paying the bill. A hosted multi-tenant deployment that
needs a cap its tenants cannot lift clamps the two numbers in
`budget.effective_ceiling` with `min()` against the settings values. That is the
only line that changes, and it is called out here so the change is a decision
rather than a discovery.

### Failing to read the ledger allows the call

`evaluate` never raises; its failure direction is *allow*, and it says so in the
log. This is a spend control, not a security control. The worst case of failing
open is an invoice; the worst case of failing closed is a product that stops
working because a `SELECT` timed out. It is the same rule `record_model_usage`
follows — accounting never breaks a turn — and a ceiling built on top of
accounting does not get to break the rule its foundation keeps.

## Consequences

- **A workspace can park itself and need an owner to unpark it.** That is the
  feature, but it is worth stating as a cost: with a tight ceiling and a chatty
  workspace, a person mid-conversation gets stopped and cannot self-serve unless
  they are an owner. The event carries the numbers and the fix, so at least the
  message is actionable.
- **The unpriced rule can fire on a partially priced deployment.** Price
  `gpt-5.5` but not `text-embedding-3-small`, set a dollar ceiling, and the first
  embedding in the window parks the next agent turn. That is correct — the
  ceiling genuinely cannot see that spend — but it is surprising, and the fix
  (price the embedding model, or set a token ceiling) is one line in `.env`.
- **Every model step now costs an indexed lookup, and a ceilinged one costs an
  aggregate.** A workspace with no ceiling reads one `workspace_budgets` row and
  stops; the `model_usage` aggregate — bounded by the window, on
  `ix_model_usage_workspace_created` — happens only once a limit exists to
  compare against, and twice for an unattended turn. If that ever matters the
  answer is a short-TTL cache of the window sum, not a looser check.
- **The web does not yet render any of this.** `paused_reason` is on `RunOut` and
  `WorkflowRunOut` and the park emits `run.waiting_for_budget`, but the chat
  banner keys off event types it does not know and the workflow status label map
  has no entry for a budget park. Until the web is updated, a stopped run shows
  as parked with no card — the API is complete and the surface is not.
- **Nothing is enforced for tool spend.** This ceiling counts model tokens.
  A workflow of pure tool nodes calling a metered third-party API is bounded by
  nothing here, and calling this a "spend ceiling" rather than a "model spend
  ceiling" would overclaim.
- **No alerting.** Reaching a ceiling writes an event and an audit row and stops
  a run; it emails nobody. For a scheduled workflow at 3am, the owner finds out
  when they look — which is better than finding out from the invoice, and worse
  than being told.
