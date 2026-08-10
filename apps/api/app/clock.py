from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time as a naive datetime.

    Columns store naive UTC, so every stored-vs-computed comparison assumes naive
    values; returning an aware datetime here would raise TypeError at those sites.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_from_timestamp(timestamp: float) -> datetime:
    """A POSIX timestamp as a naive UTC datetime.

    Replaces the deprecated ``datetime.utcfromtimestamp``. Passing ``timezone.utc``
    is what keeps this UTC: bare ``datetime.fromtimestamp`` would return local time.
    """
    return datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)
