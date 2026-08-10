# MVP threat model

## Protected assets

Workspace documents, derived passages, graph provenance, dataset snapshots,
dashboard definitions, private app releases, conversation history,
identity/session data, tool configuration, external responses, audit history,
and infrastructure credentials.

## Trust boundaries

Uploaded bytes, extracted text, prompts, model output, tool arguments, URLs,
redirects, DNS answers, and external responses are untrusted. The browser is not
trusted to enforce workspace ownership or tool grants.

## Primary controls

- Every owned database read and write is scoped by `workspace_id`.
- Development identity headers are rejected outside development mode.
- Mutations require persisted idempotency keys.
- Uploads enforce extension, byte, PDF-page, and CSV-row caps; uploaded content is
  extracted but never executed.
- Tool requests require HTTPS and an exact host allowlist. DNS results and every
  redirect are rejected for private, loopback, link-local, multicast, reserved,
  unspecified, and metadata-address space.
- Tool execution uses GET only, a ten-second timeout, a redirect cap, and a
  response-size cap. Credentials are never accepted from prompt arguments.
- Approval decisions are actor-attributed, immutable, and audited.
- Source deletion removes chunks from the read path before object cleanup.
- Graph nodes and edges are derived, workspace-scoped, bounded, and rebuildable
  from authoritative chunks. They cannot grant access to source content.
- Dataset creation accepts only ready workspace-owned CSV or JSON sources.
  Normalization caps rows, columns, cell size, and snapshot size.
- Analytical requests use a typed AST. Field names must match the stored schema,
  values are bound parameters, results are capped at 500 rows, and DuckDB runs
  in an isolated single-threaded in-memory connection.
- Dashboard app releases contain immutable bounded dashboard snapshots. No
  supplied HTML, JavaScript, package, SQL, or server code is executed on the
  server or in the workspace page.
- Coded app releases (generated HTML/JS) execute only inside an opaque-origin
  sandboxed iframe (`sandbox="allow-scripts"`, no `allow-same-origin`) served
  from a dedicated frame route whose CSP is `default-src 'none'` with
  `connect-src 'none'`: no network, no cookies, no parent DOM. Data crosses a
  single validated postMessage boundary; live queries are checked against the
  release's declared dataset bindings and always go through the typed
  DatasetQuery engine. Generated HTML is size-capped, linted against external
  references, immutable, and content-hashed. Publication remains owner-only.
- Integration credentials (OAuth tokens, Garmin session tokens) are encrypted
  at rest with a Fernet key (`INTEGRATIONS_ENCRYPTION_KEY`), never returned by
  any API response, and every use is audited. Passwords supplied for the
  credential-based Garmin connector are used for the login exchange only and
  never persisted. Synced external content (email bodies, activity data) is
  untrusted source text under the same rules as uploads.
- LLM agent tool calls are limited to a read-only registry (workspace-scoped
  retrieval, typed dataset queries, graph/memory lookups, and read-only
  integration fetches), bounded by an iteration cap, and recorded per call in
  the run event log and audit history.
- Public publication is owner-only, requires an explicit public visibility
  choice, and exposes only the selected current release. Rollback changes a
  pointer and retains the release audit history.
- Browser responses set CSP, frame, MIME-sniffing, referrer, and permissions
  headers. API responses carry validated request IDs and no-store headers.

## Server-side code execution (ADR 0005)

Generated code runs, and that is a different risk class from everything above.
It never runs on an API host: a session is a `sandbox_sessions` row naming a
machine at a driver — a throwaway container (`container`, the deployment target)
or a hosted microVM (`e2b`). The controls, and then the risk they do not remove.

- `SANDBOX_ENABLED` is off by default. A deployment that does nothing has no
  execution feature, no execution tools in the model's registry, and no driver.
- **The provider ladder is gated structurally, not by convention.** `subprocess`
  runs generated code as the API process's own uid, with that process's access
  to `.env`, the database file and `~/.aws`; `fake` executes nothing and would
  tell users their code ran. `Settings` refuses to construct with either outside
  `APP_ENV=development|test`, the same gate that stops `MODEL_PROVIDER=scripted`
  and `DEV_AUTO_LOGIN`. `subprocess` is not a sandbox and is not called one.
- `container` runs each execution as `docker run --rm` with `--network none`,
  `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`,
  `--user 65534`, and pids/memory/cpu limits. There is no long-lived container,
  so a crashed API leaks no compute.
- **Tenancy is one function.** `sandbox.session.resolve_session` is the only
  code path that turns a session id into an addressable machine, and it filters
  on `workspace_id` inside the query. A foreign session id and an unknown one
  produce the same 404 and the same tool refusal, so neither confirms the other.
  The provider-side external id is never returned by any API response.
