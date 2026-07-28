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

## Known MVP limitations

- Production OIDC, encrypted connector credentials, and row-level security
  policies remain deployment-adapter work. The development identity is rejected
  outside development mode. Browser cookie authentication is not implemented;
  a future cookie adapter must add CSRF defenses.
- DNS validation does not pin the HTTP connection to the validated address.
  Production egress should run through a policy-enforcing proxy to eliminate the
  DNS-rebinding time-of-check/time-of-use gap.
- SQLite and in-process tasks are not multi-process production transports.
- The deterministic answer adapter quotes evidence; a production model requires
  prompt-injection evaluation and unsupported-answer monitoring.
- Public app slugs are globally unique. Published snapshots may still contain
  sensitive aggregate values, so owners must review a draft before publication.
