"""Adapters for the embedding seam the services call.

Services embed through `embed_batch`, which returns vectors *and* the revision
the provider answered with, and which takes the width and dtype of the generation
being written. Test doubles care about none of that — they exist to return a
known vector, or to refuse — so `as_batch` lets a double stay the one-line lambda
it was and adapts its shape at the seam.

Keeping the doubles in the old shape is deliberate: a double rewritten to build
`EmbeddedBatch` objects would start asserting the plumbing rather than the
behaviour each test is actually about.
"""
from __future__ import annotations

import inspect
from typing import Callable, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.config import Settings
from app.services import embedding_generations as generations
from app.services.embeddings import DTYPE_WIDTHS, EmbeddedBatch


def seed_vector(
    db: Session,
    *,
    owner_kind: str,
    owner_id: str,
    workspace_id: str,
    blob: bytes,
    dtype: str = "float32",
    model: str = "text-embedding-3-small",
) -> None:
    """Store a fixture vector where the services will actually look for it.

    Tests used to seed by assigning to the row's `embedding` column, which is no
    longer where retrieval reads from — vectors live in `embedding_vectors`, keyed
    by generation, so that a new contract can be built without overwriting the one
    being served. Production rows were moved there by migration 0068; fixtures
    have no migration to move them, so they say it here instead.

    The generation is created from the fixture's own vector width, and activated,
    because a test that seeds a vector means for it to be readable. Widths used by
    tests (8, 64) are not in the calibrated floor table, so the floor falls back to
    `retrieval_dense_floor` and the thresholds these tests were written against
    still hold.
    """
    generation = generations.active_generation(db)
    if generation is None:
        generation = generations.create_generation(
            db,
            model=model,
            dimensions=len(blob) // DTYPE_WIDTHS[dtype],
            storage_dtype=dtype,
            note="test fixture",
        )
        generations.activate(db, generation)
    generations.store_vector(
        db,
        generation=generation,
        owner_kind=owner_kind,
        owner_id=owner_id,
        workspace_id=workspace_id,
        vector=blob,
    )


def as_batch(
    embedder: Callable[..., Optional[List[bytes]]],
) -> Callable[..., Optional[EmbeddedBatch]]:
    """Adapt a `texts -> blobs | None` double to the `embed_batch` signature.

    `dimensions` and `dtype` are accepted and ignored: a double returns whatever
    vector the test pinned, and re-cutting it to the generation's width here would
    silently rewrite the fixture the assertions were written against.
    """

    # A double that declares `model` (or **kwargs) is asking to see the contract
    # it was called under — which is the only way to tell a read embedded for one
    # generation from a read embedded for another, since that no longer shows up
    # in `settings`. Everything else stays the two-argument lambda it was.
    parameters = inspect.signature(embedder).parameters
    wants_contract = "model" in parameters or any(
        parameter.kind is parameter.VAR_KEYWORD for parameter in parameters.values()
    )

    def call(
        texts: Sequence[str],
        settings: Optional[Settings] = None,
        **contract: object,
    ) -> Optional[EmbeddedBatch]:
        blobs = (
            embedder(texts, settings, **contract)
            if wants_contract
            else embedder(texts, settings)
        )
        if blobs is None:
            return None
        return EmbeddedBatch(blobs=list(blobs), revision="")

    return call
