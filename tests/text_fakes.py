# lead-bot/tests/text_fakes.py
# Stand-ins for Jev, the writer and the sender, shared by the conversation, worker and scenario
# tests. Not a test module itself.
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from conftest import BUSINESS_NAME, HOURS, MONDAY_10AM, PHOENIX, make_consent, make_lead
from test_booking import FakeCalendar
from typesafe_sdk import ChoiceAnswer, NoulAnswer

from lead_bot.booking import CalendarBooking
from lead_bot.conversation import ConversationServices
from lead_bot.jev import Answer, JevError
from lead_bot.settings import BusinessSettings
from lead_bot.store import Store
from lead_bot.worker import SendRefused, Worker
from lead_bot.writer import Turn, Writer, WriterError

SETTINGS = BusinessSettings(business_name=BUSINESS_NAME, timezone=PHOENIX, texting_hours=HOURS)

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
    Running out of script fails the test: the bot asked Jev something the test didn't expect.
    """

    def __init__(self, *replies: str | tuple[str, Mapping[str, float]] | JevError) -> None:
        self._replies = list(replies)
        self.states: list[Mapping[str, Any]] = []

    @property
    def unused(self) -> int:
        return len(self._replies)

    def __call__(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> Mapping[str, Answer]:
        self.states.append(state)
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


class RecordingSender:
    """Keeps what the worker sends. `refuse` or `fail` make the next send fail that way."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []  # (phone, body)
        self.refuse: str | None = None
        self.fail: Exception | None = None

    @property
    def bodies(self) -> list[str]:
        return [body for _, body in self.sent]

    def send(self, phone: str, body: str) -> str:
        if self.refuse is not None:
            raise SendRefused(self.refuse)
        if self.fail is not None:
            raise self.fail
        self.sent.append((phone, body))
        return f"SM{len(self.sent)}"


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: datetime = MONDAY_10AM) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_services(
    jev: ScriptedJev, calendar: FakeCalendar | None = None, writer: Writer | None = None
) -> ConversationServices:
    # The calendar stays at Monday 10 AM whatever the worker's clock says, so the offered times
    # in a test never move.
    booking = CalendarBooking(
        SETTINGS.inspection_rules,
        calendar if calendar is not None else FakeCalendar(),
        clock=lambda: MONDAY_10AM,
    )
    return ConversationServices(ask_jev=jev, booking=lambda: booking, writer=writer)


def make_worker(
    store: Store,
    jev: ScriptedJev,
    clock: Clock,
    sender: RecordingSender | None = None,
    calendar: FakeCalendar | None = None,
    writer: Writer | None = None,
) -> tuple[Worker, RecordingSender]:
    sender = sender if sender is not None else RecordingSender()
    worker = Worker(store, SETTINGS, make_services(jev, calendar, writer), sender, clock=clock)
    return worker, sender


def start_conversation(store: Store, worker: Worker, clock: Clock) -> int:
    """Add a consenting lead and send its first text. Returns the lead id."""
    added = store.add_lead(make_lead(), make_consent(), clock(), HOURS)
    worker.run_once()
    return added.lead_id


def reply(store: Store, worker: Worker, clock: Clock, phone: str, text: str) -> None:
    """The homeowner texts back 30 seconds later, and the worker answers."""
    clock.now += timedelta(seconds=30)
    store.record_incoming_text(phone, text, f"SMIN{clock.now.timestamp()}", clock())
    worker.run_once()
