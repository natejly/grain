from __future__ import annotations

import hashlib
import math
import struct
import threading
from collections import OrderedDict
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


def query_cache_key(text: str, model: str, provider: str) -> str:
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

    Hashing keeps a key's size constant. Prompts run to thousands of characters
    and the raw text would otherwise dominate what the cache costs to hold.
    """
    normalized = " ".join(text.split()).casefold()
    return hashlib.sha256(
        f"{provider}\x00{model}\x00{normalized}".encode()
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


def pack_vector(values: List[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(blob: bytes) -> List[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


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
    rows: Sequence[Tuple[str, Optional[bytes]]], query_blob: bytes
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
    """
    width = len(query_blob)
    if width == 0 or width % 4:
        return []
    ids: List[str] = []
    blobs: List[bytes] = []
    for row_id, blob in rows:
        if blob is not None and len(blob) == width:
            ids.append(row_id)
            blobs.append(blob)
    if not ids:
        return []
    matrix = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(ids), width // 4)
    query = np.frombuffer(query_blob, dtype=np.float32)
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


def embed_texts(
    texts: List[str],
    settings: Optional[Settings] = None,
) -> Optional[List[bytes]]:
    """Embed texts with the configured provider, or None when there is none.

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
    `embeddings.create` in this app and its usage is recorded here.
    """
    settings = settings or get_settings()
    if not texts:
        return []
    if settings.active_model_provider != "openai":
        return None
    if not settings.has_openai_key or settings.openai_api_key is None:
        return None
    from openai import OpenAI

    client = OpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.openai_timeout_seconds,
        max_retries=1,
    )
    response = client.embeddings.create(
        model=settings.openai_embedding_model,
        input=[text[:4000] for text in texts],
    )
    usage_service.record_model_usage(
        model=settings.openai_embedding_model,
        operation=usage_service.EMBEDDING,
        # The embeddings API reports `prompt_tokens`/`total_tokens` rather than
        # the Responses API's input/output split; `token_counts` reads both.
        usage=getattr(response, "usage", None),
        settings=settings,
    )
    ordered = sorted(response.data, key=lambda item: item.index)
    return [pack_vector(item.embedding) for item in ordered]
