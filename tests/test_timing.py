# lead-bot/tests/test_timing.py
# Calling hours and the follow-up schedule. Fixed times, no real clock.
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from lead_bot.timing import (
    MAX_CALL_ATTEMPTS,
    CallingHours,
    clock_label,
    follow_up_call_time,
    is_allowed,
    next_allowed_time,
)

PHOENIX = ZoneInfo("America/Phoenix")  # UTC-7 all year: no daylight saving
DENVER = ZoneInfo("America/Denver")  # has daylight saving
HOURS = CallingHours(PHOENIX)  # 8:00 to 20:30


def phoenix(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=PHOENIX)


# ---- the window ----


@pytest.mark.parametrize(
    ("moment", "allowed"),
    [
        (phoenix(2026, 10, 5, 7, 59, 59), False),
        (phoenix(2026, 10, 5, 8, 0), True),  # the rule bans calls BEFORE 8, so 8:00 is fine
        (phoenix(2026, 10, 5, 13, 0), True),
        (phoenix(2026, 10, 5, 20, 29, 59), True),
        (phoenix(2026, 10, 5, 20, 30), False),  # the safety margin starts here
        (phoenix(2026, 10, 5, 23, 0), False),
        (phoenix(2026, 10, 5, 2, 0), False),
    ],
)
def test_is_allowed_at_the_edges(moment: datetime, allowed: bool) -> None:
    assert is_allowed(moment, HOURS) is allowed


def test_the_window_is_judged_in_the_business_timezone_not_utc() -> None:
    # 16:00 UTC is 9:00 in Phoenix: allowed, even though 16:00 sounds like the afternoon.
    assert is_allowed(datetime(2026, 10, 5, 16, 0, tzinfo=UTC), HOURS)
    # 14:00 UTC is 7:00 in Phoenix: too early.
    assert not is_allowed(datetime(2026, 10, 5, 14, 0, tzinfo=UTC), HOURS)


def test_naive_datetimes_are_refused() -> None:
    with pytest.raises(ValueError, match="no timezone"):
        is_allowed(datetime(2026, 10, 5, 12, 0), HOURS)


@pytest.mark.parametrize(
    ("earliest", "latest"),
    [(time(7, 30), time(20, 0)), (time(8, 0), time(21, 30)), (time(12, 0), time(12, 0))],
)
def test_settings_outside_the_legal_window_are_refused(earliest: time, latest: time) -> None:
    with pytest.raises(ValueError):
        CallingHours(PHOENIX, earliest=earliest, latest=latest)


def test_a_narrower_window_is_accepted() -> None:
    narrow = CallingHours(PHOENIX, earliest=time(9, 0), latest=time(18, 0))
    assert not is_allowed(phoenix(2026, 10, 5, 8, 30), narrow)


# ---- the next opening ----


def test_inside_the_window_the_call_is_due_now() -> None:
    moment = phoenix(2026, 10, 5, 10, 15)
    assert next_allowed_time(moment, HOURS) == moment


def test_early_morning_waits_until_eight_the_same_day() -> None:
    assert next_allowed_time(phoenix(2026, 10, 5, 6, 40), HOURS) == phoenix(2026, 10, 5, 8, 0)


def test_late_evening_waits_until_eight_the_next_day() -> None:
    assert next_allowed_time(phoenix(2026, 10, 5, 22, 10), HOURS) == phoenix(2026, 10, 6, 8, 0)


def test_the_next_opening_is_returned_in_utc() -> None:
    result = next_allowed_time(phoenix(2026, 10, 5, 22, 10), HOURS)
    assert result.utcoffset() == timedelta(0)


def test_the_opening_stays_at_eight_across_a_daylight_saving_change() -> None:
    # Denver moved its clocks back on 1 November 2026 at 2 a.m. A form at 10 p.m. on 31 October
    # (MDT, UTC-6) must be called at 8 a.m. MST (UTC-7) the next day, which is 15:00 UTC. Adding
    # 10 hours to the UTC time instead would give 7 a.m. local: an illegal call.
    denver_hours = CallingHours(DENVER)
    result = next_allowed_time(datetime(2026, 10, 31, 22, 0, tzinfo=DENVER), denver_hours)
    assert result.astimezone(DENVER).time() == time(8, 0)
    assert result == datetime(2026, 11, 1, 15, 0, tzinfo=UTC)


# ---- follow-ups ----


def test_follow_ups_are_measured_from_the_first_call() -> None:
    first_call = phoenix(2026, 10, 5, 10, 0)
    after_first = follow_up_call_time(first_call, 1, now=phoenix(2026, 10, 5, 10, 1), hours=HOURS)
    assert after_first == phoenix(2026, 10, 5, 12, 0)  # 2 hours
    after_second = follow_up_call_time(first_call, 2, now=phoenix(2026, 10, 5, 12, 1), hours=HOURS)
    assert after_second == phoenix(2026, 10, 6, 10, 0)  # 1 day
    after_third = follow_up_call_time(first_call, 3, now=phoenix(2026, 10, 6, 10, 1), hours=HOURS)
    assert after_third == phoenix(2026, 10, 8, 10, 0)  # 3 days


def test_no_call_after_the_last_attempt() -> None:
    first_call = phoenix(2026, 10, 5, 10, 0)
    now = phoenix(2026, 10, 8, 10, 1)
    assert follow_up_call_time(first_call, MAX_CALL_ATTEMPTS, now, HOURS) is None


def test_a_follow_up_that_lands_at_night_moves_to_the_morning() -> None:
    # First call at 7:30 p.m.; 2 hours later is 9:30 p.m., outside the window.
    first_call = phoenix(2026, 10, 5, 19, 30)
    result = follow_up_call_time(first_call, 1, now=phoenix(2026, 10, 5, 19, 31), hours=HOURS)
    assert result == phoenix(2026, 10, 6, 8, 0)


def test_overdue_follow_ups_are_spaced_out_not_fired_together() -> None:
    # The dialer was down for two days. The 1-day follow-up's planned time has long passed, but
    # it waits at least an hour after the last call instead of ringing right away.
    first_call = phoenix(2026, 10, 5, 10, 0)
    now = phoenix(2026, 10, 7, 11, 0)
    result = follow_up_call_time(first_call, 2, now=now, hours=HOURS)
    assert result == now + timedelta(hours=1)


def test_clock_label() -> None:
    assert clock_label(phoenix(2026, 10, 5, 8, 0), PHOENIX) == "8:00 AM"
    assert clock_label(phoenix(2026, 10, 5, 12, 5), PHOENIX) == "12:05 PM"
    assert clock_label(phoenix(2026, 10, 5, 0, 30), PHOENIX) == "12:30 AM"
