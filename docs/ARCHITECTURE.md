# Architecture

## Product boundary

Jasmine is a cited knowledge and analytical workspace. It does execute generated
code — the boundary is that code only ever runs somewhere holding no workspace
authority, never that code does not run. See "Code execution boundaries" below,
which is the section to read before trusting any security claim in this file.
PostgreSQL is the production system of record. SQLite is a deterministic
development adapter. Originals live behind an object-storage boundary, while
derived passages, projections, dataset metadata, and provenance are
transactional records.

## Durable state

A user mutation first creates durable state and an idempotency record. Runs append
immutable events with a per-run sequence. Clients reconnect using `Last-Event-ID`
or the `after` query parameter. Runs use the states `queued`, `running`,
`waiting_for_approval`, `cancelling`, `completed`, `failed`, and `cancelled`.

The local executor invokes the same task entrypoints intended for a production
queue. Runs receive bounded leases. Startup recovery requeues expired local work,
resumes queued ingestion, and safely retries approved read-only calls. A
production queue adds transport-level claiming and retry scheduling while using
the same idempotent task entrypoints.

## Retrieval

Retrieval is hybrid and on by default: a BM25 arm and a dense (cosine) arm, fused
by reciprocal rank. `RETRIEVAL_BM25=0` and `RETRIEVAL_HYBRID=0` are ablation
switches back to the older single-arm lexical scorer, not feature flags awaiting
a launch.

Neither arm uses a database-specific index. The lexical arm reads an inverted
index kept in ordinary rows (`chunk_terms`) and the dense arm stores embeddings
as packed blobs scored in Python over a recency-capped candidate set, so one
ranking function produces identical results on SQLite and PostgreSQL. There is no
`tsvector` and no pgvector anywhere in `apps/api` — an earlier version of this
document described both as the intended production boundary, and that is not the
design that shipped.

Every returned passage carries the source, chunk ordinal, character offsets, and
immutable chunk ID. A cited web page is the one exception: it is mapped onto the
same `Evidence` shape and validated by the same citation checker, but its
`chunk_id` is a synthetic `web:<digest>` addressing no passage, and its
provenance is the `url` on the citation rather than a chunk to open.

## Graph projection

The graph is a rebuildable SQL projection over ready chunks. Entities and links
retain bounded source, chunk, and memory provenance. Source ingestion or deletion
marks/rebuilds the projection. PostgreSQL and source objects remain authoritative;
no graph database is required. Graph expansion is not injected into retrieval
until an evaluation demonstrates repeatable improvement over the lexical/vector
baseline.

Extraction runs in two layers. A capitalization regex generates the candidate
names; a bounded per-passage LLM pass then types and trusts a subset of them,
adds the names capitalization cannot see
and the typed relations (`works_on`, `owns`, `part_of`, `located_in`,
`reports_to`, `uses`, `created`, `depends_on`, `acquired`, `related_to`) that
co-occurrence can only guess at. The vocabulary is closed, but an extracted
relation is normalised onto it — "creates" becomes `created` — rather than
flattened to the null `related_to`, and a pair that carries a named relation
does not also keep the null one. Each edge carries the extractor's confidence;
co-occurrence remains the fallback relation at a fixed low score. Extraction
calls are capped per rebuild and a provider outage degrades to the regex path
rather than failing the rebuild.

The regex layer filters names that cannot identify a thing: bare calendar words
(`October`, `Tuesday`) are dropped, because capitalization alone cannot tell the
month from the surname. Better evidence overrules that guess — an extractor that
read the sentence and typed `Friday` as a product, or a human who curated `May`
onto a memory, keeps the name. Article-prefixed duplicates are merged into the
bare name when the workspace also uses it on its own, so `The Atlas` and `Atlas`
are one node; every lookup keyed on a normalized name tries both spellings so the
merge cannot hide the node. Casing no longer types entities — an ALL-CAPS token
like `RFC` is an acronym of unknown referent, not an organization.
Co-occurrence is quadratic in the entities of a passage, so a pair earns an edge
only by recurring across passages, or by coming from a passage specific enough to
have named at most a handful of things — except that the rule may never orphan an
entity, so a name whose every pairing was thinned keeps its strongest one. That
floor is what stops a workspace of meeting notes, where no pair ever recurs, from
projecting entities and no edges at all.

`graph_neighbors` and `graph_path` walk the projection — entities within N hops,
and the shortest chain of relations between two entities. Both are read-only and
bound their expansion, so a hub entity returns a ranked slice flagged
`truncated` rather than every edge it owns.

## Analytics

CSV and JSON sources can be normalized into immutable dataset versions. Each
version stores a content hash, typed schema, row count, and private object
snapshot. Clients submit a typed filter/group/metric AST. The API validates every
field and operation against the stored schema, generates a parameterized query,
and runs it in an isolated in-memory DuckDB connection with row, column, cell,
snapshot, result, and thread limits. Arbitrary SQL is never accepted.

Dashboards persist a validated visualization spec plus the dataset query, not
rendered HTML or executable code.

## Generated apps

A generated app is a name, globally unique slug, visibility policy, and immutable
release history. Creating a release executes its dashboards under the owner's
workspace scope and stores bounded result snapshots in a versioned manifest.
Publishing moves an explicit pointer; rollback moves it to an earlier immutable
release. Public routes return only the current release of an app marked public.

