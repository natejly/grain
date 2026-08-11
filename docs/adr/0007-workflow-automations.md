# 0007 — Workflow automations: compile with a model, execute on the run loop

## Status

Accepted, partially implemented. This pass lands the decision, the schema and
the compiler. The executor is specified here and deliberately not built yet;
"What is not built" says exactly where the line is.

## Context

The ask is ordinary and the failure mode is not: *describe an automation in
English, get a graph, run it on a schedule.* "Every Monday, pull the open PRs,
summarise them, post to Slack." The obvious move is LangGraph, and the obvious
move deserves a harder look than usual here, because this codebase already
contains most of what a workflow runtime is for.

What exists, before any of this:

- **A pausable, resumable state machine.** `services/agent_loop.py` serialises
  `LoopState` into `runs.agent_state_json`, and a turn ends as `Done`, `Paused`
  or `Cancelled`. `pending_calls` models undrained tool calls specifically so a
  resumed turn cannot emit a `function_call` with no matching output — a subtle
  invariant that took real work to get right.
- **An approval gate with teeth.** `resolve_policy` reads `tool_policies` with a
  fallback to `ToolSpec.read_only`. A tool that resolves to `ask` writes an
  `AgentToolCall(status="proposed")`, parks the run at
  `waiting_for_approval`, and resumes through
  `POST /api/agent-tool-calls/{id}/decision`.
- **Durable resumable streaming.** `RunEvent` rows with a per-run sequence, a
  `Last-Event-ID` cursor, and a reader that can rejoin a run mid-flight.
- **Tenancy that is tested, not asserted.** Every table carries `workspace_id`,
  every query filters on it, and `tests/isolation.py` enumerates every route as a
  cross-tenant probe — currently ~130 cases, with a coverage test that fails when
  a route is added without one.

A workflow engine is: a graph, a scheduler, durable state, resumption, and
human-in-the-loop interrupts. Four of those five already exist here, built
against this product's tenancy and approval model. The question is not "is
LangGraph good" — it is good — but "what happens when a second orchestration
runtime, with its own durability model, is placed beside one that already
enforces guarantees we are unwilling to lose."

## The options, judged

**(a) LangGraph as the executor**, bridged to the run/event/approval machinery.
**(b) A small DAG executor** on the existing machinery, borrowing LangGraph's
ideas but not its runtime.
**(c) LangGraph for authoring/validation only.**

### Does an approval still park and resume correctly?

This is the question that decides it. LangGraph has a genuinely good answer of
its own — `interrupt()` suspends a node and `Command(resume=...)` continues it,
persisted through a `BaseCheckpointSaver`. It is close enough to our park/resume
that the resemblance is the trap.

Under (a), a workflow that parks has to satisfy two systems at once. The
approval must appear as an `AgentToolCall` row, because that is what the
existing decision endpoint, the approval inbox and the audit trail all read. And
LangGraph must hold a checkpoint it can resume from, because that is how its
graph continues. So `POST /api/agent-tool-calls/{id}/decision` becomes a
translator: resolve our row, then reach into a checkpointer and issue a
`Command(resume=...)` against the right thread and the right task id. Two state
machines now have to agree on what "parked" means, and the moments they can
disagree are exactly the moments that matter — a crash between our commit and
LangGraph's checkpoint write, a decision arriving twice, a run cancelled while
suspended. `pending_calls` exists in `agent_loop.py` because that class of
disagreement already bit us once with a single state machine.

Under (b) the question does not arise. A node that needs approval calls
`_park_for_approval`, writes an `AgentToolCall`, and the *existing* endpoint
resumes it. There is one park, one resume, one row.

### Does a node failure resume without re-running completed nodes?

Both can. The difference is where the answer lives. LangGraph resumes from a
checkpoint — an opaque, versioned blob written at superstep boundaries. Our
answer is `workflow_node_runs` with a unique constraint on
`(workflow_run_id, node_key)`: "which nodes finished" is a row set you can
`SELECT`, show in a UI, and reason about in a code review. For a system whose
nodes send email and open pull requests, "at least once" is the wrong number, and
a durable record that a specific node already ran is worth more than a general
mechanism that also produces one.

