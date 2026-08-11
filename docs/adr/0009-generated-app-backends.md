# 0009 — Generated apps do not get a backend

## Status

Accepted. Amends ADR 0004 and ADR 0005; supersedes neither. The rendering
sandbox and the execution sandbox both stand exactly as written.

## Context

The ask is: dashboards that "ideally run frontend only but can be a vibe-coded
Python FastAPI backend serving a React frontend if need be". The first clause is
what we already ship. The second clause is a different system, and it is worth
saying precisely why before anyone starts on it.

A generated app *with a backend* needs a process that stays up, holds a port and
answers requests. Nothing in this deployment is that shape. There is no
always-on service anywhere in the product: even the workflow scheduler is an
external cron calling `POST /api/workflows/tick`, chosen in
`services/workflows/schedule.py` specifically "to avoid adding an always-on
service to a product that does not have one". ADR 0005's container driver is the
closest existing fit and does not fit at all — `docker run --rm` per execution,
`--network none`, no listening port, no lifecycle, and no route from a browser
to it. It is deliberately a machine that exists for the duration of one call.

And the authority problem is worse than the plumbing problem. A backend that
cannot query workspace data is a static file server. A backend that *can* query
workspace data holds exactly the authority ADR 0004 exists to deny — that ADR's
stated risk was "arbitrary code execution with workspace authority", and ADR
0005 preserved the property by putting execution somewhere with no credentials,
no database URL and no socket. A standing server for LLM-written code that
answers `SELECT`-shaped questions about a tenant's data gives that authority
back, to the one component in the system nobody reads before running.

So the question this ADR settles is not "can we run FastAPI somewhere" — we
could. It is whether a dashboard needing a server is a feature request or a
diagnosis.

## Decision

**Generated dashboards have no backend.** They have a frontend, a typed query
API, and — when Python is genuinely required — a compute step that runs and
exits. Nothing is added that listens on a port.

Three tiers, and the boundary between the second and the third is the decision.

**Tier 0 — frontend only, with live typed queries. Already built.** The frame
already has a backend in the only sense that matters: `window.jasmine.query()`
crosses the postMessage boundary in `components/sandbox-frame.tsx`, is checked
against the release's declared `data_bindings`, and is answered by
`analytics.execute_dataset_query` — a typed AST, field names matched against
stored schema, values bound as parameters, DuckDB single-threaded and in-memory,
500 rows. The app gets a query API it did not write and cannot widen. That is
strictly better than a generated backend, because the enforcement point is ours.

**Tier 1 — a precompute step, not a server.** When the computation genuinely
needs Python — a forecast, a clustering, a regression, a join across three
datasets, anything in the scipy/statsmodels stack the frame cannot carry — the
generated Python runs on the ADR 0005 path, at authoring time and on refresh,
and its *output* becomes a dataset version the app binds to like any other. This
is a materialized view with a refresh schedule, not a service. It runs under
`--network none` with the constructed environment from `sandbox/policy.py`, it
holds no credential, and it is dead before the dashboard is ever opened.

Three constraints make Tier 1 honest, and they are the design, not decoration:

- **Approval binds to the code hash, not the run.** Execution tools are
  `read_only=False` and land on the approval gate, which is correct for a
  human-in-the-loop turn and unworkable for a nightly refresh. So the owner
  approves *a release's precompute source by content hash*, once, in the same
  owner-only act as publication; a scheduled refresh re-runs exactly that hash.
  Regeneration changes the hash and needs a new approval. Nothing unattended
  ever runs code a human did not sign.
- **Promotion to a dataset is a rule change, and it is scoped.** Sandbox
  artifacts are written `status="stored"` and never `"ready"`
  (`sandbox/outputs.py`), precisely so retrieval will not quote bytes nothing
  verified; dataset creation accepts only `ready` sources (`api/analytics.py`).
  Tier 1 crosses that line deliberately and only for artifacts produced by an
  approved release hash. The resulting dataset version records the source hash
  and the input version hashes, so "where did this number come from" has an
  answer. It becomes a dataset, not a passage: it does not enter the RAG path.
