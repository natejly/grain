"""The English-first schedule composer: a sentence in, a cron and its future out.

Two halves, each tested against the boundary it owns:

- `next_fires` is the preview's clock, and the property worth proving is that
  it answers exactly as the ticker would — the same UTC minute-scan against the
  local wall clock, so a fall-back hour shifts the UTC instant and a
  spring-forward gap skips a day, here as in dispatch. A preview that computed
  fires any other way would be a promise the ticker does not keep.

- POST /api/crons/compile-schedule runs offline through the scripted provider's
  deterministic parser, and its contract is the `compile_document` argument in
  miniature: whatever the model answers goes through the same
  `_validate_schedule` a hand-typed cron passes, so a compiled schedule is
  never held to a lower standard than a typed one — and every refusal is one
  sentence a person can act on.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.workflows.schedule import next_fires
from app.services.workflows.validate import cron_error


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


# --------------------------------------------------------------------------
# next_fires: the pure helper
# --------------------------------------------------------------------------


def test_next_fires_walks_forward_from_the_next_minute() -> None:
    fires = next_fires("30 9 * * 1", "UTC", now=at("2026-08-22T00:00"))
    assert fires == [
        at("2026-08-24T09:30"),  # the Monday after a Saturday `now`
        at("2026-08-31T09:30"),
        at("2026-09-07T09:30"),
    ]


def test_next_fires_excludes_now_itself() -> None:
    # The scan starts at now + 1 minute: a cron matching this very minute has
    # already been the ticker's business, not the preview's.
    fires = next_fires("0 12 * * *", "UTC", count=1, now=at("2026-08-22T12:00"))
    assert fires == [at("2026-08-23T12:00")]


def test_next_fires_crosses_fall_back_like_the_ticker() -> None:
    # New York leaves DST on 2026-11-01: "9am local" is 13:00 UTC before the
    # change and 14:00 UTC after. The preview must show the shift, because the
    # ticker will fire on it.
    fires = next_fires("0 9 * * *", "America/New_York", now=at("2026-10-31T00:00"))
    assert fires == [
        at("2026-10-31T13:00"),
        at("2026-11-01T14:00"),
        at("2026-11-02T14:00"),
    ]


def test_next_fires_skips_the_spring_forward_gap() -> None:
    # 2:30am does not exist in New York on 2026-03-08 — the wall clock jumps
    # from 2:00 to 3:00 — so that day simply has no fire, exactly as dispatch
    # would see it.
    fires = next_fires("30 2 * * *", "America/New_York", now=at("2026-03-07T00:00"))
    assert fires == [
        at("2026-03-07T07:30"),  # EST, UTC-5
        at("2026-03-09T06:30"),  # EDT, UTC-4; March 8 is absent
        at("2026-03-10T06:30"),
    ]


def test_next_fires_bounds_a_cron_that_never_fires() -> None:
    # February 31st is a valid expression and an impossible date. The scan gives
    # up at its horizon and answers "no upcoming fires" rather than spinning.
    assert next_fires("0 0 31 2 *", "UTC", now=at("2026-08-22T00:00")) == []


def test_next_fires_answers_empty_rather_than_raising() -> None:
    now = at("2026-08-22T00:00")
    assert next_fires("not a cron", "UTC", now=now) == []
    assert next_fires("* * * * *", "Neverland/Nowhere", now=now) == []
    assert next_fires("* * * * *", "UTC", count=0, now=now) == []


# --------------------------------------------------------------------------
# POST /api/crons/compile-schedule
# --------------------------------------------------------------------------


def test_compile_schedule_is_deterministic_offline(client) -> None:
    body = {"text": "every weekday at 9am", "timezone": "America/New_York"}
    first = client.post("/api/crons/compile-schedule", json=body)
    second = client.post("/api/crons/compile-schedule", json=body)
    assert first.status_code == 200
    assert first.json() == second.json()
    data = first.json()
    assert data["schedule_cron"] == "0 9 * * 1-5"
    assert data["schedule_timezone"] == "America/New_York"
    assert len(data["next_fires"]) == 3
    for stamp in data["next_fires"]:
        datetime.fromisoformat(stamp)  # each row is a real instant


def test_compile_schedule_defaults_the_timezone_to_utc(client) -> None:
    response = client.post(
        "/api/crons/compile-schedule", json={"text": "every 30 minutes"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["schedule_cron"] == "*/30 * * * *"
    assert data["schedule_timezone"] == "UTC"


@pytest.mark.parametrize(
    "text",
    [
        "every weekday at 9am",
        "every 30 minutes",
        "every monday and thursday at 5:30pm",
        "monthly on the 15th",
        "every morning",
    ],
)
def test_compiled_answers_pass_the_hand_typed_standard(client, text: str) -> None:
    # The route promises that anything it returns 200 for would also have been
    # accepted typed by hand: a valid 5-field cron, a real zone, a real future.
    response = client.post(
        "/api/crons/compile-schedule", json={"text": text, "timezone": "Asia/Tokyo"}
    )
    assert response.status_code == 200
    data = response.json()
    assert cron_error(data["schedule_cron"]) is None
    ZoneInfo(data["schedule_timezone"])  # raises on an unknown zone
    assert 1 <= len(data["next_fires"]) <= 3


def test_prose_that_is_not_a_schedule_is_a_sentence_422(client) -> None:
    response = client.post(
        "/api/crons/compile-schedule", json={"text": "do the thing whenever"}
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "could not read" in detail
    assert "every weekday at 9am" in detail  # the refusal teaches the shape


def test_a_bad_cron_from_the_model_is_refused_by_the_validator(client) -> None:
    # The scripted parser translates "at 99:00" faithfully into an hour of 99 —
    # on purpose. What refuses it is `_validate_schedule`, the same chokepoint a
    # hand-typed cron passes, which is the whole point of the design.
    response = client.post(
        "/api/crons/compile-schedule", json={"text": "every day at 99:00"}
    )
    assert response.status_code == 422
    assert "outside" in response.json()["detail"]


def test_an_unknown_timezone_is_a_sentence_422(client) -> None:
    response = client.post(
        "/api/crons/compile-schedule",
        json={"text": "every morning", "timezone": "Neverland/Nowhere"},
    )
    assert response.status_code == 422
    assert "unknown timezone" in response.json()["detail"]


def test_blank_text_is_refused_before_any_model_call(client) -> None:
    empty = client.post("/api/crons/compile-schedule", json={"text": ""})
    assert empty.status_code == 422  # pydantic's min_length
    blank = client.post("/api/crons/compile-schedule", json={"text": "   "})
    assert blank.status_code == 422
    assert "describe the schedule" in blank.json()["detail"]