### Is every node's tool call workspace-scoped and policy-gated?

Under (b), by construction: the node executes through the same
`build_registry(db, context)` → `resolve_policy` → `_execute_call` path a chat
turn uses. There is no second execution path to audit.

Under (a) this is where the cost is least visible and largest. LangGraph's
checkpointer interface is keyed on `config["configurable"]["thread_id"]` — a
string. Nothing in the interface carries a tenant. A workspace-scoped
checkpointer means encoding the workspace into a thread id and trusting that
encoding, which moves an isolation guarantee from a `WHERE workspace_id = ?` that
130 tests exercise onto string formatting that none of them can. That is a
downgrade in the one property this product is most willing to spend on.

### What does it cost in dependencies and in concepts?

Measured, not estimated. `langgraph` installs cleanly on this Python 3.12.11 and
imports fine — installability is not the objection. Against **this** virtualenv
it resolves to **17 packages**:

```
jsonpatch, jsonpointer, langchain-core, langchain-protocol, langgraph,
langgraph-checkpoint, langgraph-prebuilt, langgraph-sdk, langsmith, orjson,
ormsgpack, requests-toolbelt, tenacity, uuid_utils, websockets, xxhash, zstandard
```

Two details in that list are worth more than its length.

**It downgrades a package we already have.** `langgraph-sdk` pins
`websockets<16,>=14`; this environment runs `websockets==17.0.1` (via
`uvicorn[standard]`). Adopting LangGraph moves our ASGI server's websocket
implementation back two major versions to satisfy a graph library's *client
SDK*, which we would not otherwise use at all.

**It brings a telemetry client for a third-party SaaS.** `langsmith` is a hard
dependency of `langchain-core`, and its purpose is shipping traces — prompts,
tool arguments, outputs — to LangSmith. It is off unless configured. But ADR 0005
went to the trouble of asserting that *nothing* from `Settings` is forwarded into
a sandbox, and wrote a test that fails if a new `SecretStr` field ever leaks.
Adding a library whose resting posture is "trace to our cloud when an environment
variable says so" is a step away from that stance, and the step is invisible in a
diff that just says `+ langgraph`.

The conceptual cost is the larger one. A reader of this codebase currently holds
one execution model: a run, its events, its tool calls, its approvals. Option (a)
asks them to hold that *and* channels, reducers, supersteps, checkpoint tuples,
`Command`, thread ids, and the mapping between the two. For a feature whose
graphs are a dozen nodes executed once in topological order.

### The honest case for LangGraph

It is real, and it is about a product we do not have yet. LangGraph earns its
keep when graphs have cycles, when supersteps fan out over hundreds of items with
channel reducers merging the results, when several agents pass control between
each other, and when time-travel debugging over checkpoint history is how you
find out what went wrong. If workflow automations grow in that direction — and
"multi-agent" is the most plausible direction they grow — this decision should be
revisited rather than defended. What makes it safe to defer is that the
compiler, which is most of the work and all of the differentiation, is executor
agnostic: it emits a JSON document, and a document is as easy to walk into a
`StateGraph` as into our own loop.

## Decision

**(b). Build the DAG executor on the existing run/event/approval machinery. Do
not adopt LangGraph — not as the runtime, and not for authoring either.**

Option (c) is rejected for a separate reason: LangGraph's validation is
*graph-shape* validation, and graph shape is the easy half. The checks that
matter here — this tool exists in *this workspace's* registry, these arguments
satisfy *that tool's* JSON Schema, this reference points at an upstream node —
are entirely Jasmine-specific, and a dependency that does not perform them is a
dependency carried for `add_edge`.

What we take from LangGraph is its design, not its code: a checkpoint boundary
between steps, an interrupt that suspends rather than fails, and state that flows
along edges instead of through globals.

### The graph is a document, and the compiler is the product

`services/workflows/dag.py` defines the grammar: nodes, edges, a trigger. Two
node kinds, and the difference is a review property rather than an
implementation detail. A **tool** node names a tool and its arguments — a reader
can see exactly what it will call. An **agent** node hands a prompt to the
existing agent loop, which picks its own tools at run time — a reader cannot.
Both are policy-gated identically; only one is statically reviewable.