- **A precompute that needs the internet is a connector, not a dashboard.**
  Egress stays `none`. If the number comes from a third party, it arrives
  through `services/connectors/`, which already has credential handling, audit
  and an untrusted-content story.

**Tier 2 — a standing per-app process — is refused.** No listening port, no
per-app origin, no reverse proxy, no service identity, no supervisor.

### Why refused, question by question

The brief asked where such an app runs, what it may reach, how the browser
addresses it, how it authenticates, how it dies, and what happens when the
workspace is deleted. Each answer is either a re-opening of ADR 0004 or a
subsystem larger than the feature.

**Where it runs.** Not on an API host — that line is not negotiable and ADR 0005
already holds it. So: a container per app, with `-p`, a restart policy, health
checks, an image or a code mount, and a scheduler placing it. That is a PaaS.
It is a good product; it is not this one, and building it as a side effect of a
dashboard feature is how you end up operating one badly.

**What it may reach.** Two options and both are bad in the same place. Give it a
database connection and every generated app becomes a place where tenancy can be
lost — the `workspace_id` filter in every query, which `tests/isolation.py`
enumerates route by route, would now have a peer written by a language model.
Give it a scoped HTTP token back to our API instead and we have minted a service
account for LLM-written code, whose blast radius is a whole workspace. Note the
asymmetry with ADR 0005: there, prompt injection needed a *socket* to get data
out. Here it gets a credential to pull data *in* that the user never bound. The
declared-bindings model is the thing that works, and it works because the app
receives data it cannot ask to widen.

**How the browser addresses it.** This is the argument that decides it. A
same-origin path proxy (`/api/apps/{id}/backend/*`) puts attacker-controlled
response bytes on our origin, which throws away the opaque origin that ADR 0004
bought — a generated backend answering `Content-Type: text/html` there is stored
XSS with session scope. The correct shape is a separate origin per release,
which costs wildcard DNS, a wildcard certificate, and a router we now operate.
And then the coupling: the frontend currently runs `sandbox="allow-scripts"`
with `connect-src 'none'`, so **it cannot call its own backend**. Admitting a
backend therefore requires relaxing the renderer sandbox — the frame must be
allowed network, and to authenticate it must hold either a token or an origin
that can hold cookies. *The backend cannot be added without weakening the
frontend boundary that is currently doing all the work.* Two ADRs' worth of
containment, spent on plumbing.

**How it authenticates.** Downward: the browser to the app, which needs the
above. Upward: the app to us, which needs per-app identity, issuance, rotation
and revocation — a small PKI for code that is regenerated whenever a user
rephrases a prompt.

**How it dies.** An idle reaper, as in ADR 0005 — but there a session is private
tooling and reaping it costs nobody anything. A published dashboard is a URL
someone shared. Reaping now means cold starts, 502s, and "my dashboard is
asleep" as a support category. Not reaping means N dashboards is N idle
processes, metered in wall-clock nobody asked for, against a spend model (ADR
0008) built for tokens.

**What happens when the workspace is deleted.** Today deletion is rows and
objects. It would now have to stop compute, and *fail loudly* if it cannot — a
deleted workspace whose backend still answers from a warm cache is a retention
violation, not a bug. Workspace deletion would acquire a dependency on the
container runtime being reachable.

**And publication ends the argument.** Published apps are owner-published and
publicly reachable. A public backend is unauthenticated compute, on the
internet, running LLM-written code, with a path to workspace data.

### What "I need a backend" usually means

Read the request as a diagnosis and it decomposes into four things, three of
which are already ours:

- *"I need to run Python over my data."* Tier 1. It never needed to be a server;
  it needed to be a computation.
- *"I need to call a third-party API when the dashboard loads."* A connector.
  Credentials belong on the server, not in generated code, and a view-time fetch
  is a rate limit and an outage attached to a page load.
- *"I need the dashboard to *do* something — submit, trigger, write."* That is a
  workflow (ADR 0007) or a tool call, both of which already have approval,
  audit and idempotency. A dashboard that mutates through a private backend has
  none of the three.
