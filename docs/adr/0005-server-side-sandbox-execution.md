# 0005 — Server-side sandbox execution in hosted microVMs

## Status

Accepted. Amends ADR 0004; does not supersede it.

## Context

ADR 0004 gave generated code a boundary with no workspace authority: an opaque
origin, `connect-src 'none'`, no cookies, no parent DOM. That boundary is a good
renderer and a poor computer. It cannot run Python, cannot install a package,
cannot open a socket, and cannot read a file the user uploaded. So the single
most common knowledge-work request — *"here is a spreadsheet, clean it up and
plot it"* — fails, and it fails in a way no amount of prompt work fixes.

The obvious reading is that ADR 0004 must be reversed. It does not. Read its
actual rationale: the risk it refused was **arbitrary code execution with
workspace authority**, and the property it bought was that generated code runs
somewhere that cannot reach our data or our infrastructure. A hosted microVM on
a third party's infrastructure preserves that property exactly. Code still never
executes on a Jasmine host, still holds no session, still cannot reach the
database. What changes is that the boundary now has a kernel.

So this is an amendment: ADR 0004 keeps the *rendering* sandbox, and this ADR
adds an *execution* sandbox beside it. They are different boundaries for
different jobs, and neither runs code on our servers.

## Decision

Execution happens in per-session Firecracker microVMs operated by E2B, reached
through a narrow provider interface (`app/services/sandbox/`).

- **A driver seam, not an SDK dependency.** `SandboxProvider` is a small
  Protocol. The E2B driver implements it; a `FakeProvider` implements it for
  tests. Nothing above the seam imports `e2b`. This is not vendor-neutrality
  theatre — it is what lets the entire tool layer, quota layer and approval path
  be tested without a network or a key.
- **Sessions are rows, and the row is the authority.** A sandbox's provider-side
  id is reachable only through a `sandbox_sessions` row selected by
  `workspace_id`. `resolve_session` is the sole function that turns a session id
  into a live handle, and it filters by workspace before it returns. There is no
  code path that accepts a provider id from a caller.
- **The sandbox environment is an allowlist, never a copy.** Nothing from
  `Settings` is forwarded. No `OPENAI_API_KEY`, no `DATABASE_URL`, no session
  or encryption key. A test asserts every `SecretStr` field on `Settings` is
  absent from the constructed environment, so adding a secret later cannot
  quietly leak it.
- **No egress, and packages are pre-baked instead.** The default policy is
  `none`. This is the decision that makes the rest of the design easy: with no
  route out, prompt-injected code has nowhere to send what it can read, and the
  feature's worst risk stops being a risk rather than being mitigated.
  It costs runtime `pip install`, so `infra/sandbox/Dockerfile` ships the
  scientific stack — numpy, pandas, matplotlib, scipy, scikit-learn, statsmodels,
  sympy, pyarrow, openpyxl, Pillow and friends — and that Dockerfile *is* the
  package policy. Adding a library is an image rebuild, deliberately.
  `allowlist` and `open` remain for a workspace that genuinely needs a named API,
  as opt-ins rather than defaults. Cloud metadata and link-local ranges are
  denied in every mode including `open` — that denial is not a policy an operator
  can switch off.
- **Writes are approval-gated by inheritance.** Execution tools declare
  `read_only=False`, so they land on the existing `ask` policy from ADR 0006's
  approval loop and render the code in the proposal preview. Approving once with
  "always allow" is a per-workspace `tool_policies` row, exactly like every
  other write tool.
- **Quotas are enforced before creation, not after billing.** Concurrent
  sessions per workspace, wall-clock per execution, and executions per run.
- **The concurrency quota is a constraint, not a count.** A workspace's limit is
  `N` numbered slots; a session that holds one records the number in
  `sandbox_sessions.slot_index`, and `(workspace_id, slot_index)` is unique. A
  create reserves its slot with an INSERT the database will refuse if the number
  is taken, so the loser learns it lost from its own failed write, before
  `provider.create` — the machine that would break the limit is never started and
  never billed.

  Counting cannot enforce this and no arrangement of counting can. A count
  describes rows that were visible when it ran, so two overlapping creates can
  each be told nothing is ahead of them; ranking committed claims by
  `(created_at, id)` instead — which this replaced — only moves the flaw, because
  the ordering key is stamped while the INSERT is being built and the row becomes
  visible a round trip later, so the claim that sorts second can commit and be
  admitted first, and then the claim that sorts first sees nothing ahead of it
  and is admitted too. That was a real escape: two live machines against a quota
  of one, reproduced deterministically in
  `tests/test_stress_sandbox.py::test_a_claim_that_commits_out_of_order_cannot_take_a_second_slot`.

  Uniqueness is chosen because it is the one serialisation primitive SQLite and
  Postgres genuinely share: `SELECT ... FOR UPDATE` is a no-op on SQLite and
  SQLite's single-writer lock has no counterpart on Postgres, but a unique index
  refuses a duplicate on both, at any isolation level, however the transactions
  interleave. NULL means "holds no slot" and NULLs never collide on either
  engine, so killing, failing, or retiring a session hands the number straight
  back. A claim whose creator died still stops holding its slot after
  `CLAIM_TTL` — the next create in that workspace retires it, and the reaper
  sweeps the rest — so one crash cannot cost a workspace a slot permanently.