Data moves between nodes through a closed reference syntax, `{{ node_id.output }}`
and `{{ input.field }}`, and nothing else. Not an expression language. A
workflow is a stored program a scheduler may run with nobody watching, and the
smallest thing that carries a value between steps is the one with the least to
audit.

`services/workflows/validate.py` is where the feature's central claim lives:
**a model that hallucinates a tool must fail at compile time, not at 3am
mid-run.** Every check runs and every finding is collected, because the caller's
next move is either to show a person the whole list or to hand it back to the
model as a repair prompt, and both are worse one error at a time.

- ids are slugs, unique, and may not claim the reserved `input` namespace
- edges name declared nodes; no self-edges, no duplicates
- the graph is acyclic, by Kahn's algorithm, and a cycle names the stuck nodes
- **every tool exists in this workspace's registry** — `build_registry(db, context)`,
  the same mapping the executor will use, not a static list. MCP servers,
  database connections and integrations all contribute to it, so a tool that
  exists for one tenant genuinely does not exist for another, and the validator
  is correct about that for free.
- arguments type-check against the tool's own published JSON Schema
- **every reference points upstream.** A node reading `{{ later.output }}` passes
  every other check and reads an empty value at run time. This is the check that
  catches it, and it is ancestry rather than adjacency, so reaching through an
  intermediate node is legal.

A value that is *purely* a reference cannot be typed at compile time — only the
upstream node knows — so it is deferred rather than guessed at. A reference
embedded in a larger string is a string at run time and is checked as one. The
distinction is one regex and it is the difference between false rejections and
missed ones.

Errors block; warnings do not. `tool_unknown` is a broken program. A tool whose
*own* schema is malformed — which an MCP server can ship and the workflow author
cannot fix — should not make the author's automation uncompilable; it should make
the fact that its arguments went unchecked visible.

One bounded repair pass sits around the compile. Structured-output models fail on
the long tail — a tool name short an `s`, an edge to a node they renamed, a cron
with six fields — and every one of those is mechanically describable. Handing the
model the `CompileError` list recovers most of them. The ones it cannot recover
still fail closed. What never happens is a graph reaching the database because we
stopped checking.

### Approvals: a workflow is not a way around the gate

**Every node executes through `resolve_policy` unchanged.** No workflow-specific
policy path, no "trusted workflow" flag, no bypass for scheduled runs. A
`read_only=False` tool inside a workflow parks exactly as it parks in chat, by
writing an `AgentToolCall(status="proposed")` and suspending the workflow run at
`waiting_for_approval`.

The alternative — scoping workflows read-only and refusing write tools at compile
time — was considered and rejected. It is safer and it is also useless: "post to
Slack" is a write, and an automation product that cannot complete its own
canonical example is not a product. Worse, it would push authors toward agent
nodes with a prompt like "post this to Slack", which is the same write with less
review surface.

So the answer is the roadmap's (§7 #10): **unattended runs park at the first
write and land in an approval inbox**, using the existing path verbatim. A
scheduled workflow that only reads runs to completion at 3am. One that writes
gets as far as the write and waits for a human, which is the correct behaviour
when there is no human at the diff.

Two consequences follow into the schema. `workflow_runs.trigger` records
`manual` or `schedule`, because "nobody was watching" is a fact the audit trail
should carry rather than infer. And `workflow_node_runs.policy` records *what
authorised each node* — `allow` because the tool is read-only or the workspace
granted a standing permission, or `ask` because a human decided on this specific
call. Without that column the trail cannot tell an unattended 3am write from an
approved one, and those are very different events.

Compilation itself grants nothing. It writes no `tool_policies` row and creates
no run. A compiled workflow is a proposal.

### Schema

Migration `0019_workflows`, three tables, all `workspace_id`-scoped.

- **`workflows`** — the definition. `graph_json` is the compiled DAG and
  `source_prompt` is the sentence it came from; both are kept because they answer
  different questions, and a recompile that drifts from the ask is only visible
  when the ask survives. `version` bumps on recompile.
- **`workflow_runs`** — one execution of one workflow *version*, deliberately
  shaped like `runs`: same status vocabulary, same `waiting_for_approval` park,
  and a nullable `run_id` pointing at the chat run that carries the approval
  record and the `RunEvent` stream. That nullable column is the whole
  integration.
