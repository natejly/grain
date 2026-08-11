# ADR 0003: Use typed analytics and static generated-app releases

## Status

Accepted in part; the generated-app half is **superseded by ADR 0004**.

The analytics half below still holds exactly as written. The second paragraph of
the decision — declarative manifests, "generated packages, HTML, JavaScript, and
server code are never executed" — does not: ADR 0004 replaced it with generated
HTML/JS executed inside an opaque-origin sandboxed iframe, and ADR 0005 added a
server-side execution sandbox beside it. That paragraph is left unedited because
an ADR records what was decided at the time; this status line is how a reader
learns it no longer describes the system.

## Decision

CSV/JSON sources become immutable normalized dataset versions. Analytical
requests use a typed filter/group/metric contract validated against stored schema
metadata and run in bounded in-memory DuckDB connections. Arbitrary SQL is not an
API surface.

Generated apps are immutable declarative manifests containing bounded dashboard
snapshots. React renders the fixed manifest schema. Publication and rollback only
change an audited current-release pointer; generated packages, HTML, JavaScript,
and server code are never executed.

## Consequences

Dashboards and shareable apps are useful without creating a code-execution or
dependency-supply-chain boundary. Published releases are reproducible and
rollback is constant-time. Interactive custom application code remains out of
scope until it has an isolated build/runtime design and a separate threat model.

## Postscript: what appends to the chain

The decision above says versions are immutable and content-hashed but never says
what makes a *second* one, and for a while nothing in the product did — the web
app created a dataset per tabular source and `POST /api/datasets/{id}/versions`
had no caller, so `current_version` was permanently 1 outside the connector sync
path (`services/connectors/landing.py`). Re-uploading a corrected CSV collided
with the dataset's unique name, and the 409 was swallowed: the fix landed as a
source and every dashboard bound to that dataset kept querying the bad rows.

Two things append a version now, and they are the whole set: a connector sync
(`upsert_dataset`) and a re-upload of a tabular source whose base filename
matches an existing dataset (`use-workspace.ts`). Both go through
`create_dataset_version`, so the chain is real rather than aspirational. A
dataset's identity is its *name*; a version's identity is its content hash.
