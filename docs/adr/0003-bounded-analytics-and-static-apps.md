# ADR 0003: Use typed analytics and static generated-app releases

## Status

Accepted

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
