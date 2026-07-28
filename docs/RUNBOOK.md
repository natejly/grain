# Local runbook

## Health

`GET /health` checks API and database readiness. Successful responses include an
`X-Request-ID`; callers may supply a safe request ID for log correlation. The
browser shows “Connected” after bootstrap succeeds. Process logs remain on
stdout.

## Reset development data

Stop the API, move `data/workspace.db` and `data/objects` to a backup location,
then run `make seed`. Do not delete production data with this procedure.

## Failed ingestion

Open **Sources** and inspect the source error. Common failures are unsupported
extensions, empty extracted PDFs, or row/page limits. Retrying the original upload
with the same idempotency key returns the original result; use a new key after
correcting the file.

## Stuck run

Inspect `/api/runs/{id}/events` and the run row. `waiting_for_approval` is expected
until a decision. Development startup recovery requeues `running` work with an
expired lease and emits `run.recovered`. Production workers should poll the same
lease field while also enforcing a transport-level lease. External calls may
only be retried when the configured method is safe and read-only.

## Graph projection

The graph automatically rebuilds after successful ingestion and source deletion.
If it is `stale` or `failed`, use **Graph → Rebuild** or send an idempotent
`POST /api/graph/rebuild`. Graph data is disposable; do not restore it without
also restoring its authoritative source/chunk records.

## Dataset or dashboard failure

Dataset creation accepts ready CSV/JSON sources only. Inspect the API error for
row, column, cell, encoding, or normalized-size limits. A new dataset version
copies a new immutable normalized snapshot; it never mutates an older version.
Dashboard query errors indicate a spec that no longer validates against the
current dataset schema. Correct the spec or select a compatible version.

## App publication and rollback

Publishing and rollback require the workspace owner role and an idempotency key.
Review the draft snapshot before making an app public. To roll back, select an
older release in **Apps**; this updates the current-release pointer and leaves all
release content and audit events intact. Changing a dashboard does not alter an
existing app release—create a new snapshot.

## Backup and retention

Back up the database and object bucket as one logical recovery point. Dataset
version snapshots live in the object bucket and must be included. Graph
projections may be rebuilt, but app release manifests and dashboard definitions
are durable database records. Audit and idempotency records should outlive
ordinary conversation retention. A scheduled cleanup should physically purge
tombstoned source objects after the recovery window while retaining the deletion
audit event.

## Release verification

Run `make verify`. It executes linting, type checks, API/unit tests, retrieval
evaluation, the production Next.js build, and isolated Chromium journeys. CI also
regenerates OpenAPI and fails on contract drift.
