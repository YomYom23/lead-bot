# lead-bot/src/lead_bot/dialer.py
"""The dialer: places each scheduled call when it comes due.

The form server only schedules calls (store.py). This separate process polls for calls that
are due and dials them one at a time. Right before each dial, it checks again:
  - the number isn't on the do-not-call list,
  - the lead's consent record says yes,
  - the lead is still being followed up,
  - the time is inside calling hours.
If a check fails, the call isn't placed. If a check can't run at all (a database error), the
exception stops the dialer, and again nothing is dialed.

Run it next to the server:
    uv run python -m lead_bot.dialer

Run exactly ONE dialer per database. Calls are claimed atomically, so two dialers wouldn't
dial the same call twice. But two could reach the same homeowner within seconds, through a
duplicate lead's call.

Phase 1 has no phone connection. main() uses DryRunCallPlacer, which logs the call it would
make and places nothing. lead_bot.simulate runs one dial through these same checks with a
simulated phone network instead, and phase 4 swaps in Twilio.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from dotenv import load_dotenv

from .settings import BusinessSettings, database_path_from_environment
from .store import DueCall, Store
from .timing import CallingHours, is_allowed, next_allowed_time, utc_now

logger = logging.getLogger(__name__)

# How often to look for due calls. The promise is a call within 60 seconds of the form, and
# polling every 2 seconds leaves nearly all of that for the call itself to connect.
POLL_INTERVAL_SECONDS = 2.0

Clock = Callable[[], datetime]


class CallPlacer(Protocol):
    def place_call(self, call: DueCall) -> str:
        """Start the call. Returns the provider's id for it (Twilio's CallSid).

        Raises if the provider refused the call. Returning means the provider accepted it, not
        that anyone answered: the outcome is reported later through Store.record_call_outcome.
        """
        ...


class DryRunCallPlacer:
    """Phase 1 stand-in: logs the call it would make and places nothing.

    No outcome is ever reported for these calls, so no follow-up is scheduled after them. A
    made-up "no answer" would schedule follow-ups for calls that never happened.
    """

    def place_call(self, call: DueCall) -> str:
        logger.warning(
            "DRY RUN, no phone call placed: would call lead %d (%s) at %s, attempt %d",
            call.lead_id,
            call.full_name,
            call.phone,
            call.attempt,
        )
        return f"dry-run-{call.call_id}"


def dial_next_due_call(store: Store, placer: CallPlacer, hours: CallingHours, clock: Clock) -> bool:
    """Check and dial the most overdue call. Returns False if nothing was due."""
    due_call = store.claim_due_call(clock())
    if due_call is None:
        return False

    # The checks, most serious first. Each failure cancels or moves the call; none of them dial.
    call_id = due_call.call_id
    if due_call.on_do_not_call:
        store.cancel_call(call_id, "number is on the do-not-call list", clock(), stop_lead_as="opted_out")
        logger.info("call %d cancelled: number opted out", call_id)
        return True
    if not due_call.consent_given:
        # Leads without consent never get a call scheduled, so reaching this means the data was
        # changed by hand or there's a bug. Stop, and tell the owner rather than guess.
        store.cancel_call(
            call_id, "no call consent on record", clock(), stop_lead_as="owner_alerted", alert_owner=True
        )
        logger.error("call %d cancelled: lead %d has no call consent on record", call_id, due_call.lead_id)
        return True
    if due_call.lead_status != "following_up":
        store.cancel_call(call_id, f"lead is {due_call.lead_status}", clock())
        logger.info("call %d cancelled: lead %d is %s", call_id, due_call.lead_id, due_call.lead_status)
        return True

    # Read the clock again rather than reusing the claim time: this is the moment of dialing,
    # so it's the time the calling-hours rule applies to.
    dial_time = clock()
    if not is_allowed(dial_time, hours):
        later = next_allowed_time(dial_time, hours)
        store.reschedule_call(due_call.call_id, later)
        logger.info("call %d moved to %s: outside calling hours", due_call.call_id, later.isoformat())
        return True

    try:
        provider_call_id = placer.place_call(due_call)
    except Exception as error:
        # Broad on purpose: whatever the provider rejected, this attempt is over. It counts as an
        # attempt, so the next follow-up is still scheduled, and the error is kept on the row.
        logger.exception("call %d: the phone provider refused it", due_call.call_id)
        store.record_call_outcome(
            due_call.call_id, "failed", clock(), hours, note=f"{type(error).__name__}: {error}"[:1000]
        )
        return True
    store.mark_call_placed(due_call.call_id, provider_call_id, dial_time)
    logger.info("call %d placed: lead %d, attempt %d", due_call.call_id, due_call.lead_id, due_call.attempt)
    return True


def close_interrupted_calls(store: Store, hours: CallingHours, clock: Clock) -> int:
    """Settle calls a previous dialer left mid-dial. Run once at startup.

    It's unknown whether such a call reached the phone network. The process may have died just
    after the provider accepted it. Dialing again now could ring the homeowner twice within
    seconds. So the attempt is recorded as failed instead. The normal schedule then places the
    next call, at least an hour away.
    """
    interrupted = store.interrupted_call_ids()
    for call_id in interrupted:
        store.record_call_outcome(
            call_id, "failed", clock(), hours, note="dialer stopped mid-dial; not redialed right away"
        )
    return len(interrupted)


def run_dialer(
    store: Store,
    placer: CallPlacer,
    hours: CallingHours,
    clock: Clock = utc_now,
    should_stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Dial due calls until should_stop() returns True. In production it never does."""
    interrupted_count = close_interrupted_calls(store, hours, clock)
    if interrupted_count:
        logger.warning("settled %d call(s) a previous dialer left mid-dial", interrupted_count)
    while not should_stop():
        # Sleep only when nothing was due, so a burst of due calls goes out without pauses.
        if not dial_next_due_call(store, placer, hours, clock):
            sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = BusinessSettings.from_environment()
    database_path = database_path_from_environment()
    logger.info(
        "dialer started (DRY RUN: no phone calls are placed); database=%s timezone=%s",
        database_path,
        settings.timezone.key,
    )
    try:
        run_dialer(Store(database_path), DryRunCallPlacer(), settings.calling_hours)
    except KeyboardInterrupt:
        # Ctrl+C. A call claimed at that moment is settled at the next start.
        logger.info("dialer stopped")


if __name__ == "__main__":
    main()
