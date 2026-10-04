# lead-bot/tests/test_booking.py
# CalendarBooking against an in-memory fake calendar: the re-check before booking, one inspection
# per lead on retries, and two homeowners picking the same slot.
from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from lead_bot.booking import BookingResult, CalendarBooking, CalendarUnavailable, event_id_for_booking_key
from lead_bot.slots import BookingRules, BusyPeriod, slot_id_for

PHOENIX = ZoneInfo("America/Phoenix")
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
MONDAY_7AM = datetime(2026, 10, 5, 7, 0, tzinfo=PHOENIX)
MONDAY_10AM = datetime(2026, 10, 5, 10, 0, tzinfo=PHOENIX)
TEN_AM_SLOT = slot_id_for(MONDAY_10AM)


@dataclass
class CreatedEvent:
    event_id: str
    start: datetime
    end: datetime
    location: str
    deleted: bool = False


class FakeCalendar:
    """Holds events in memory and behaves like Google for the three calls booking makes.

    `pause_after_free_check` lets the race test hold one booking between its free check and its
    create, which is exactly where two bookings could interleave.
    """

    def __init__(self, busy: Sequence[BusyPeriod] = ()) -> None:
        self.other_busy = list(busy)  # things already on the calendar that the bot didn't make
        self.events: list[CreatedEvent] = []
        self.pause_after_free_check: threading.Event | None = None
        # One flag per free check, set as each one starts, so the race test can tell whether a
        # second booking got as far as its check.
        self.free_check_started = [threading.Event(), threading.Event()]
        self._free_check_count = 0
        self._count_lock = threading.Lock()

    def busy_periods(self, time_min: datetime, time_max: datetime) -> list[BusyPeriod]:
        busy = [*self.other_busy, *(BusyPeriod(e.start, e.end) for e in self.events if not e.deleted)]
        result = [period for period in busy if period.start < time_max and time_min < period.end]
        with self._count_lock:
            if self._free_check_count < len(self.free_check_started):
                self.free_check_started[self._free_check_count].set()
            self._free_check_count += 1
        if self.pause_after_free_check is not None:
            self.pause_after_free_check.wait(timeout=5)
        return result

    def booked_event_start(self, event_id: str) -> datetime | None:
        return next((e.start for e in self.events if e.event_id == event_id and not e.deleted), None)

    def create_event(
        self,
        *,
        event_id: str,
        start: datetime,
        end: datetime,
        timezone_name: str,
        summary: str,
        description: str,
        location: str,
    ) -> bool:
        if any(e.event_id == event_id for e in self.events):  # Google: 409 duplicate, even if deleted
            return False
        self.events.append(CreatedEvent(event_id, start, end, location))
        return True


def booking_on(calendar: FakeCalendar, now: datetime = MONDAY_7AM) -> CalendarBooking:
    return CalendarBooking(RULES, calendar, clock=lambda: now)


def book(booking: CalendarBooking, slot_id: str, booking_key: str = "lead-1") -> BookingResult:
    return booking.book(
        slot_id,
        booking_key=booking_key,
        summary="Roof inspection: Dana Ruiz",
        description="Booked on an AI call.",
        location="123 Main St, Phoenix",
    )


# ---- offering ----


def test_open_slots_skip_what_the_calendar_already_has() -> None:
    dentist = BusyPeriod(MONDAY_10AM, MONDAY_10AM + timedelta(hours=1))
    slots = booking_on(FakeCalendar(busy=[dentist])).get_open_slots()
    labels = [slot.label for slot in slots[:3]]
    assert labels == ["Monday October 5 at 9 AM", "Monday October 5 at 11 AM", "Monday October 5 at 12 PM"]


# ---- booking ----


def test_booking_a_free_slot_creates_one_event_at_the_address() -> None:
    calendar = FakeCalendar()
    result = book(booking_on(calendar), TEN_AM_SLOT)

    assert result == BookingResult(ok=True, slot_label="Monday October 5 at 10 AM", start=MONDAY_10AM)
    [event] = calendar.events
    assert event.event_id == event_id_for_booking_key("lead-1")
    assert event.start == MONDAY_10AM
    assert event.end - event.start == timedelta(minutes=45)  # the gap isn't part of the event
    assert event.location == "123 Main St, Phoenix"


def test_a_slot_that_filled_since_it_was_offered_is_reported_taken() -> None:
    # Offered at 7 AM; by the time the homeowner picks it, the owner has put something there.
    calendar = FakeCalendar()
    booking = booking_on(calendar)
    assert TEN_AM_SLOT in [slot.id for slot in booking.get_open_slots()]
    calendar.other_busy.append(BusyPeriod(MONDAY_10AM, MONDAY_10AM + timedelta(minutes=30)))

    assert book(booking, TEN_AM_SLOT).ok is False
    assert calendar.events == []


