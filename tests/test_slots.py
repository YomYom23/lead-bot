# lead-bot/tests/test_slots.py
# Slot generation: pure functions with a fixed "now", so no clock and no calendar.
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from lead_bot.slots import (
    BookingRules,
    BusyPeriod,
    candidate_starts,
    open_slots,
    slot_id_for,
    slot_label,
    start_from_slot_id,
)

PHOENIX = ZoneInfo("America/Phoenix")  # UTC-7 all year: Arizona doesn't use daylight saving

RULES = BookingRules(
    timezone=PHOENIX,
    open_weekdays=frozenset({0, 1, 2, 3, 4}),
    opening_time=time(9, 0),
    closing_time=time(17, 0),
    appointment_length=timedelta(minutes=45),
    gap_after_appointment=timedelta(minutes=15),
    minimum_notice=timedelta(hours=2),
    days_ahead=14,
)

# Monday 5 October 2026, 7:00 AM in Phoenix. With 2 hours' notice, 9:00 is the first bookable start.
MONDAY_7AM = datetime(2026, 10, 5, 7, 0, tzinfo=PHOENIX)


def phoenix(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=PHOENIX)


def local_times_on(day: int, now: datetime = MONDAY_7AM, busy: tuple[BusyPeriod, ...] = ()) -> list[str]:
    """The open start times on one October day, as "HH:MM" in Phoenix time."""
    slots = open_slots(RULES, busy, now)
    starts = [start_from_slot_id(slot.id) for slot in slots]
    return [
        f"{start.astimezone(PHOENIX):%H:%M}"
        for start in starts
        if start is not None and start.astimezone(PHOENIX).day == day
    ]


def test_a_full_weekday_has_hourly_starts_from_9_to_4() -> None:
    # 45-minute appointment + 15-minute gap = a slot every hour. 16:00-16:45 is the last that
    # ends by 17:00; a 17:00 start would end after closing.
    assert local_times_on(5) == ["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00"]


def test_weekends_have_no_slots() -> None:
    assert local_times_on(10) == []  # Saturday
    assert local_times_on(11) == []  # Sunday
    assert len(local_times_on(12)) == 8  # Monday again


def test_nothing_starts_sooner_than_the_minimum_notice() -> None:
    # At 10:30 the earliest allowed start is 12:30, so 13:00 is the first.
    assert local_times_on(5, now=phoenix(5, 10, 30))[0] == "13:00"


def test_slots_stop_after_days_ahead() -> None:
    starts = candidate_starts(RULES, MONDAY_7AM)
    last_day = max(start.astimezone(PHOENIX).date() for start in starts)
    assert str(last_day) == "2026-10-19"  # today + 14 days, a Monday


def test_a_busy_period_removes_only_the_slots_it_overlaps() -> None:
    meeting = BusyPeriod(phoenix(5, 10, 30), phoenix(5, 11, 0))  # inside the 10:00 slot's block
    times = local_times_on(5, busy=(meeting,))
    assert "10:00" not in times
    assert "09:00" in times and "11:00" in times


def test_busy_time_in_the_gap_still_blocks_the_slot() -> None:
    # 9:50 is after the 9:00 appointment ends (9:45) but inside the 15 minutes kept free after it.
    errand = BusyPeriod(phoenix(5, 9, 50), phoenix(5, 10, 0))
    assert "09:00" not in local_times_on(5, busy=(errand,))


def test_busy_period_ending_exactly_at_a_start_does_not_block_it() -> None:
    # Busy periods end exclusively: a meeting until 9:00 leaves a 9:00 start free.
    early_meeting = BusyPeriod(phoenix(5, 8, 0), phoenix(5, 9, 0))
    assert local_times_on(5, busy=(early_meeting,))[0] == "09:00"


def test_busy_times_in_utc_are_compared_as_instants() -> None:
    # Google returns busy times in UTC. 17:00 UTC is 10:00 in Phoenix.
    in_utc = BusyPeriod(datetime(2026, 10, 5, 17, 0, tzinfo=UTC), datetime(2026, 10, 5, 17, 30, tzinfo=UTC))
    times = local_times_on(5, busy=(in_utc,))
    assert "10:00" not in times and "09:00" in times


def test_slot_id_round_trips_to_the_same_instant() -> None:
    start = phoenix(5, 9)
    slot_id = slot_id_for(start)
    assert slot_id == "slot-20261005T1600Z"  # 9:00 in Phoenix is 16:00 UTC
    assert start_from_slot_id(slot_id) == start


def test_anything_that_is_not_a_slot_id_reads_as_none() -> None:
    assert start_from_slot_id("none") is None
    assert start_from_slot_id("slot-garbage") is None


def test_labels_read_naturally_in_local_time() -> None:
    # Spoken on a call, so whole words, and no ":00" on the hour.
    assert slot_label(phoenix(5, 9), PHOENIX) == "Monday October 5 at 9 AM"
    assert slot_label(phoenix(5, 12), PHOENIX) == "Monday October 5 at 12 PM"  # noon is 12, not 0
    assert slot_label(phoenix(16, 16), PHOENIX) == "Friday October 16 at 4 PM"
    assert slot_label(phoenix(5, 9) + timedelta(minutes=30), PHOENIX) == "Monday October 5 at 9:30 AM"
    # A UTC start is shown in the business's time.
    assert slot_label(datetime(2026, 10, 5, 16, 0, tzinfo=UTC), PHOENIX) == "Monday October 5 at 9 AM"
