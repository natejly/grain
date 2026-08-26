from __future__ import annotations

import hashlib
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

from ..config import Settings, get_settings
from . import usage as usage_service

# Entries, not bytes. One entry is a 64-char key plus one vector — 6KB for
# text-embedding-3-small, 12KB for -3-large — so 256 entries costs ~1.5-3MB.
# The bound is not decoration: the key is user-typed prompt text, so its
# cardinality is driven entirely by input and an unbounded dict here is a memory
# leak with a chat box attached.
QUERY_CACHE_MAX_ENTRIES = 256


def query_cache_key(
    text: str, model: str, provider: str, generation_id: str = ""
) -> str:
    """Cache key for one query's embedding: (provider, embedding model, text).

    The model is part of the key because vectors from two models are not
    comparable at all — different dimensionality at best, silently different
    geometry at worst — so serving a `text-embedding-3-small` vector to a run
    configured for `-3-large` is a correctness bug, not a stale-cache annoyance.

    The *provider* is part of it for a blunter reason: `embed_texts` answers None
    for any provider but openai, and a caller that gets None is contractually in
    lexical-only mode. With the provider out of the key a scripted-mode recall
    reads a vector some earlier openai-mode call left behind and starts scoring
    semantically — the one thing that mode promises not to do.
    Nothing routes two providers through one process today (`get_settings` is
    `lru_cache`d and the only `model_copy` in the app edits `memory_recall_limit`),
    so this closes the hole rather than repairs a live break; it costs one string.

    Normalisation is deliberately shallow: casefold and whitespace collapse,
    nothing else. Those are the two differences the rest of recall already
    ignores (`tokenize` lowercases, `normalize_memory_content` collapses runs of
    space), so folding them here cannot make the cached path disagree with the
    uncached one about which memories match. Anything deeper — stripping
    punctuation or stopwords, sorting terms — would merge queries an embedding
    model reads as different sentences: "do not deploy on friday" and "deploy on
    friday" share every content word and mean opposite things.

    The *generation* joins the key because the model name stopped identifying the
    vector once width and dtype became choices. Two generations can name the same
    model and produce vectors that cannot be compared at all — 1536 float32 and
    256 float16 differ in every byte and in length — so without this a query
    embedded during a migration would be served, from cache, to the arm that
    cannot read it. Every stored vector would then fail the equal-length guard and
    the dense arm would return nothing, for exactly as long as the entry lived.

    Hashing keeps a key's size constant. Prompts run to thousands of characters
    and the raw text would otherwise dominate what the cache costs to hold.
    """
    normalized = " ".join(text.split()).casefold()
    return hashlib.sha256(
        f"{provider}\x00{model}\x00{generation_id}\x00{normalized}".encode()
    ).hexdigest()


class QueryEmbeddingCache:
    """A bounded, thread-safe LRU of query vectors.

    Thread-safe because FastAPI runs sync endpoints on a threadpool, so several
    turns touch this at once even in the single-process deployment it is sized
    for. `OrderedDict` operations hold the GIL individually but a get-then-evict
    pair does not, and a torn eviction would drop a live entry.
    """

    def __init__(self, max_entries: int = QUERY_CACHE_MAX_ENTRIES) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[bytes]:
        with self._lock:
            blob = self._entries.get(key)
            if blob is None:
                return None
            self._entries.move_to_end(key)
            return blob

    def put(self, key: str, blob: Optional[bytes]) -> None:
        """Store a vector. A falsy blob is dropped, never cached.

        "No vector" is a property of the process's configuration and of whether
        the provider answered — not of the query text. Caching it would pin a
        keyless miss, or one timed-out call, onto a key that the very next lookup
        could well satisfy.
        """
        if not blob:
            return
        with self._lock:
            self._entries[key] = blob
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# Process-local by design. See services/memory.py::_embed_query for why this is
# not a database table.
query_embedding_cache = QueryEmbeddingCache()


# The dtypes a vector may be stored in, and what one element costs.
#
# Explicitly little-endian ("<") rather than native. `pack_vector` used
# `struct.pack("<f")` before this was a choice, so every float32 vector already
# on disk is little-endian; letting numpy pick native byte order would keep
# working on every machine this runs on today and silently reinterpret the whole
# corpus on the first big-endian one. Pinning it costs a character.
DTYPE_CODES = {"float32": "<f4", "float16": "<f2"}
DTYPE_WIDTHS = {"float32": 4, "float16": 2}
DEFAULT_DTYPE = "float32"


def dtype_width(dtype: str) -> int:
    """Bytes per element, for a dtype name from a generation record."""
    try:
        return DTYPE_WIDTHS[dtype]
    except KeyError:  # pragma: no cover - guarded at write time
        raise ValueError(f"unknown embedding dtype: {dtype!r}") from None


