# Architecture

## Product boundary

Fieldnote is a cited knowledge and analytical workspace, not a general
autonomous-code platform. PostgreSQL is the production system of record. SQLite
is a deterministic development adapter. Originals live behind an object-storage
boundary, while derived passages, projections, dataset metadata, and provenance
are transactional records.

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

The local adapter scores normalized lexical overlap with length normalization and
a token budget. The production boundary is designed for PostgreSQL full-text
ranking plus pgvector and reciprocal-rank fusion. Every returned passage carries
the source, chunk ordinal, character offsets, and immutable chunk ID.

## Graph projection

The graph is a rebuildable SQL projection over ready chunks. Entities and
co-occurrence links retain bounded source and chunk provenance. Source ingestion
or deletion marks/rebuilds the projection. PostgreSQL and source objects remain
authoritative; no graph database is required. Graph expansion is not injected
into retrieval until an evaluation demonstrates repeatable improvement over the
lexical/vector baseline.

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
workspace scope and stores only bounded static result snapshots in a versioned
manifest. Publishing moves an explicit pointer; rollback moves it to an earlier
immutable release. Public routes return only the current release of an app marked
public. React renders the fixed manifest schema and never evaluates generated
JavaScript, HTML, dependencies, or server code.

## Tool execution

The MVP has one tool shape: an administrator-defined, agent-granted HTTPS GET.
Prompts never directly choose arbitrary destinations. A proposed call becomes a
durable `ToolCall`, pauses the run, and cannot execute until an immutable approval
decision exists. Destination and DNS validation happen again at execution time.

## Repository

- `apps/web` — Next.js workspace
- `apps/api` — FastAPI HTTP and domain services
- `apps/worker` — production queue adapter contract
- `packages/api-client` — typed browser contract and checked-in OpenAPI
- `infra` — optional production-like local services
