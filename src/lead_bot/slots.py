# lead-bot/src/lead_bot/slots.py
"""Works out which inspection times are open. Pure: no calendar calls and no clock.

booking.py fetches the busy times from Google Calendar and passes them in along with the current
time, so every rule here can be tested with fixed values.

How a day is cut into slots, using the placeholder rules in settings.py (Mon-Fri 8-5, 1-hour
inspection, 30 minutes to drive to the next job): each slot is a 90-minute block. The starts are
8:00, 9:30, 11:00, 12:30, 14:00 and 15:30; 15:30-16:30 is the last inspection that ends by
closing time.

Copied from booking-bot, with three changes: Slot is defined here (booking-bot kept it in its
questions module), labels are worded to be spoken by the call's voice, and earliest_per_day
picks the times to offer on a call.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Slot:
    """One bookable inspection time. `id` is internal; `label` is what the homeowner hears."""

    id: str
    label: str  # e.g. "Monday October 5 at 9:30 AM"


@dataclass(frozen=True)
class BookingRules:
    """When inspections can happen. One set per business."""

    timezone: ZoneInfo  # the business's local time; opening hours are in this zone
    open_weekdays: frozenset[int]  # Monday is 0 and Sunday is 6, as in date.weekday()
    opening_time: time
    closing_time: time
    appointment_length: timedelta
    gap_after_appointment: timedelta  # time kept free after each inspection (driving)
    minimum_notice: timedelta  # nothing is offered or booked that starts sooner than this from now
    days_ahead: int  # slots are offered for today and this many days after it

    @property
    def block_length(self) -> timedelta:
        """How much of the calendar one booking needs to be free: the inspection plus the gap."""
        return self.appointment_length + self.gap_after_appointment


@dataclass(frozen=True)
class BusyPeriod:
    """A stretch of time the calendar is already taken. Both ends are timezone-aware."""

    start: datetime
    end: datetime  # exclusive: a period ending at 9:00 doesn't block a 9:00 start


def candidate_starts(rules: BookingRules, now: datetime) -> list[datetime]:
    """Every start time the rules allow, from now until days_ahead, before looking at the calendar."""
    earliest_start = now + rules.minimum_notice
    today = now.astimezone(rules.timezone).date()
    starts: list[datetime] = []
    for day_offset in range(rules.days_ahead + 1):
        day = today + timedelta(days=day_offset)
        if day.weekday() not in rules.open_weekdays:
            continue
        start = datetime.combine(day, rules.opening_time, tzinfo=rules.timezone)
        closing = datetime.combine(day, rules.closing_time, tzinfo=rules.timezone)
        # The inspection itself must end by closing time.
        while start + rules.appointment_length <= closing:
            if start >= earliest_start:
                starts.append(start)
            start += rules.block_length
    return starts


def is_free(start: datetime, rules: BookingRules, busy_periods: Sequence[BusyPeriod]) -> bool:
    """True if nothing on the calendar overlaps this slot's block (the inspection plus its gap)."""
    block_end = start + rules.block_length
    # Two time ranges overlap when each one starts before the other one ends. Comparing aware
    # datetimes works across timezones: Python compares the instants, not the clock readings.
    return not any(busy.start < block_end and start < busy.end for busy in busy_periods)


def open_slots(rules: BookingRules, busy_periods: Sequence[BusyPeriod], now: datetime) -> list[Slot]:
    """The bookable slots, earliest first."""
    # busy_periods is a Sequence, not any Iterable, because is_free reads it once per candidate.
    # A generator passed here would be used up by the first candidate, and every later slot
    # would look free.
    return [
        make_slot(start, rules)
        for start in candidate_starts(rules, now)
        if is_free(start, rules, busy_periods)
    ]


def local_day(slot: Slot, timezone: ZoneInfo) -> date | None:
    """The business-local date a slot falls on, or None if its id isn't a slot id."""
    start = start_from_slot_id(slot.id)
    return None if start is None else start.astimezone(timezone).date()


def earliest_per_day(slots: Sequence[Slot], count: int, timezone: ZoneInfo) -> list[Slot]:
    """The earliest slot on each of the first `count` days that have one.

    Heard on a phone call, three times on three different days are easier to tell apart, and
    more likely to include one that fits, than 8 AM, 9:30 and 11 on the same morning.
    """
    chosen: list[Slot] = []
    days_used: set[date] = set()
    for slot in slots:  # already earliest first
        day = local_day(slot, timezone)
        if day is None or day in days_used:
            continue
        days_used.add(day)
        chosen.append(slot)
        if len(chosen) == count:
            break
    return chosen


# ---- slot ids and labels ----

# The id is the start time in UTC, so it means the same instant whoever reads it back, and
# booking.py can recover the time from the id alone. It's what Jev returns when the homeowner
# picks a time.
SLOT_ID_FORMAT = "slot-%Y%m%dT%H%MZ"


def slot_id_for(start: datetime) -> str:
    return start.astimezone(UTC).strftime(SLOT_ID_FORMAT)


def start_from_slot_id(slot_id: str) -> datetime | None:
    """The start time an id stands for, or None if it isn't a slot id this module made."""
    try:
        return datetime.strptime(slot_id, SLOT_ID_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def slot_label(start: datetime, timezone: ZoneInfo) -> str:
    """What the homeowner hears, in the business's local time: "Monday October 5 at 9:30 AM".

    Whole words, not abbreviations: a text-to-speech voice may read "Mon" as the word "mon".
    On the hour the minutes are left out ("at 8 AM"), the way a person says it.
    No comma inside a label: the offer joins three labels with commas.
    Built by hand rather than with strftime's %-d and %-I, which don't exist on Windows.
    """
    local_start = start.astimezone(timezone)
    hour_on_12_hour_clock = local_start.hour % 12 or 12  # midnight and noon are 12, not 0
    minutes = f":{local_start:%M}" if local_start.minute else ""
    am_or_pm = "AM" if local_start.hour < 12 else "PM"
    return f"{local_start:%A %B} {local_start.day} at {hour_on_12_hour_clock}{minutes} {am_or_pm}"


def make_slot(start: datetime, rules: BookingRules) -> Slot:
    return Slot(id=slot_id_for(start), label=slot_label(start, rules.timezone))
