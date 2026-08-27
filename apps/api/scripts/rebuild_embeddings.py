"""Build, inspect, activate and roll back embedding generations.

Changing how a corpus is embedded used to be an unmonitored outage: editing the
model made every stored vector fail the reader's filter at once, hybrid search
silently became lexical search, and nothing logged it. This is the supervised
version of that operation.

    export PYTHONPATH=apps/api
    python apps/api/scripts/rebuild_embeddings.py --status
    python apps/api/scripts/rebuild_embeddings.py --dimensions 256 --dtype float16
    python apps/api/scripts/rebuild_embeddings.py --dimensions 256 --dtype float16 --activate
    python apps/api/scripts/rebuild_embeddings.py --rollback

Building is separate from activating on purpose. A generation is written while
nothing reads it, its coverage is checked against the corpus, and only then does
it become the one retrieval uses — so a half-finished build is never the live
index, and the generation it replaces keeps its vectors for the rollback.

Narrowing a vector costs no provider call. Matryoshka models put the most
information in the leading dimensions, so a 256-dim vector is a renormalised
prefix of the 1536-dim one already stored; measured against the provider's own
`dimensions=256` output, the mean cosine between the two is 0.999997. That is
what makes this worth running: adopting a smaller embedding is a pass over rows
we already have rather than a corpus-wide re-embed with a bill attached.

A *different model* is another matter — nothing about one model's geometry can be
derived from another's — so that path re-embeds, and this script will say so
rather than pretend it can truncate its way there.
"""
from __future__ import annotations

import argparse
import sys

from app.config import get_settings
from app.database import SessionLocal
from app.services import embedding_generations as generations


def _print_status(db) -> None:
    rows = generations.list_generations(db)
    if not rows:
        print("no generations yet; the first embed will create one")
        return
    print(f"{'status':9} {'model':26} {'dims':>5} {'dtype':8} {'floor':>6}  id")
    print("-" * 78)
    for row in rows:
        print(
            f"{row.status:9} {row.model:26} {row.dimensions:5d} "
            f"{row.storage_dtype:8} {row.dense_floor:6.4f}  {row.id}"
        )
        if row.note:
            print(f"{'':9} {row.note}")
    active = generations.active_generation(db)
    if active is not None:
        print(f"\ncoverage of the active generation: {generations.coverage(db, active).describe()}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="list generations and exit")
    parser.add_argument(
        "--dimensions", type=int, help="vector width to build (default: configured)"
    )
    parser.add_argument(
        "--dtype", choices=sorted(generations.DTYPE_WIDTHS), help="storage dtype to build"
    )
    parser.add_argument(
        "--activate",
        action="store_true",
        help="activate the generation once it is complete",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="activate even if coverage is incomplete (says so in the log)",
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="reinstate the most recently retired generation and exit",
    )
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.status:
            _print_status(db)
            return

        if args.rollback:
            previous = generations.rollback(db)
            if previous is None:
                print("nothing to roll back to: no retired generation")
                sys.exit(1)
            db.commit()
            print(f"rolled back to {previous.id} ({previous.model}, {previous.dimensions}d)")
            return

        settings = get_settings()
        source = generations.active_generation(db)
        if source is None:
            print(
                "no active generation to build from — embed something first, or run "
                "backfill_chunks.py"
            )
            sys.exit(1)

        model = settings.openai_embedding_model
        dimensions = args.dimensions or settings.openai_embedding_dimensions
        dtype = args.dtype or settings.embedding_storage_dtype
        if (model, dimensions, dtype) == (
            source.model,
            source.dimensions,
            source.storage_dtype,
        ):
            print(
                f"the active generation already is {model} / {dimensions}d / {dtype}; "
                "nothing to build"
            )
            return
        if model != source.model:
            print(
                f"cannot derive {model} from {source.model}: a different model has "
                "different geometry and must be re-embedded. Set "
                "OPENAI_EMBEDDING_MODEL and run backfill_chunks.py, which writes into "
                "the new generation without touching the active one."
            )
            sys.exit(1)
        if dimensions > source.dimensions:
            print(
                f"cannot widen {source.dimensions}d to {dimensions}d: truncation only "
                "goes down. Widening requires re-embedding."
            )
            sys.exit(1)

        target = generations.create_generation(
            db,
            model=model,
            dimensions=dimensions,
            revision=source.revision,
            storage_dtype=dtype,
            normalization=source.normalization,
            input_format=source.input_format,
            note=(
                f"Derived from {source.id} by Matryoshka truncation "
                f"({source.dimensions}d {source.storage_dtype} -> {dimensions}d {dtype}); "
                "no provider calls."
            ),
            settings=settings,
        )
        print(
            f"building {target.id}: {source.dimensions}d {source.storage_dtype} -> "
            f"{dimensions}d {dtype}, floor {target.dense_floor:.4f}"
        )
        written = generations.materialize_by_truncation(
            db, source=source, target=target
        )
        db.commit()
        print(f"wrote {written} vector(s) with no provider calls")

        report = generations.coverage(db, target)
        print(f"coverage: {report.describe()}")
        if not args.activate:
            print(
                f"\nnot activated. Retrieval still reads {source.id}. When you are "
                f"satisfied:\n  ... rebuild_embeddings.py --dimensions {dimensions} "
                f"--dtype {dtype} --activate"
            )
            return
        generations.activate(db, target, force=args.force)
        db.commit()
        print(f"activated {target.id}; {source.id} retired with its vectors intact")
        print("roll back with: ... rebuild_embeddings.py --rollback")
    finally:
        db.close()


if __name__ == "__main__":
    main()