def test_a_slot_that_is_now_too_soon_is_reported_taken() -> None:
    # Offered at 7 AM, picked at 8:30 AM: 10:00 is now inside the 2 hours' notice.
    calendar = FakeCalendar()
    late_answer = booking_on(calendar, now=datetime(2026, 10, 5, 8, 30, tzinfo=PHOENIX))
    assert book(late_answer, TEN_AM_SLOT).ok is False
    assert calendar.events == []


# The last one is a real time, but not on the hourly grid the rules produce.
OFF_GRID_SLOT = slot_id_for(MONDAY_10AM + timedelta(minutes=30))


@pytest.mark.parametrize("slot_id", ["none", "slot-garbage", OFF_GRID_SLOT])
def test_ids_that_are_not_bookable_slots_are_refused(slot_id: str) -> None:
    calendar = FakeCalendar()
    assert book(booking_on(calendar), slot_id).ok is False
    assert calendar.events == []


def test_booking_again_for_the_same_lead_confirms_the_first_booking() -> None:
    """The bug this prevents: the event is created, then the call drops before the homeowner
    hears the confirmation. Booking again for that lead must not see its own event as busy and
    report the time as taken."""
    calendar = FakeCalendar()
    booking = booking_on(calendar)
    first_attempt = book(booking, TEN_AM_SLOT)
    retry = book(booking, TEN_AM_SLOT)

    assert first_attempt.ok and retry.ok
    assert retry.slot_label == first_attempt.slot_label
    assert len(calendar.events) == 1


def test_a_lead_already_booked_is_told_the_time_actually_booked() -> None:
    calendar = FakeCalendar()
    booking = booking_on(calendar)
    book(booking, TEN_AM_SLOT)
    eleven_am_slot = slot_id_for(MONDAY_10AM + timedelta(hours=1))
    retry = book(booking, eleven_am_slot)

    assert retry.slot_label == "Monday October 5 at 10 AM"  # what's on the calendar, not what was asked
    assert len(calendar.events) == 1


def test_a_deleted_booking_is_not_silently_rebooked() -> None:
    # The owner deleted the event the first booking made. Google won't reuse the id, and booking
    # it again would undo the owner's decision: hand it to a person instead.
    calendar = FakeCalendar()
    booking = booking_on(calendar)
    book(booking, TEN_AM_SLOT)
    calendar.events[0].deleted = True
    with pytest.raises(CalendarUnavailable, match="booked once and deleted"):
        book(booking, TEN_AM_SLOT)


def test_two_homeowners_picking_the_same_slot_get_one_event() -> None:
    """Two bookings for the 10:00 slot at once, each held at the worst moment: after its free
    check, before its create. Exactly one may win; the other must be told the slot was taken."""
    calendar = FakeCalendar()
    booking = booking_on(calendar)
    hold = threading.Event()
    calendar.pause_after_free_check = hold
    results: dict[str, BookingResult] = {}

    def book_for(booking_key: str) -> None:
        results[booking_key] = book(booking, TEN_AM_SLOT, booking_key=booking_key)

    first = threading.Thread(target=book_for, args=("lead-a",))
    second = threading.Thread(target=book_for, args=("lead-b",))
    first.start()
    assert calendar.free_check_started[0].wait(timeout=5)  # lead-a is inside its free check
    second.start()
    # Give lead-b the chance to reach its own free check while lead-a is still paused there.
    # With the lock it can't (it waits for lead-a), so this times out. Without the lock it gets
    # there, both checks see the slot free, and both would create an event.
    calendar.free_check_started[1].wait(timeout=0.5)
    hold.set()  # let both continue
    first.join(timeout=5)
    second.join(timeout=5)

    assert sorted(result.ok for result in results.values()) == [False, True]
    assert len(calendar.events) == 1


def test_event_ids_follow_googles_rules() -> None:
    # Google: only a-v and 0-9, 5 to 1024 characters.
    event_id = event_id_for_booking_key("lead-12")
    assert 5 <= len(event_id) <= 1024
    assert set(event_id) <= set("0123456789abcdefghijklmnopqrstuv")
    assert event_id_for_booking_key("lead-1") == event_id_for_booking_key("lead-1")
    assert event_id_for_booking_key("lead-1") != event_id_for_booking_key("lead-2")


def test_utc_now_and_local_rules_agree() -> None:
    # The real clock returns UTC. 14:00 UTC is 7:00 in Phoenix, the same as MONDAY_7AM.
    seven_am_in_utc = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)
    utc_booking = CalendarBooking(RULES, FakeCalendar(), clock=lambda: seven_am_in_utc)
    assert utc_booking.get_open_slots()[0].label == "Monday October 5 at 9 AM"
