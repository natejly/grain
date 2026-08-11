# Jasmine

Jasmine is a local-first agentic knowledge workspace. It combines cited chat,
explicit tool approval, a rebuildable knowledge graph, bounded analytical
dashboards, and immutable published app snapshots.

The expansion phases retain the original safety gates: the graph is a disposable
projection rather than a second system of record, and analytics accept a typed
query contract rather than arbitrary SQL. The third gate changed shape rather
than holding: generated apps used to be declarative snapshots that executed
nothing, and since ADR 0004 they are generated HTML/JS executed in an
opaque-origin sandboxed iframe with no cookies, no parent access and no network.
See the
[implementation plan](.cursor/plans/agentic_knowledge_workspace_619562a2.plan.md).

The API requires an OpenAI key: `OPENAI_API_KEY` must be set or the API refuses
to start. There is no offline mode — chat, memory extraction, graph typing, and
app generation are all model work. The key is read by the Python API only and is
never exposed through `NEXT_PUBLIC_*`, the bootstrap response, or browser storage.

## What works

- Persistent conversations and immutable, resumable SSE run events
- Idempotent conversation, message, upload, cancellation, deletion, and approval mutations
- Markdown, text, PDF, CSV, and JSON ingestion with bounded extraction
- Workspace-scoped hybrid retrieval (BM25 + dense, fused by reciprocal rank),
  cited answers, and exact passage provenance
- Tombstone-first source deletion and derived-chunk cleanup
- Roughly fifty agent tools governed by `ToolPolicy`, with standing grants scoped
  to chat or to unattended workflows, listable and revocable
- Durable approval/denial, SSRF checks, response limits, and audit history
- Rebuildable workspace-scoped entity graph with passage provenance, typed
  relations, and bounded multi-hop walks
- Immutable CSV/JSON dataset versions and bounded DuckDB aggregations
- Declarative table, bar, line, and donut dashboards, with parameterised
  templates bound to a dataset's real columns and per-user pinned tiles on a
  twelve-column home grid
- Private or public app releases with immutable snapshots and rollback
- Workflow automations compiled from a sentence into a reviewable DAG, with
  typed run inputs validated before the first node executes
- Documents in a folder tree, with agent edits proposed for inline review and a
  chat thread per document
- An agent loop with tool approval behind every chat turn, backed by OpenAI
- Responsive Next.js workspace on a cream-and-mint light theme, with a dark theme
  that follows the OS or an explicit toggle. The rail holds the places you work
  — chat, files (files, projects, boards, dashboards), knowledge (sources,
  memory, graph) and workflows — and a top-right menu holds the places you
  configure and audit: connections (databases, MCP, integrations), activity and
  admin. Creating is an action in the corner rather than a destination, and the
  sandbox is a capability the agent uses, not a page you visit

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

Try asking “Who owns the Atlas pilot?” after seeding. To exercise approvals, ask
for something that writes — “draft a launch runbook” — and the run parks on an
approval card you can answer in the conversation or from **Activity**, which
queues every request waiting on a human.

(`/tool github-zen` still reaches the older HTTP-tool path, but only in a dev
database: the `Tool` row it needs is written by `seed_dev_workspace` and by no
endpoint, so that path cannot fire in a real deployment and no longer has a
surface of its own.)

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

## Code execution (optional)

The agent can run Python and shell commands — cleaning a spreadsheet, fitting a
model, drawing a chart — in a sandbox that is not this host. There is no Sandbox
page to visit: you ask for the chart in chat, and the figure comes back on the
tool card in the conversation that asked. It is off unless you turn it on:

```dotenv
SANDBOX_ENABLED=1
SANDBOX_PROVIDER=container      # `make sandbox-image` builds the image first
SANDBOX_NETWORK_POLICY=none     # the default
```

`container` is the deployment driver: one throwaway `docker run --rm` per
execution with `--network none`, `--read-only`, `--cap-drop ALL`, a non-root
user, and pid/memory/cpu limits. `e2b` runs the same interface on hosted
Firecracker microVMs and needs `SANDBOX_API_KEY`; note that it sends the
documents being analysed to a third party. `subprocess` **is not a sandbox** —
it runs generated code as the API's own user — and the API refuses to start with
it unless `APP_ENV` is `development` or `test`.

Two things are worth understanding before changing the defaults. There is no
network in the sandbox, so `pip install` does not work at runtime and every
library the agent might import has to be in `infra/sandbox/Dockerfile` already;
adding one is an image rebuild. And that missing network is what makes this
feature safe rather than merely sandboxed: with `SANDBOX_NETWORK_POLICY=open`, a
document the agent was asked to analyse can carry instructions, the code the
agent writes can honour them, and it then has both your data and a socket. No
escape is required for that. Use `allowlist` if you need a package index or one
named API, and read [the threat model](docs/THREAT_MODEL.md) before using `open`.

Execution tools are approval-gated like every other write tool, and the approval
card shows the code alongside the session's network policy.

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
