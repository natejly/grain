# Fieldnote

Fieldnote is a local-first agentic knowledge workspace. It combines cited chat,
explicit tool approval, a rebuildable knowledge graph, bounded analytical
dashboards, and immutable published app snapshots.

The expansion phases retain the original safety gates: the graph is a disposable
projection rather than a second system of record, analytics accept a typed query
contract rather than arbitrary SQL, and generated apps render declarative static
snapshots rather than executing generated dependencies or server code. See the
[implementation plan](.cursor/plans/agentic_knowledge_workspace_619562a2.plan.md).

The API requires an OpenAI key: `OPENAI_API_KEY` must be set or the API refuses
to start. There is no offline mode — chat, memory extraction, graph typing, and
app generation are all model work. The key is read by the Python API only and is
never exposed through `NEXT_PUBLIC_*`, the bootstrap response, or browser storage.

## What works

- Persistent conversations and immutable, resumable SSE run events
- Idempotent conversation, message, upload, cancellation, deletion, and approval mutations
- Markdown, text, PDF, CSV, and JSON ingestion with bounded extraction
- Workspace-scoped lexical retrieval, cited answers, and exact passage provenance
- Tombstone-first source deletion and derived-chunk cleanup
- Per-agent grants for an allowlisted HTTPS GET tool
- Durable approval/denial, SSRF checks, response limits, and audit history
- Rebuildable workspace-scoped entity graph with passage provenance, typed
  relations, and bounded multi-hop walks
- Immutable CSV/JSON dataset versions and bounded DuckDB aggregations
- Declarative table, bar, line, and donut dashboards
- Private or public app releases with immutable snapshots and rollback
- An agent loop with tool approval behind every chat turn, backed by OpenAI
- Responsive dark Next.js workspace across chat, sources, graph, dashboards, apps,
  approvals, and activity

## Quick start

Requirements: Python 3.10+, Node 20+, and npm. Docker is optional.

```bash
make install
cp .env.example .env
# Add your OpenAI key to .env before going further; the API will not start
# without it. See "Connect OpenAI" below.
make seed
```

Run both servers with one command:

```bash
make dev
```

It checks ports and dependencies, waits for the API health check, and prefixes
logs with `[api]` / `[web]`. To run the servers separately use `make dev-api`
and `make dev-web` in two terminals.

Open [http://localhost:3000](http://localhost:3000). The API reference is at
[http://localhost:8000/docs](http://localhost:8000/docs).

Try asking “Who owns the Atlas pilot?” after seeding. To exercise approvals,
send `/tool github-zen`, open **Activity**, and approve or deny the request.

## Connect OpenAI

This is required, not optional. Create an API key in the OpenAI platform, then
open the root `.env` file in an editor and add:

```dotenv
MODEL_PROVIDER=openai
OPENAI_API_KEY=your-key
OPENAI_MODEL=gpt-5.5
OPENAI_REASONING_EFFORT=low
```

Starting the API without a key fails immediately with a message naming
`OPENAI_API_KEY`, rather than booting and erroring on the first message.

Restart `make dev-api` after changing `.env`. Do not prefix the key with
`NEXT_PUBLIC_`, commit `.env`, paste the key into chat, or place it in the web
application.

The one exception is `MODEL_PROVIDER=scripted`, a test double that replays a JSON
script instead of calling a provider (`apps/api/app/services/scripted_model.py`).
It is what the test suite and the browser suite run on, and the API refuses to
boot it outside `APP_ENV=development` or `test`.

## Verification

```bash
make lint
make test
make eval
make build
make test-e2e
```

The checked-in evaluation corpus must maintain at least 80% recall@5 and 90%
top-citation precision. Expand the corpus before treating these numbers as a
production-quality benchmark.

## Production adapters

`infra/compose.yaml` provides PostgreSQL/pgvector, Redis, and MinIO. Install the
Postgres extra with `pip install -e "apps/api[postgres]"`, set `DATABASE_URL`,
then run:

```bash
cd apps/api
../../.venv/bin/alembic upgrade head
```

The development server uses SQLite, filesystem objects, and in-process background
tasks. PostgreSQL, a queue transport, object storage, and production identity are
deployment adapters; the durable state machines and workspace-scoped APIs remain
the same. Run `make migrate` before starting a deployed API.

See [Architecture](docs/ARCHITECTURE.md), [Threat model](docs/THREAT_MODEL.md),
and [Runbook](docs/RUNBOOK.md).
