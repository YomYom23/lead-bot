# lead-bot/src/lead_bot/simulate.py
"""A phone in the terminal: dial a lead the way the dialer would, then talk to the bot.

    uv run python -m lead_bot.simulate --new-lead
    uv run python -m lead_bot.simulate --new-lead --offline

What's real: the database, the dialer's last-moment checks, the TwiML (built, then read back the
way Twilio would), the live-call WebSocket, and the call logic. With --offline, Jev and Google
Calendar are swapped for the crude stand-ins below. Without it, they're the real ones, which
needs TYPESAFE_API_KEY in .env and a Google sign-in (uv run python -m lead_bot.google_login).

What's simulated: Twilio. You type what the homeowner says; a line that's just a digit is a key
press. The opening is printed, where Twilio would speak it.

The server runs inside this process on a free local port, unless --server points at one that's
already running.
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import uvicorn
from dotenv import load_dotenv
from typesafe_sdk import ChoiceAnswer, NoulAnswer
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from .booking import CalendarBooking
from .call_questions import HAZARD_QUESTIONS
from .call_script import CHECKING_LINE, call_twiml
from .call_session import CallServices
from .consent import CONSENT_VERSION, ConsentEvidence, consent_text
from .dialer import dial_next_due_call
from .jev import Answer
from .leads import parse_lead_form
from .server import create_app
from .settings import BusinessSettings, database_path_from_environment
from .slots import BusyPeriod
from .store import DueCall, Store
from .timing import utc_now
from .wiring import build_call_services

REPLY_WAIT_SECONDS = 20.0  # Jev plus the calendar, worst case
FOLLOW_UP_MESSAGE_WAIT_SECONDS = 0.5  # e.g. the "end" that follows a goodbye
KEYPRESS = re.compile(r"^[0-9*#]$")


class SimulatedPhoneNetwork:
    """Stands where Twilio's call API will (phase 4): accepts a call and keeps its TwiML."""

    def __init__(self, settings: BusinessSettings) -> None:
        self._settings = settings
        self.placed: tuple[DueCall, str] | None = None

    def place_call(self, call: DueCall) -> str:
        self.placed = (call, call_twiml(self._settings, call.call_id, call.relay_token))
        return f"SIM{call.call_id}"


def read_twiml(twiml: str) -> tuple[str, str, dict[str, str]]:
    """What Twilio takes from the TwiML: the WebSocket URL, the opening, and the parameters."""
    relay = ElementTree.fromstring(twiml).find("./Connect/ConversationRelay")
    if relay is None:
        raise ValueError("the TwiML has no <Connect><ConversationRelay>")
    parameters = {element.attrib["name"]: element.attrib["value"] for element in relay.iter("Parameter")}
    return relay.attrib["url"], relay.attrib["welcomeGreeting"], parameters


# ---- the --offline stand-ins ----


