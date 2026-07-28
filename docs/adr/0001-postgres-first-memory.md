# ADR 0001: Postgres-first memory

Status: accepted

The MVP uses PostgreSQL as both system of record and retrieval engine. Neo4j is
not part of the initial runtime.

This keeps deletion, provenance, ownership, and retrieval within one consistency
boundary. A graph projection may be added later only if a checked-in evaluation
shows a repeatable quality gain that outweighs projection lag and operational
cost. Any future graph remains rebuildable from Postgres and cannot become the
ownership authority.

