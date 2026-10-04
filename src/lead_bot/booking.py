# lead-bot/src/lead_bot/booking.py
"""Offering and booking roof inspections against Google Calendar.

A call only needs two operations: "what's open?" and "book this slot id". This module answers
both, using the slot rules (slots.py) and the calendar (google_calendar.py).

Two protections live here:
  - Re-checking before booking. A slot was free when it was offered, but by the time the
    homeowner picks it, the owner or another caller may have taken it. So book() asks the
    calendar again right before creating the event.
  - One inspection per lead. The event id is derived from a booking key ("lead-12"). A lead who
    is called a second time, or a booking retried after an error, finds the event already made
    and confirms it instead of putting a second inspection on the calendar.

Copied from booking-bot, where the key was the Instagram message id.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .slots import (
    BookingRules,
    BusyPeriod,
    Slot,
    candidate_starts,
    is_free,
    open_slots,
    slot_label,
    start_from_slot_id,
)


@dataclass(frozen=True)
class BookingResult:
    ok: bool  # False when the slot was taken between offering it and the homeowner picking it
    slot_label: str = ""
    start: datetime | None = None


class CalendarUnavailable(Exception):
    """The calendar couldn't be reached or read: an outage, a timeout, or a bad response.

    A call answers this by handing the homeowner to a person ("someone will call you back").
    """


class Calendar(Protocol):
    """The calendar operations booking needs. GoogleCalendarClient is the real one; tests pass a fake."""

    def busy_periods(self, time_min: datetime, time_max: datetime) -> Sequence[BusyPeriod]: ...

    def booked_event_start(self, event_id: str) -> datetime | None: ...

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
    ) -> bool: ...


def event_id_for_booking_key(booking_key: str) -> str:
    """The calendar event id for the booking made under this key, e.g. "lead-12".

    Google only accepts ids made of the letters a-v and the digits 0-9, 5 to 1024 characters long.
    A SHA-256 hex digest (0-9 and a-f) always fits.
    """
    return hashlib.sha256(f"booking:{booking_key}".encode()).hexdigest()


class CalendarBooking:
    def __init__(
        self,
        rules: BookingRules,
        calendar: Calendar,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._rules = rules
        self._calendar = calendar
        self._clock = clock  # tests pass a fixed time
        # Makes the check-then-create in book() one step for this process. Several calls can be
        # live at once, each turn running on its own thread, and two homeowners could pick the
        # same slot at the same moment.
        self._booking_lock = threading.Lock()

    @property
    def rules(self) -> BookingRules:
        return self._rules

    def get_open_slots(self) -> list[Slot]:
        now = self._clock()
        # One query covers every day slots can be offered on. The extra day is a margin so the
        # last day's slots are fully inside the range.
        busy = self._calendar.busy_periods(now, now + timedelta(days=self._rules.days_ahead + 1))
        return open_slots(self._rules, busy, now)

    def book(
        self, slot_id: str, *, booking_key: str, summary: str, description: str, location: str
    ) -> BookingResult:
        """Create the event for this slot, or report it taken. Raises CalendarUnavailable."""
        with self._booking_lock:
            event_id = event_id_for_booking_key(booking_key)

            # 1. Already booked under this key: confirm the time that was actually booked, which
            #    may not be slot_id. This comes before the free check, which would see our own
            #    event as busy and report the slot taken.
            already_booked_start = self._calendar.booked_event_start(event_id)
            if already_booked_start is not None:
                return BookingResult(
                    ok=True,
                    slot_label=slot_label(already_booked_start, self._rules.timezone),
                    start=already_booked_start,
                )

            # 2. The slot must still be one the rules allow right now. Offered at the start of a
            #    call, it can be inside the minimum notice by the time the homeowner picks it.
            start = start_from_slot_id(slot_id)
            if start is None or start not in candidate_starts(self._rules, self._clock()):
                return BookingResult(ok=False)

            # 3. The re-check: the slot must still be free on the calendar.
            block_end = start + self._rules.block_length
            if not is_free(start, self._rules, self._calendar.busy_periods(start, block_end)):
                return BookingResult(ok=False)

            # 4. Book it.
            created = self._calendar.create_event(
                event_id=event_id,
                start=start,
                end=start + self._rules.appointment_length,
                timezone_name=self._rules.timezone.key,
                summary=summary,
                description=description,
                location=location,
            )
            if not created:
                # Google says the id is taken, yet step 1 found no live event under it: the event
                # was made and then deleted. Google doesn't let a deleted event's id be reused, and
                # booking again would undo the owner's deletion anyway, so a person takes over.
                raise CalendarUnavailable(f"the inspection for {booking_key} was booked once and deleted")
            return BookingResult(ok=True, slot_label=slot_label(start, self._rules.timezone), start=start)