- **`workflow_node_runs`** — per-node state, unique on
  `(workflow_run_id, node_key)`. That constraint is what makes "skip the nodes
  that already finished" a database fact rather than a convention: a resumed
  executor reads this table, and a node that succeeded cannot be inserted twice
  or executed twice.

## What is not built

Stated plainly so nothing here reads as shipped.

- **The executor.** Specified above, no code. The schema and the compiler are
  what this pass lands.
- **HTTP routes.** No endpoint creates, compiles, runs or lists a workflow yet,
  which is why `tests/isolation.py` gained no cases — there is nothing to probe.
  When routes land they need `RouteCase`s, and the coverage test will insist.
- **The schedule ticker.** `trigger_kind`, `schedule_cron` and
  `schedule_timezone` are stored and the cron is validated, but nothing dispatches
  one. A stored schedule is a recorded intent. The UI must not describe it as
  active until a ticker exists, because "it will run every Monday" is a promise,
  and a promise the system does not keep is worse than a missing feature.
- **Run-time reference resolution and coercion.** Compile-time deferral means
  `{{ x.output }}` in an integer field is unchecked until the executor
  substitutes it. The executor must *reject* a value that does not fit the
  schema, never coerce it.

## Consequences

- **The standing `allow` is the real hole, and workflows widen it.**
  `tool_policies` is workspace-wide. Someone clicking "always allow" on
  `send_email` in a chat has authorised every future scheduled workflow to send
  email unattended, forever, without seeing a workflow. This is pre-existing —
  the grant model has always been per-workspace-per-tool — but automation is what
  turns a convenience into a standing capability, because the thing exercising
  the grant is no longer a person typing. `workflow_node_runs.policy` makes it
  *visible after the fact*. It does not make it safe. The real fix is a policy
  scope narrower than the workspace — per-workflow grants, or an expiry on
  "always allow" — and it is not in this ADR.
- **An approval inbox nobody empties is a workflow that never runs; one everybody
  empties reflexively is a gate that does nothing.** A weekly workflow that parks
  every Monday and is approved every Monday trains its reviewer to approve
  without reading, which is exactly the habituation that made click-through
  consent worthless. The gate's value is inversely proportional to how often it
  fires. That argues for workflows with one write at the end rather than five
  throughout, and for approval cards that show a diff worth reading —
  `AgentToolCall.proposal_preview` already exists for this and workflow nodes
  must use it.
- **Agent nodes cannot be reviewed the way tool nodes can.** "What will this
  workflow do?" has a complete static answer for a graph of tool nodes and no
  static answer at all for one containing an agent node. Every call an agent node
  makes is still policy-gated, so the security property holds; the
  *reviewability* property does not. A UI that renders both node kinds the same
  way is lying about that, and the compiler's tool/agent split exists so it does
  not have to.
- **A graph is validated against a registry that changes underneath it.** A
  workflow compiled while an MCP server was connected names tools that vanish
  when it is disconnected. The executor must re-validate against the live
  registry at run start, and the failure is loud — `tool_unknown`. What is *not*
  detectable is an MCP server replaced by a different one keeping the same tool
  names with different behaviour. The stored graph looks identical and does
  something else. Pinning a server identity per node would close this and is not
  done.
- **Prompt injection composes with the standing grant, and that combination is
  the sharp edge.** A node fetches a document; the document contains instructions;
  a downstream agent node honours them. Every write it then attempts parks for
  approval — that is the containment, and it holds. But in a workspace that has
  granted a standing `allow` on the write tool, there is no park and therefore no
  containment. The two residual risks above are not independent, and a deployment
  running unattended workflows should treat "always allow" on a write tool as the
  decision that matters most.
- **`jsonschema` becomes a declared dependency.** It was already in the tree via
  `mcp`; `validate.py` imports it directly, and a direct import riding on someone
  else's transitive dependency breaks the day that dependency reorganises. Zero
  net install, one line of honesty in `pyproject.toml`.
- **Cron is validated but not scheduled, which is a UI hazard more than a
  technical one.** See "What is not built".
