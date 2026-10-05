# lead-bot/src/lead_bot/simulate.py
"""A phone in the terminal: you are the homeowner, texting the bot.

    uv run python -m lead_bot.simulate --offline     # keyword stand-ins, no keys needed
    uv run python -m lead_bot.simulate               # real Jev and the real writer

It adds a consenting test lead, then runs the real worker (worker.py) against a fresh database:
the first text, your replies, the reminders, the booking. What's real: the database, the
worker's checks, the conversation logic, and (without --offline) Jev and the writer. What's not:
Twilio (texts are printed, and what you type goes straight into the database the way phase 2's
webhook will put it there), and the calendar, which is always in memory.

Commands at the prompt:
    /wait 2h       pretend that much time passes with no reply (2h, 30m, 1d), so reminders and
                   the hand-off of quiet conversations can be seen
    /state         show where the conversation is, the answers saved, and the owner alerts
    /quit          stop
"""

from __future__ import annotations

import argparse
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .booking import CalendarBooking
from .consent import CONSENT_VERSION, ConsentEvidence, consent_text
from .conversation import ConversationServices
from .leads import parse_lead_form
from .settings import BusinessSettings
from .stand_ins import MemoryCalendar, OfflineJev
from .store import Store
from .timing import TextingHours, next_allowed_time, utc_now
from .worker import Sender, Worker

PHOENIX = ZoneInfo("America/Phoenix")
# Fixed settings, the same as the scenario runner's, so a run never depends on what's in .env.
# The callback number is the default placeholder from settings.py.
SIMULATED_SETTINGS = BusinessSettings(
    business_name="Desert Ridge Roofing LLC", timezone=PHOENIX, texting_hours=TextingHours(PHOENIX)
)
SIMULATED_PHONE = "(602) 555-0142"
WAIT_COMMAND = re.compile(r"^/wait\s+(\d+)\s*([mhd])$", re.IGNORECASE)
WAIT_UNITS = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}


class SimulatedClock:
    """The worker's clock. Starts at the real time (moved into texting hours) and moves on /wait."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class TerminalSender:
    """Prints each text as it would arrive on the homeowner's phone."""

    def __init__(self, clock: SimulatedClock, settings: BusinessSettings) -> None:
        self._count = 0
        self._clock = clock
        self._settings = settings

    def send(self, phone: str, body: str) -> str:
        self._count += 1
        local_time = self._clock().astimezone(self._settings.timezone)
        print(f"\n  [{self._settings.business_name}, {local_time:%a %H:%M}]")
        for line in body.splitlines():
            print(f"  {line}")
        return f"SIM{self._count}"


def add_simulated_lead(store: Store, settings: BusinessSettings, now: datetime) -> int:
    lead = parse_lead_form("Dana Ruiz", SIMULATED_PHONE, "12 Saguaro Ln, Mesa AZ", "storm_damage")
    consent = ConsentEvidence(
        given=True,
        version=CONSENT_VERSION,
        text_shown=consent_text(settings.business_name),
        ip_address="127.0.0.1",
        forwarded_for=None,
        user_agent="simulator",
        source="simulate.py",
    )
    return store.add_lead(lead, consent, now, settings.texting_hours).lead_id


def build_services(
    offline: bool, settings: BusinessSettings, clock: SimulatedClock
) -> tuple[ConversationServices, str]:
    """Jev and the writer (real or stand-in), with an in-memory calendar either way.

    The calendar runs on the simulated clock, so /wait moves the inspection times on too.
    """
    booking = CalendarBooking(settings.inspection_rules, MemoryCalendar(), clock=clock)
    if offline:
        return ConversationServices(ask_jev=OfflineJev(), booking=lambda: booking), "offline stand-ins"
    from .wiring import build_jev, build_writer, require_model_keys

    require_model_keys()
    services = ConversationServices(ask_jev=build_jev().ask, booking=lambda: booking, writer=build_writer())
    return services, "real Jev and writer, in-memory calendar"


def print_state(store: Store, lead_id: int) -> None:
    conversation = store.conversation(lead_id)
    print(f"  lead status: {store.lead_status(lead_id)}")
    if conversation is not None:
        print(f"  stage: {conversation.stage}; result: {store.conversation_result(lead_id)}")
    print(f"  answers: {store.lead_answers(lead_id)}")
    print(f"  booking: {store.booking_label(lead_id)}")
    print(f"  owner alerts: {[alert.reason for alert in store.owner_alerts()]}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--offline", action="store_true", help="keyword stand-ins for Jev, and no writer")
    arguments = parser.parse_args()
    load_dotenv()
    settings = SIMULATED_SETTINGS
    # Start inside texting hours, so the first text goes at once even when run late at night.
    clock = SimulatedClock(next_allowed_time(utc_now(), settings.texting_hours))
    services, mode = build_services(arguments.offline, settings, clock)
    sender: Sender = TerminalSender(clock, settings)

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work_directory:
        store = Store(Path(work_directory) / "simulated.db")
        worker = Worker(store, settings, services, sender, clock=clock)
        lead_id = add_simulated_lead(store, settings, clock())
        print(f"[simulator] {mode}. You are Dana Ruiz, {SIMULATED_PHONE}. /wait 2h, /state, /quit.")
        worker.run_once()
        message_number = 0
        while True:
            try:
                line = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line == "/quit":
                break
            if line == "/state":
                print_state(store, lead_id)
                continue
            wait = WAIT_COMMAND.match(line)
            if wait is not None:
                clock.now += int(wait.group(1)) * WAIT_UNITS[wait.group(2).lower()]
                print(f"  [{clock.now.astimezone(settings.timezone):%a %H:%M}]")
                worker.run_once()
                continue
            if line.startswith("/"):
                print("  commands: /wait 2h, /state, /quit")
                continue
            message_number += 1
            clock.now += timedelta(seconds=30)  # a reply takes a moment to type
            # What phase 2's webhook does with a text from Twilio: save it, then the worker answers.
            store.record_incoming_text(lead_phone(store, lead_id), line, f"SIMIN{message_number}", clock())
            if not worker.run_once().texts_sent:
                print("  (no reply)")
        print_state(store, lead_id)


def lead_phone(store: Store, lead_id: int) -> str:
    conversation = store.conversation(lead_id)
    if conversation is None:
        raise SystemExit("[simulator] the test lead has no conversation")
    return conversation.phone


if __name__ == "__main__":
    main()
