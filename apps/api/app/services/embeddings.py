from __future__ import annotations

import math
import struct
from typing import List, Optional

from ..config import Settings, get_settings


def pack_vector(values: List[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(blob: bytes) -> List[float]:
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob))


def cosine_similarity(left: List[float], right: List[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0.0 or norm_right == 0.0:
        return 0.0
    return dot / (norm_left * norm_right)


def embed_texts(
    texts: List[str],
    settings: Optional[Settings] = None,
) -> Optional[List[bytes]]:
    """Embed texts with the configured provider, or None in deterministic mode.

    Callers must treat None as "lexical recall only" — never as an error.
    """
    settings = settings or get_settings()
    if not texts:
        return []
    if settings.active_model_provider != "openai" or settings.openai_api_key is None:
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
    ordered = sorted(response.data, key=lambda item: item.index)
    return [pack_vector(item.embedding) for item in ordered]
