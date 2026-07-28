# ADR 0002: Keep graph memory as a rebuildable SQL projection

## Status

Accepted

## Decision

Entity and co-occurrence data is projected from workspace-owned chunks into
workspace-scoped SQL tables. Every node and edge retains bounded source/chunk
provenance. Ingestion and deletion rebuild the projection; PostgreSQL remains
authoritative.

Neo4j is not added. Graph-derived retrieval expansion remains disabled until the
checked-in evaluation can demonstrate a material, repeatable quality gain over
the existing retrieval baseline.

## Consequences

Local setup gains graph exploration without another stateful service or
cross-store consistency protocol. The projection may be dropped and rebuilt at
any time. More expressive graph traversal will require measured justification
and a new ADR.