## The provider ladder

Three drivers behind one Protocol, in increasing order of what they actually
guarantee. The seam is what makes this a configuration change rather than three
rewrites, and it is the reason the seam was worth building before the first
driver.

| Driver | Isolation | Where it runs | Allowed in |
|---|---|---|---|
| `subprocess` | **none** | this host, this uid | development/test only |
| `container` | namespaces, no network, no caps, non-root, read-only | this host, in Docker | anywhere — **the deployment target** |
| `e2b` | Firecracker microVM | vendor infrastructure | anywhere — optional managed path |

Both local drivers model a session as a **host directory**, not a live machine.
There is no long-lived container: each execution is `docker run --rm` against a
bind mount. So a crashed API leaks no compute, there is nothing to reap, and the
persistent-workspace promise is kept by the filesystem. The cost, and it is a
real one worth stating: **interpreter state does not survive between
executions.** Files persist; a DataFrame loaded in the previous call does not.
Code that relies on notebook-style continuation works against E2B's kernel and
fails here, so generated scripts should be self-contained.

**`subprocess` is not a sandbox and is not called one anywhere in this codebase.**
It runs generated code as the API process's own user, which can read `.env`, the
database file, and `~/.aws/credentials`. It exists for one reason: local
development without a provider key, so that the tool layer, the approval path and
the UI can be exercised end to end on a laptop. It applies what a subprocess can
apply — a scrubbed environment built from nothing, a per-session temp directory
as cwd, `setrlimit` on CPU, address space, file size and process count, no shell,
and a process-group kill on timeout — and none of that is a boundary against code
that is actually trying. `_guard_sandbox` refuses to construct `Settings` with
`SANDBOX_PROVIDER=subprocess` outside development/test, which is the same
structural gate that already stops `MODEL_PROVIDER=scripted` and `DEV_AUTO_LOGIN`
reaching production. That gate is the whole safety argument; the rlimits are
merely so a runaway loop does not take the laptop with it.

**The bind mount must be on a path the container runtime can actually see.**
Trivially true on Linux and a real trap on any VM-backed runtime: Colima,
Rancher and Docker Desktop share only certain host directories into their VM, so
a session directory under macOS's `/var/folders` temp space bind-mounts as an
*empty* directory rather than failing. The symptom is
`python3: can't open file '/workspace/.jasmine_exec.py'` on every execution,
which reads like a driver bug and is not one. `SANDBOX_WORKDIR` defaults to
`./data/sandboxes` under the repo, which is inside `$HOME` and therefore shared;
point it somewhere exotic and check the runtime shares that path first.

**On AWS**, `container` is the driver; the only question is where the Docker
socket lives. ECS-on-EC2 or a plain EC2 host works directly. Fargate does not
permit spawning containers from a task, so a Fargate deployment needs either a
small EC2 executor or a switch to a managed driver. Because the sandbox needs no
network, the execution host needs no NAT gateway and no egress route at all,
which makes the VPC side of this unusually simple.

**`agentcore` remains an option if a managed path is ever wanted**, and on
the axis that made the managed-vendor choice uncomfortable it is strictly better:
the sandbox runs inside the deployment's own AWS account, so customer code and
customer data never leave it, and access is IAM-native with CloudTrail audit and
VPC/PrivateLink available. There is no second vendor and no DPA to negotiate.
Concretely it is `bedrock-agentcore`'s Code Interpreter
(`start_code_interpreter_session` / `invoke_code_interpreter` /
`stop_code_interpreter_session`), which keeps interpreter state across calls
within a session and supports sessions long enough to be worth resuming.

A caution worth recording rather than discovering later: AgentCore's `SANDBOX`
network mode permitted DNS resolution, and researchers demonstrated DNS-based
command-and-control and exfiltration through it before AWS remediated the vector
in March 2026. That is not an argument against AgentCore — it is a concrete
instance of the residual risk this ADR already names. Egress is where sandboxes
leak, the leak does not need a kernel escape, and "the provider says it is
sandboxed" is not the same claim as "this network cannot carry your data out."

## Consequences

- **Turning egress on re-opens the one serious risk.** With the default `none`
  there is no exfiltration path: prompt-injected code can read the documents in
  the sandbox and has nowhere to send them. That property is worth defending,
  because it is doing more work than every other control here combined. Anyone
  setting `SANDBOX_NETWORK_POLICY=open` should understand what they are choosing:
  a document the agent was asked to analyse can carry instructions, the agent
  writes code that honours them, and the code then has both the data and a
  socket. No sandbox escape is required for that, and none of the container flags
  below prevent it.
- **A pre-baked image means a missing import is a dead end.** With no network,
  "I need seaborn" is an image rebuild and a redeploy rather than a `pip install`
  the agent can run itself. That is the honest cost of the trade, and it argues
  for a generous image rather than a minimal one.
- Execution is now a metered external dependency. A provider outage degrades
  analysis to nothing, and the tool must fail with a legible message rather than
  a stack trace.
- Paused sandboxes persist indefinitely on the provider side and cost storage.
  Sessions carry a reaper: idle past `sandbox_session_idle_days` is killed.
- The browser bundler and the CSP-locked preview frame stay exactly as they are.
  Rendering a React app and running pandas are different problems, and collapsing
  them into one mechanism would make both worse.
