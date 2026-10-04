# lead-bot/tests/call_fakes.py
# Stand-ins for Jev and the calendar, shared by the call tests. Not a test module itself.
from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from conftest import BUSINESS_NAME, HOURS, MONDAY_10AM, PHOENIX, make_consent, make_lead
from test_booking import FakeCalendar
from typesafe_sdk import ChoiceAnswer, NoulAnswer

from lead_bot.booking import CalendarBooking
from lead_bot.call_session import CallServices, CallSession
from lead_bot.jev import Answer, JevError
from lead_bot.settings import BusinessSettings
from lead_bot.store import ConnectedCall, Store
from lead_bot.writer import Turn, Writer, WriterError

SETTINGS = BusinessSettings(
    business_name=BUSINESS_NAME,
    timezone=PHOENIX,
    calling_hours=HOURS,
    callback_phone="+14805550199",
)

# Put in a script where the homeowner picks a time: replaced by the first time actually offered.
FIRST_OFFERED_TIME = "<first offered time>"
# Put in a script where the guard checks a writer draft (a Jev call with no "answer" question).
GUARD_CHECK = "<guard check>"


class ScriptedJev:
    """Answers each Jev call with the next scripted reply, and records what it was asked.

    A reply is an option id for the "answer" question, optionally with yes/no answers set, e.g.
    ("yes", {}) or ("unclear", {"opt_out": 0.9}). JevError in the script raises it instead.
    The guard's reply checks are a Jev call too, with no "answer" question: script it as
    (GUARD_CHECK, {"price_talk": 0.9}), or just GUARD_CHECK for a clean pass.
    Running out of script fails the test: the call asked Jev something the test didn't expect.
    """

    def __init__(self, *replies: str | tuple[str, Mapping[str, float]] | JevError) -> None:
        self._replies = list(replies)
        self.states: list[Mapping[str, Any]] = []
        # Set to hold the next call inside Jev until released, like a slow response.
        self.hold: threading.Event | None = None
        self.inside_jev = threading.Event()

    def __call__(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> Mapping[str, Answer]:
        self.states.append(state)
        self.inside_jev.set()
        if self.hold is not None:
            assert self.hold.wait(timeout=5)
        reply = self._replies.pop(0) if self._replies else None
        assert reply is not None, f"Jev was asked about {dict(state)!r} unscripted"
        if isinstance(reply, JevError):
            raise reply
        option, yes_probabilities = (reply, {}) if isinstance(reply, str) else reply
        is_guard_call = "answer" not in questions
        assert is_guard_call == (option == GUARD_CHECK), (
            f"script says {option!r}, Jev was asked {list(questions)}"
        )
        answers: dict[str, Answer] = {
            name: NoulAnswer(type="noul", noul=yes_probabilities.get(name, 0.0))
            for name in questions
            if name != "answer"
        }
        if is_guard_call:
            return answers
        if option == FIRST_OFFERED_TIME:
            option = next(iter(questions["answer"].criteria))  # the offered times come first
        answers["answer"] = ChoiceAnswer(type="choice", choice=option, confidence=0.9, probabilities={})
        return answers


class StubWriter:
    """The writer, scripted: returns each draft in turn (or raises WriterError), and records
    the facts and history it was given."""

    def __init__(self, *drafts: str | WriterError) -> None:
        self._drafts = list(drafts)
        self.calls: list[tuple[str, list[Turn]]] = []

    def write(self, *, facts: str, history: Sequence[Turn], reason: str) -> str:
        self.calls.append((facts, list(history)))
        assert self._drafts, "the writer was asked for a draft the test didn't script"
        draft = self._drafts.pop(0)
        if isinstance(draft, WriterError):
            raise draft
        return draft


def connect_new_call(store: Store, now: datetime = MONDAY_10AM) -> tuple[ConnectedCall, str]:
    """Add a consenting lead and take its first call as far as connected. Returns (call, token)."""
    store.add_lead(make_lead(), make_consent(), now, HOURS)
    due_call = store.claim_due_call(now)
    assert due_call is not None
    store.mark_call_placed(due_call.call_id, f"SIM{due_call.call_id}", now)
    connected = store.connect_call(due_call.call_id, due_call.relay_token, now)
    assert connected is not None
    return connected, due_call.relay_token


def make_services(
    jev: ScriptedJev, calendar: FakeCalendar, now: datetime = MONDAY_10AM, writer: Writer | None = None
) -> CallServices:
    booking = CalendarBooking(SETTINGS.inspection_rules, calendar, clock=lambda: now)
    return CallServices(ask_jev=jev, booking=lambda: booking, writer=writer)


def make_session(
    store: Store, jev: ScriptedJev, calendar: FakeCalendar | None = None, writer: Writer | None = None
) -> tuple[CallSession, ConnectedCall, FakeCalendar]:
    connected, _ = connect_new_call(store)
    calendar = calendar if calendar is not None else FakeCalendar()
    services = make_services(jev, calendar, writer=writer)
    session = CallSession(connected, SETTINGS, store, services, clock=lambda: MONDAY_10AM)
    return session, connected, calendar