Since ADR 0004 the manifest is `kind: "code"` and carries a self-contained,
size-capped, content-hashed HTML document that **is** executed in the browser.
Until that ADR the manifest was a declarative snapshot and this document said
React "never evaluates generated JavaScript, HTML, dependencies, or server code";
that sentence outlived the design by several releases, which is the failure mode
a security claim in a document is most prone to. What replaced it is in the next
section.

## Code execution boundaries

Three of them, none of which runs generated code with workspace authority.

**Rendering (ADR 0004).** Generated app HTML/JS is served as a document from a
dedicated frame route and embedded as `<iframe sandbox="allow-scripts">` with no
`allow-same-origin`, so it runs in an opaque origin with no cookies and no access
to the parent DOM. Its CSP is `default-src 'none'` with `connect-src 'none'`, so
it cannot reach the network at all. Data crosses one validated `postMessage`
protocol: the host checks the event source, checks each requested dataset against
the release's declared bindings, and answers from the typed query engine
described under Analytics. `apps/web/components/sandbox-frame.tsx` is the
security-critical surface.

**Execution (ADR 0005).** The agent can run Python and shell through a
`SandboxProvider` seam. It is opt-in (`SANDBOX_ENABLED=0` by default) and
network-less by default; the deployable drivers are a Docker container per
execution (`--network none --read-only --cap-drop ALL --security-opt
no-new-privileges`, unprivileged uid, memory/cpu/pid caps) and hosted E2B
microVMs. The `subprocess` and `fake` drivers provide no isolation and a boot
guard refuses them outside development. Charts and files an execution produces
become `Source` rows with `status="stored"` — stored, deliberately not indexed,
so retrieval cannot quote something that was never ingested.

Originals, including those charts, are streamed back by one workspace-scoped
authenticated route. It sends `Content-Disposition: attachment` for everything
except raster images, because this API is a different origin from the web app but
it is the origin the session cookie belongs to: an inline SVG, HTML file or PDF
would execute as the API. The stored object key is resolved and required to sit
under the caller's own workspace directory rather than pattern-matched for
traversal.

**LaTeX preview.** TeX Live compiled to WebAssembly, running in the browser with
the network closed and a fixed package tier.

## Tool execution

The agent has roughly fifty tools, assembled per request by `build_registry`:
retrieval, datasets, graph, memory, documents and boards, project files, database
connections, sandbox, connectors, and whatever an attached MCP server advertises.
The single-HTTPS-GET tool this section used to describe is now one legacy shape
among them.

A proposed call becomes a durable `AgentToolCall`. Whether it executes is decided
by `resolve_policy`, the one decision point: absent an override, read-only tools
run unattended and write-capable tools park the run on an approval that must be
recorded before anything happens. A `ToolPolicy` row overrides that default, and
it is scoped — a standing "always allow" clicked in a conversation is `chat`
scope and is ignored by an unattended workflow, which is ADR 0007's sharpest
residual risk made structural. A standing grant is listable and revocable
(`GET`/`DELETE /api/tool-policies`); `resolve_policy` reads the table on every
call, so a revoke takes effect on the next tool call.

Hosted web search is the exception to all of the above: it executes inside the
model provider's infrastructure, so there is no `ToolSpec`, no approval card and
no local execution. Its results rejoin the normal citation contract.

## Multi-tenancy

Every workspace-scoped table carries a `workspace_id` and every query filters on
it. That claim is proved by experiment rather than by reading: `tests/isolation.py`
enumerates every operation in the OpenAPI document as a request aimed at a second
tenant's resource, and a coverage test fails when a route joins the app without an
isolation verdict. A refusal must carry the exact expected status, because a
refusal with the wrong reason is still an oracle.

## Agent loop, cost, and automation

A turn is a serialized `LoopState` on the run row, so a run can park mid-turn —
on an approval or on a spend ceiling — and resume in another process. Runs carry
`paused_reason` to say which.

Model usage is metered at two chokepoints into a `ModelUsage` ledger (counts and
identifiers, never prompt text), and `cost_usd` is null rather than zero when a
model has no configured price. Per-window USD and token ceilings park a run at
`waiting_for_approval` instead of killing it; a dollar ceiling over unpriced calls
is refused at boot rather than silently meaning "no limit" (ADR 0008). Owners read
and set this at `/api/admin/usage` and `/api/admin/budget`.

Workflows (ADR 0007) compile an English description into a DAG and execute it on
the same run loop, on a schedule, at `workflow` policy scope.

## Other subsystems

Memory (durable per-workspace items with supersession), MCP client with per-user
OAuth tokens (ADR 0006), projects with a LaTeX toolchain and bibliography
management, documents and kanban boards as agent-editable artifacts, third-party
connectors (Gmail, Strava, Garmin) and external database connections. Each has
routes under `/api` and a view in the web app.

## Repository

- `apps/web` — Next.js workspace
- `apps/api` — FastAPI HTTP and domain services; `app/services/` holds the agent
  loop, retrieval, sandbox, workflows, MCP, projects, artifacts, connectors,
  auth, memory, usage and budget
- `apps/worker` — production queue adapter contract
- `packages/api-client` — typed browser contract and checked-in OpenAPI
- `infra` — optional production-like local services
- `docs/adr` — the decisions; where this document and an ADR disagree, the ADR is
  the record and this document is the summary that drifted