def content_fingerprint(text: str) -> str:
    """sha256 of the exact text a vector was built from.

    Stored beside the vector so staleness is detectable rather than assumed. The
    text is hashed verbatim — no casefolding, no whitespace collapse — because
    the question this answers is "did the bytes we embedded change", and any
    normalisation here would answer a different, weaker question.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pack_vector(values: List[float], dtype: str = DEFAULT_DTYPE) -> bytes:
    return np.asarray(values, dtype=DTYPE_CODES[dtype]).tobytes()


def unpack_vector(blob: bytes, dtype: str = DEFAULT_DTYPE) -> List[float]:
    return np.frombuffer(blob, dtype=DTYPE_CODES[dtype]).astype(np.float32).tolist()


def truncate_vector(
    blob: bytes,
    *,
    dimensions: int,
    source_dtype: str = DEFAULT_DTYPE,
    dtype: str = DEFAULT_DTYPE,
) -> Optional[bytes]:
    """Re-cut an existing vector to a smaller Matryoshka prefix, locally.

    This is what makes a new generation nearly free. Matryoshka models pack the
    most information into the leading dimensions, so a shorter vector is a prefix
    of a longer one — slice, renormalise, done, with no provider call. Measured
    against OpenAI's own `dimensions=256` output on the eval corpus, the mean
    cosine between the two is 0.999997, so a locally re-cut vector is the API's
    vector to six decimal places.

    That turns "adopt a smaller embedding" from a corpus-wide re-embed — the
    expensive, rate-limited, hours-long thing that makes teams never do it — into
    a pass over rows we already have. It is also what lets several dimensions be
    compared honestly, since each is materialised from the identical source pass
    rather than from a separate call that would differ for its own reasons.

    Renormalisation is not optional. Dropping dimensions shortens the vector, and
    a shorter vector has a smaller dot product with everything; skipping this step
    leaves cosines that depend on how much of the tail happened to be cut.

    Returns None when the source is too short to cut, which is the honest answer
    for a vector that was never long enough to hold this generation's prefix.
    """
    source = np.frombuffer(blob, dtype=DTYPE_CODES[source_dtype])
    if source.size < dimensions:
        return None
    head = source[:dimensions].astype(np.float32)
    norm = float(np.linalg.norm(head))
    if norm == 0.0:
        # An all-zero prefix carries no direction to preserve. Normalising it
        # would divide by zero and storing it would put a vector that matches
        # nothing at cosine 0 into the index; refusing is the same answer as
        # "not embedded", which callers already handle.
        return None
    return np.asarray(head / norm, dtype=DTYPE_CODES[dtype]).tobytes()


def cosine_similarity(left: List[float], right: List[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


def ranked_cosine_scores(
    rows: Sequence[Tuple[str, Optional[bytes]]],
    query_blob: bytes,
    dtype: str = DEFAULT_DTYPE,
) -> List[Tuple[str, float]]:
    """(id, cosine) for every row that can be compared to the query, best first.

    One matmul rather than a Python loop, because both callers — memory recall and
    document retrieval — score tens of thousands of rows per turn.

    Blobs whose length differs from the query's are dropped rather than scored.
    That is exactly what `cosine_similarity` does on a length mismatch, and it is
    what keeps a workspace holding vectors from two embedding models (1536-dim and
    3072-dim) from making `reshape` raise.

    The ordering is total: score descending, then id, so two rows at identical
    similarity always come back in the same order on both backends and a caller
    that truncates the list truncates it the same way every run.

    `dtype` must be the storage dtype of the generation these blobs belong to.
    It is a parameter rather than something inferred from the byte length because
    it cannot be inferred: a 512-byte blob is 128 float32s or 256 float16s, and
    both reshape without complaint. Passing the wrong one does not raise, it
    ranks — on numbers read out of the middle of somebody else's floats.
    """
    element = dtype_width(dtype)
    width = len(query_blob)
    if width == 0 or width % element:
        return []
    ids: List[str] = []
    blobs: List[bytes] = []
    for row_id, blob in rows:
        if blob is not None and len(blob) == width:
            ids.append(row_id)
            blobs.append(blob)
    if not ids:
        return []
    code = DTYPE_CODES[dtype]
    # Widened to float32 for the arithmetic even when stored narrower: a float16
    # matmul accumulates in float16 and a 256-term dot product loses real
    # precision to rounding, which shows up as ties that should not be ties.
    # Storage width and compute width are different decisions.
    matrix = (
        np.frombuffer(b"".join(blobs), dtype=code)
        .reshape(len(ids), width // element)
        .astype(np.float32)
    )
    query = np.frombuffer(query_blob, dtype=code).astype(np.float32)
    query_norm = float(np.linalg.norm(query))
    if query_norm == 0.0:
        return []
    norms = np.linalg.norm(matrix, axis=1)
    # Stored vectors are not guaranteed unit-norm, and a zero vector would divide
    # by zero; 1.0 leaves its dot product of 0 as a similarity of 0.
    norms[norms == 0.0] = 1.0
    sims = (matrix @ query) / (norms * query_norm)
    pairs = [
        # Clamped at 0: a negative cosine is "unrelated", and letting it go
        # negative would let one bad vector subtract from a score built by adding.
        (row_id, max(0.0, float(value)))
        for row_id, value in zip(ids, sims.tolist(), strict=True)
    ]
    pairs.sort(key=lambda item: (-item[1], item[0]))
    return pairs


@dataclass(frozen=True)
class EmbeddedBatch:
    """Vectors plus the provenance needed to record what produced them."""

    blobs: List[bytes]
    #: The model id the provider answered with, for the generation's `revision`.
    #: Captured rather than assumed: it is the only field of the contract the
    #: provider gets a vote on, and the day it starts disagreeing with what we
    #: asked for is the day we need to have been recording it all along.
    revision: str


def embed_batch(
    texts: List[str],
    settings: Optional[Settings] = None,
    *,
    model: Optional[str] = None,
    dimensions: Optional[int] = None,
    dtype: str = DEFAULT_DTYPE,
) -> Optional[EmbeddedBatch]:
    """Embed texts under an explicit contract, or None when there is no provider.

    `dimensions` is passed through to the provider, which applies Matryoshka
    truncation server-side and returns an already-renormalised vector. Left None,
    the model's native width comes back — which is what every caller wanted
    before dimension was a choice.

    `model` is explicit for the same reason. A *read* embeds its query under the
    generation it is about to score against, which during a migration is not the
    configured model at all — and taking the model from configuration while taking
    the width from the generation would produce a query vector that is the right
    length and the wrong geometry. Same length, different model, no error: the one
    failure mode this whole contract exists to make unrepresentable. Defaulting to
    the configured model keeps the write path unchanged.

    See `embed_texts` for why the provider, not the key, is the gate, and for why
    this is the single accounting chokepoint for embedding spend.
    """
    settings = settings or get_settings()
    if not texts:
        return EmbeddedBatch(blobs=[], revision="")
    if settings.active_model_provider != "openai":
        return None
    if not settings.has_openai_key or settings.openai_api_key is None:
        return None
    from openai import OpenAI

    model = model or settings.openai_embedding_model
    client = OpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.openai_timeout_seconds,
        max_retries=1,
    )
    request: dict = {
        "model": model,
        "input": [text[:4000] for text in texts],
    }
    if dimensions:
        request["dimensions"] = dimensions
    response = client.embeddings.create(**request)
    usage_service.record_model_usage(
        # The model actually billed, which is the generation's on a read.
        model=model,
        operation=usage_service.EMBEDDING,
        # The embeddings API reports `prompt_tokens`/`total_tokens` rather than
        # the Responses API's input/output split; `token_counts` reads both.
        usage=getattr(response, "usage", None),
        settings=settings,
    )
    ordered = sorted(response.data, key=lambda item: item.index)
    return EmbeddedBatch(
        blobs=[pack_vector(item.embedding, dtype) for item in ordered],
        revision=str(getattr(response, "model", "") or ""),
    )


def embed_texts(
    texts: List[str],
    settings: Optional[Settings] = None,
) -> Optional[List[bytes]]:
    """Embed texts at the model's native width as float32. See `embed_batch`.

    Kept as the shape most callers want — just the vectors — now that dimension,
    dtype and revision are things a caller may need to state. Callers that write
    a vector into a generation want `embed_batch`, because they have to record
    what produced it.

    Callers must treat None as "lexical recall only" — never as an error.

    The gate is the *provider*, not the key. Gating on the key alone was wrong in
    the one direction that matters: `scripted` is a test double, and a developer
    running it almost always still has OPENAI_API_KEY sitting in .env, so a
    key-only gate sent the double — the whole `apps/api` suite and the browser
    e2e server — to the live embeddings API. That is a billed, non-hermetic,
    offline-breaking network call from the mode whose entire promise is that no
    model is behind it.

    Settings refuse to boot `openai` without a key, so the first branch is what
    actually fires; the second only catches a `model_copy` that edited the key
    out, which bypasses validation.

    This is also the accounting chokepoint for embeddings, which are the spend
    everyone forgets: no prompt, no answer, no latency anyone notices, and one
    per chunk of every document ever uploaded. There is exactly one
    `embeddings.create` in this app and its usage is recorded there.
    """
    batch = embed_batch(texts, settings)
    return None if batch is None else batch.blobs