- *"The typed query engine cannot express my query."* This one is real, and it
  is the honest gap. `DatasetQuery` allows filters, **one** `group_by`,
  count/sum/avg/min/max, one ordering and 500 rows. No joins, no second grouping
  key, no median or percentile, no time bucketing, no window functions. A
  meaningful share of "this needs a backend" is that list.

If Tier 2 pressure keeps arriving, **the investment is widening the typed
engine, not building a PaaS** — a second `group_by` key, a join across two bound
datasets, percentile metrics, and a date-truncation bucket would each be a
bounded change to `analytics.py` with the enforcement point unmoved. For
external systems, `services/dbconnect/` already runs guarded read-only SQL
against a user's own database with four layers of statement checking. Both are
places where more capability makes the boundary *stronger*, because the server
stays the one thing deciding what a query may do.

### The only shape a "yes" would take

Recorded so that a future reversal starts from the right place rather than from
`docker run -p`. If request/response semantics ever become genuinely necessary,
build **per-request cold invocation**, not a process: the browser asks *our*
route, under our auth, workspace scope and idempotency; we resolve the release,
materialize its declared bindings into a fresh ADR 0005 container, run the
generated handler for exactly one request, and return the response as data over
the existing postMessage channel — never as a document on any origin.

That keeps every property this ADR is protecting: no port, no lifecycle, no
reaper, no per-app identity, no proxy, no DNS, no certificate, `--network none`
intact, the opaque-origin frame intact, and workspace deletion unchanged because
there is nothing running to delete. It costs a container spawn per request
(hundreds of milliseconds to seconds), no state between requests, and no
streaming. It would need its own ADR and its own threat-model section.

The triggers that would justify starting it: the typed-engine widening above has
shipped and users still hit it; the need is specifically *per-view parameters*
rather than *per-refresh computation*; and there is an answer for published
public apps that does not put unauthenticated LLM-written compute on the
internet. A standing process remains refused under all three.

## Consequences

- **The FastAPI framing buys nothing and costs the boundary.** It is a
  deployment shape, not a requirement. Decomposed, the ask is "run Python over
  my data" and "show me a React UI", and both are already available without a
  process that stays up.
- **Tier 1 is the only new build**, and it is small: a sandbox execution, an
  approved code hash, an artifact promotion, and a refresh on the existing
  schedule ticker. No new origin, no new identity, no new runtime.
- **Saying no is not free.** Some reasonable dashboards are not buildable here,
  and users cannot route around it — the frame CSP means they cannot embed
  something they hosted themselves. They will either use Tier 1, ask for the
  typed engine to grow, or build it somewhere else entirely. That is a real
  cost and it is the price of the frame being airtight.

### Residual risk: a precomputed number is unverified output presented as data

This is the honest headline, and it is the direct analogue of ADR 0005's egress
paragraph. Tier 1 takes the output of code a model wrote, which nobody read,
and gives it the standing of a dataset — the same standing as an uploaded CSV a
human produced. Trust in the number is exactly trust in the code, and the whole
appeal of a vibe-coded computation is not reading it.

Nothing here fixes that, and the mitigations are deliberately modest: the source
hash and input version hashes ride on the dataset version, the frame renders the
computed-at timestamp, and approval is owner-only and per hash. Provenance is
not correctness. A regression fitted on the wrong column produces a plausible
number, an approver skimming forty lines of pandas will not catch it, and the
dashboard will render it beside numbers that came from the typed engine and are
enforced. The difference must be visible in the UI, because it is not visible in
the value.

The staleness risk is smaller and sharper: a precompute is a snapshot, and a
dashboard showing yesterday's figure under today's date is a correctness bug we
would be inviting. The computed-at must be *rendered*, not merely stored — a
timestamp in a manifest nobody displays is not a mitigation.

And unchanged from ADR 0004: a frontend-only app can still burn CPU inside its
frame, and can still spoof UI inside it. The user closes the tab; the
host-rendered "sandboxed" badge stays outside the frame where the frame cannot
draw over it.
