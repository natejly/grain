from __future__ import annotations

from fastapi import Header, HTTPException


def idempotency_key(
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=200),
) -> str:
    value = idempotency_key.strip()
    if not value:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    return value

