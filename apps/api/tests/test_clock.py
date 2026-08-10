from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.clock import utc_from_timestamp, utcnow

API_DIR = Path(__file__).resolve().parents[1]
APP_DIR = API_DIR / "app"
EPOCH = datetime(1970, 1, 1)


@pytest.fixture
def non_utc_tz():
    """Force a fixed non-UTC local timezone for the duration of a test.

    Without this, a helper that wrongly returned local time would still pass every
    assertion on a UTC host (local == UTC there), which is exactly how CI runs.
    """
    if not hasattr(time, "tzset"):  # pragma: no cover - POSIX-only
        pytest.skip("time.tzset is unavailable on this platform")
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_utcnow_is_naive_and_tracks_real_utc():
    before = time.time()
    value = utcnow()
    after = time.time()

    assert value.tzinfo is None
    # Value must be UTC wall clock, not local time: compare against the epoch seconds
    # that bracket the call, which is exactly what the old datetime.utcnow() returned.
    assert EPOCH + timedelta(seconds=before) - timedelta(seconds=1) <= value
    assert value <= EPOCH + timedelta(seconds=after) + timedelta(seconds=1)


def test_utcnow_matches_epoch_derived_utc_to_the_second():
    stamp = time.time()
    assert abs((utcnow() - (EPOCH + timedelta(seconds=stamp))).total_seconds()) < 1


def test_utcnow_is_utc_not_local_time(non_utc_tz):
    """The catastrophic regression: utcnow() silently returning local time."""
    assert utcnow() - datetime.now() > timedelta(hours=1)


def test_utc_from_timestamp_is_naive_utc(non_utc_tz):
    # 2023-11-14T22:13:20Z. Local time in America/New_York is 5h behind.
    value = utc_from_timestamp(1_700_000_000)
    assert value.tzinfo is None
    assert value == datetime(2023, 11, 14, 22, 13, 20)
    assert value != datetime.fromtimestamp(1_700_000_000)


@pytest.mark.parametrize("deprecated", ["datetime.utcnow(", "datetime.utcfromtimestamp("])
def test_no_deprecated_datetime_calls_remain(deprecated):
    offenders = [
        str(path.relative_to(API_DIR))
        for directory in (APP_DIR, API_DIR / "scripts")
        for path in directory.rglob("*.py")
        if deprecated in path.read_text()
    ]
    assert offenders == [], f"use app.clock helpers instead of {deprecated}: {offenders}"