- **The sandbox environment is constructed, never copied.** No `OPENAI_API_KEY`,
  no `DATABASE_URL`, no session or encryption key: `policy.sandbox_env` builds a
  four-key dict from scratch, and `tests/test_sandbox_security.py` introspects
  `Settings` for every `SecretStr` field and asserts none of them appears in that
  environment or in the `SandboxSpec` — so a credential added later is covered
  the day it is added.
- **Egress defaults to `none`**, and cloud-metadata plus private/link-local
  ranges (v4 and v6) are denied under every policy including `open`. That denial
  lives in code, not configuration; an operator cannot switch it off.
- The egress policy is frozen onto the session row at creation. Relaxing the
  workspace default does not retroactively widen a machine that is already
  holding someone's documents.
- Execution tools are `read_only=False`, so they inherit the existing approval
  gate. The proposal preview renders both the code and the session's network
  policy, because `requests.post(url, data=df)` is a bug under `none` and an
  exfiltration under `open`, and an approver shown only the code cannot tell
  which they are approving.
- Quotas are enforced before creation, not after billing: concurrent sessions
  per workspace, wall-clock per execution, executions per run. Idle sessions are
  reaped after `SANDBOX_SESSION_IDLE_DAYS`.
- Stored output (stdout, stderr, source, traceback) is byte-clipped on a
  codepoint boundary. Artifacts and downloads land in the workspace's object
  store as `status="stored"` sources — never `"ready"`, so retrieval will not
  quote bytes that nothing ingested and nothing verified.

### Residual risk: a sandbox with egress can exfiltrate what it holds

This is the honest headline, and it is not a hypothetical about kernel escapes.

A sandbox holds whatever the user asked the agent to analyse. With
`SANDBOX_NETWORK_POLICY=open` it also holds a socket. The realistic trigger is
prompt injection through one of those documents: a spreadsheet or PDF carries
instructions, the agent writes code that honours them, and the code has both the
data and a route out. No sandbox escape is required, no provider bug is
involved, and none of the container flags above prevent it — `--cap-drop ALL`
does not stop an HTTPS POST the code was authorised to make.

`SANDBOX_NETWORK_POLICY=allowlist` is what closes it, and the cost is real:
runtime `pip install` stops working, so any library the agent might want has to
be in `infra/sandbox/Dockerfile` already, and adding one is an image rebuild and
a redeploy. `none` — the default — closes it completely and costs the same.
Approval helps but is not the control: an approver reading forty lines of pandas
under time pressure is not reliably going to spot the one line that posts a
dataframe to a URL.

Nothing in this repository makes `open` safe. It is an opt-in for a workspace
that has decided the trade is worth it, with a named reason.

## Known MVP limitations

- Production OIDC, encrypted connector credentials, and row-level security
  policies remain deployment-adapter work. The development identity is rejected
  outside development mode. Browser cookie authentication is not implemented;
  a future cookie adapter must add CSRF defenses.
- DNS validation does not pin the HTTP connection to the validated address.
  Production egress should run through a policy-enforcing proxy to eliminate the
  DNS-rebinding time-of-check/time-of-use gap.
- SQLite and in-process tasks are not multi-process production transports.
- Every chat turn runs a real model over untrusted passage text, so the product
  requires prompt-injection evaluation and unsupported-answer monitoring. The
  scripted test double is not a substitute for either.
- Public app slugs are globally unique. Published snapshots may still contain
  sensitive aggregate values, so owners must review a draft before publication.
- Sandbox egress rules are per-CIDR lists handed to a driver, so they are only
  as good as their coverage of both address families. They previously named
  `0.0.0.0/0` alone, which says nothing about IPv6: under `allowlist` an
  unlisted IPv6 destination was reachable unless the driver happened to treat
  `allow_out` as deny-by-default, and under `none` the strictest policy was
  relying on the internet flag rather than on its own list. Both now deny
  `::/0` alongside `0.0.0.0/0` and carry the always-denied ranges explicitly, so
  each policy's list stands on its own. The residual assumption is narrower but
  real: these are rules we hand to a provider, and we do not verify the provider
  enforces them. `container` does not depend on any of this — it uses
  `--network none` and refuses `allowlist` outright.
- The `container` driver has no per-host egress filter — Docker offers none, and
  the honest implementations are a proxy or an iptables sidecar. It raises on
  `allowlist` rather than silently downgrading to `open`.
- Interpreter state does not survive between executions on the local drivers
  (each execution is a fresh container over the same bind mount); only files do.
  E2B keeps a live kernel. Generated code should be self-contained.
- With `SANDBOX_PROVIDER=e2b`, uploaded workspace documents leave the
  deployment's infrastructure and are processed by a third party under their
  terms. `container` keeps them on the deployment's own hosts.
- A paused sandbox retains its filesystem at the driver until the reaper kills
  it (`SANDBOX_SESSION_IDLE_DAYS`, default 7). Deleting a workspace document
  does not remove a copy an execution already wrote into a sandbox.