class OfflineJev:
    """Keyword matching in place of Jev, for trying the call flow with no TypeSafe key.

    Crude on purpose: it shows the flow works, not how well the bot understands people. Jev
    handles the wording this misses, and phase 3's scenario runner tests the real one.
    """

    def __call__(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> Mapping[str, Answer]:
        words = str(state["homeowner_latest_words"]).lower()
        options = list(questions["answer"].criteria)
        answers: dict[str, Answer] = {
            name: NoulAnswer(type="noul", noul=0.0) for name in HAZARD_QUESTIONS
        }
        if "not interested" in words:
            answers["opt_out"] = NoulAnswer(type="noul", noul=0.9)
        if re.search(r"\b(real person|human|someone)\b", words):
            answers["wants_human"] = NoulAnswer(type="noul", noul=0.9)
        if "wrong number" in words:
            answers["wrong_person"] = NoulAnswer(type="noul", noul=0.9)
        answers["answer"] = ChoiceAnswer(
            type="choice", choice=self._pick(words, options), confidence=0.9, probabilities={}
        )
        return answers

    @staticmethod
    def _pick(words: str, options: list[str]) -> str:
        slot_ids = [option for option in options if option.startswith("slot-")]
        if slot_ids:
            for position, word in enumerate(("first", "second", "third")):
                if word in words and position < len(slot_ids):
                    return slot_ids[position]
            if re.search(r"\b(none|neither|different|another)\b", words):
                return "none"
        if re.search(r"\b(cover|deductible|adjuster)\b", words):
            return "insurance_question"
        if re.search(r"\b(how much|cost|price)\b", words):
            return "price_question"
        if re.search(r"\b(repeat|say that again|what\?)", words):
            return "repeat"
        if words.rstrip().endswith("?"):
            return "other_question"
        if "busy" in words or "later" in words or "bad time" in words:
            return "no_time" if "no_time" in options else "unclear"
        if re.search(r"\b(not sure|don't know|no idea)\b", words) and "unsure" in options:
            return "unsure"
        if re.search(r"\b(no|nope|nothing|rent)\b", words):
            return "no" if "no" in options else "unclear"
        if re.search(r"\b(yes|yeah|yep|sure|correct|right|ok|okay|i do|i own|go ahead)\b", words):
            if "yes_has_more" in options:
                return "yes_has_more"
            return "yes" if "yes" in options else "unclear"
        return "unclear"


class MemoryCalendar:
    """An empty calendar in memory, in place of Google Calendar."""

    def __init__(self) -> None:
        self.events: dict[str, tuple[datetime, datetime, str]] = {}

    def busy_periods(self, time_min: datetime, time_max: datetime) -> list[BusyPeriod]:
        return [
            BusyPeriod(start, end)
            for start, end, _ in self.events.values()
            if start < time_max and time_min < end
        ]

    def booked_event_start(self, event_id: str) -> datetime | None:
        event = self.events.get(event_id)
        return None if event is None else event[0]

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
        if event_id in self.events:
            return False
        self.events[event_id] = (start, end, summary)
        return True


def offline_services(settings: BusinessSettings) -> CallServices:
    booking = CalendarBooking(settings.inspection_rules, MemoryCalendar())
    return CallServices(ask_jev=OfflineJev(), booking=lambda: booking)


# ---- running it ----


def free_local_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def start_embedded_server(store: Store, settings: BusinessSettings, services: CallServices) -> uvicorn.Server:
    app = create_app(store, settings, call_services=services)
    port = int(settings.public_base_url.rsplit(":", 1)[1])
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(200):
        if server.started:
            return server
        threading.Event().wait(0.05)
    raise RuntimeError("the embedded server didn't start")


def add_simulated_lead(store: Store, settings: BusinessSettings, now: datetime) -> int:
    lead = parse_lead_form("Dana Ruiz", "602-555-0142", "12 Saguaro Ln, Mesa AZ", "storm_damage")
    consent = ConsentEvidence(
        given=True,
        version=CONSENT_VERSION,
        text_shown=consent_text(settings.business_name),
        ip_address="127.0.0.1",
        forwarded_for=None,
        user_agent="lead_bot.simulate",
        source="simulator",
    )
    added = store.add_lead(lead, consent, now, settings.calling_hours)
    print(f"[simulator] added lead {added.lead_id}: {added.decision}")
    return added.lead_id


def talk(url: str, opening: str, parameters: dict[str, str], provider_call_id: str) -> None:
    with connect(url) as websocket:
        websocket.send(json.dumps({
            "type": "setup",
            "callSid": provider_call_id,
            "direction": "outbound-api",
            "customParameters": parameters,
        }))
        print(f"BOT: {opening}")
        while True:
            line = sys.stdin.readline()
            if not line:  # end of input: hang up
                print("[simulator] (you hung up)")
                return
            words = line.strip()
            if not sys.stdin.isatty():
                print(f"YOU: {words}")  # piped input isn't echoed by the terminal
            if not words:
                continue
            if KEYPRESS.match(words):
                websocket.send(json.dumps({"type": "dtmf", "digit": words}))
                wait = 2.0  # most keys get no answer at all
            else:
                prompt = {"type": "prompt", "voicePrompt": words, "lang": "en-US", "last": True}
                websocket.send(json.dumps(prompt))
                wait = REPLY_WAIT_SECONDS
            if print_replies(websocket, wait, sent_at=time.monotonic()):
                return


def print_replies(websocket: ClientConnection, wait: float, sent_at: float) -> bool:
    """Print everything the bot says this turn. Returns True once the bot has hung up.

    A turn can be more than one message: a goodbye is a "text" followed by an "end". After the
    first message, anything else from the same turn arrives within moments.

    Each reply shows how long it took. On a real call that's the silence the homeowner hears
    after they stop talking (plus Twilio's own speech-to-text and text-to-speech time).
    """
    while True:
        try:
            message = json.loads(websocket.recv(timeout=wait))
        except TimeoutError:
            return False
        except ConnectionClosed:
            print("[simulator] the line went dead")
            return True
        wait = FOLLOW_UP_MESSAGE_WAIT_SECONDS
        if message.get("type") == "text":
            print(f"BOT ({time.monotonic() - sent_at:.2f}s): {message['token']}")
            if message["token"] == CHECKING_LINE:
                wait = REPLY_WAIT_SECONDS  # the writer's answer follows, after a model call
        elif message.get("type") == "end":
            print(f"[simulator] the bot hung up: {json.loads(message.get('handoffData') or '{}')}")
            return True
        else:
            print(f"[simulator] unexpected message: {message}")


def print_summary(store: Store, call_id: int, lead_id: int) -> None:
    print("\n[simulator] ---- after the call ----")
    print(f"call result:  {store.call_result(call_id)}")
    print(f"lead status:  {store.lead_status(lead_id)}")
    print(f"answers:      {store.lead_answers(lead_id)}")
    print(f"booking:      {store.booking_label(lead_id)}")
    print(f"do-not-call:  {store.is_on_do_not_call('+16025550142')}")
    alerts = [alert.reason for alert in store.owner_alerts() if alert.lead_id == lead_id]
    print(f"owner alerts: {alerts}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--new-lead", action="store_true", help="add a consenting test lead first")
    parser.add_argument("--offline", action="store_true", help="keyword stand-ins for Jev and the calendar")
    parser.add_argument("--server", help="use this running server (e.g. http://127.0.0.1:8000)")
    parser.add_argument(
        "--pretend-now",
        type=datetime.fromisoformat,
        help="the time the dialer's calling-hours check sees, e.g. 2026-10-05T10:00-07:00. "
        "Only the dial decision uses it; this simulator can't place a real call.",
    )
    arguments = parser.parse_args()

    load_dotenv()
    settings = BusinessSettings.from_environment()
    store = Store(database_path_from_environment())
    if arguments.server:
        settings = replace(settings, public_base_url=arguments.server)
    else:
        settings = replace(settings, public_base_url=f"http://127.0.0.1:{free_local_port()}")
        services = offline_services(settings) if arguments.offline else build_call_services(settings)
        start_embedded_server(store, settings, services)
        mode = "offline stand-ins" if arguments.offline else "real Jev and Google Calendar"
        print(f"[simulator] server on {settings.public_base_url} ({mode})")

    pretend_now: datetime | None = arguments.pretend_now
    if pretend_now is not None and pretend_now.tzinfo is None:
        parser.error("--pretend-now needs a UTC offset, e.g. 2026-10-05T10:00-07:00")

    def dial_clock() -> datetime:
        return pretend_now.astimezone(UTC) if pretend_now is not None else utc_now()

    if arguments.new_lead:
        add_simulated_lead(store, settings, dial_clock())
    network = SimulatedPhoneNetwork(settings)
    if not dial_next_due_call(store, network, settings.calling_hours, dial_clock):
        next_due = store.next_due_call_time()
        if next_due is None:
            raise SystemExit("[simulator] no call is scheduled. Add one with --new-lead.")
        raise SystemExit(
            f"[simulator] nothing to dial yet: the next call is due at "
            f"{next_due.astimezone(settings.timezone):%a %b %d %I:%M %p} ({settings.timezone.key}). "
            "Pass --pretend-now to dial it as if it were that time."
        )
    if network.placed is None:
        raise SystemExit(f"[simulator] the dialer didn't dial (outside calling hours?). Now: {dial_clock()}")
    due_call, twiml = network.placed
    url, opening, parameters = read_twiml(twiml)
    print(f"[simulator] ringing {due_call.phone} (call {due_call.call_id}, attempt {due_call.attempt})")
    talk(url, opening, parameters, f"SIM{due_call.call_id}")
    threading.Event().wait(0.5)  # let the server finish recording the end of the call
    print_summary(store, due_call.call_id, due_call.lead_id)


if __name__ == "__main__":
    main()
