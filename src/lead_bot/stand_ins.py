# lead-bot/src/lead_bot/stand_ins.py
"""Stand-ins for Jev and Google Calendar, for running the bot with no keys and no sign-in.

The simulator's --offline mode and the scenario runner's --offline mode use these. They show the
conversation flow works, not how well the bot understands people: the keyword matching here is
crude on purpose. Real Jev handles the wording this misses, and the scenario runner without
--offline tests the real one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from typesafe_sdk import ChoiceAnswer, NoulAnswer

from .booking import CalendarBooking
from .conversation import ConversationServices
from .jev import Answer
from .settings import BusinessSettings
from .slots import BusyPeriod
from .text_questions import HAZARD_QUESTIONS


class OfflineJev:
    """Keyword matching in place of Jev. Called the same way: (state, questions) -> answers."""

    def __call__(self, state: Mapping[str, Any], questions: Mapping[str, Any]) -> Mapping[str, Answer]:
        if "answer" not in questions:
            # The guard's checks on a writer draft. Offline there's no writer, so this only runs
            # if someone adds one; every check passes, as the stand-in can't judge a draft.
            return {name: NoulAnswer(type="noul", noul=0.0) for name in questions}
        text = str(state["homeowner_latest_text"]).lower()
        criteria: Mapping[str, str] = questions["answer"].criteria
        answers: dict[str, Answer] = {name: NoulAnswer(type="noul", noul=0.0) for name in HAZARD_QUESTIONS}
        if "not interested" in text:
            answers["opt_out"] = NoulAnswer(type="noul", noul=0.9)
        if re.search(r"\b(real person|human|someone)\b", text):
            answers["wants_human"] = NoulAnswer(type="noul", noul=0.9)
        if re.search(r"(wrong number|never asked)", text):
            answers["wrong_person"] = NoulAnswer(type="noul", noul=0.9)
        if re.search(r"\b(go to hell|stupid|idiot)\b", text):
            answers["abusive"] = NoulAnswer(type="noul", noul=0.9)
        answers["answer"] = ChoiceAnswer(
            type="choice", choice=self._pick(text, criteria), confidence=0.9, probabilities={}
        )
        return answers

    @staticmethod
    def _pick(text: str, criteria: Mapping[str, str]) -> str:
        options = list(criteria)
        # Phone call stages (call_questions.py). A name or an address is whatever was said,
        # unless it's a question.
        if "roof" in options:
            roof_words = r"\b(roof|leak\w*|inspect\w*|repair|hail|storm|shingles?|estimate|quote|damage)\b"
            return "roof" if re.search(roof_words, text) else "message"
        if "gave_name" in options and not text.rstrip().endswith("?"):
            return "gave_name"
        if "gave_address" in options and not text.rstrip().endswith("?"):
            return "gave_address"
        slot_ids = [option for option in options if option.startswith("slot-")]
        if slot_ids:
            # Each slot's description is "2) Tuesday October 6 at 8 AM": match the time itself.
            for slot_id in slot_ids:
                label = criteria[slot_id].split(") ", 1)[-1].lower()
                if label in text:
                    return slot_id
            for position, word in enumerate(("first", "second", "third")):
                if word in text and position < len(slot_ids):
                    return slot_ids[position]
            if re.search(r"\blast\b", text):
                return slot_ids[-1]
            if re.search(r"\b(none|neither|different|another|instead|weekends?)\b", text):
                return "none"
        if re.search(r"\b(cute|single|date|gorgeous|handsome)\b", text):
            return "off_topic"
        if re.search(r"\b(cover|deductible|adjuster)\b", text):
            return "insurance_question"
        if re.search(r"\b(how much|cost|price)\b", text):
            return "price_question"
        if re.search(r"(what do you mean|huh\b|what\?)", text):
            return "repeat"
        if "change_booking" in options and re.search(r"\b(change|move|reschedule)\b", text):
            return "change_booking"
        if "different_address" in options and re.search(r"\b(moved|different address|new address)\b", text):
            return "different_address"
        if "different_address" in options and re.search(r"\b(already fixed|sold|no longer)\b", text):
            return "no"
        if text.rstrip().endswith("?"):
            return "other_question"
        if re.search(r"\b(busy|later|driving)\b", text):
            return "later"
        if "thanks" in options and re.search(r"\b(thanks|thx|thank you|ok|okay|k|great|bye)\b", text):
            return "thanks"
        if re.search(r"\b(not sure|don't know|no idea|maybe|haven't decided)\b", text):
            # Checked before yes/no: "not sure" contains "sure", which would read as a yes.
            return "unsure" if "unsure" in options else "unclear"
        if re.search(r"\b(no|nope|nothing|rent|manager|not \d+)\b", text):
            return "no" if "no" in options else "unclear"
        if re.search(r"\b(yes|yeah|yep|sure|correct|right|ok|okay|i do|i own|probably|my house)\b", text):
            return "yes" if "yes" in options else "unclear"
        return "unclear"


class MemoryCalendar:
    """An empty calendar in memory, in place of Google Calendar. Same three methods booking uses."""

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


def offline_services(settings: BusinessSettings) -> ConversationServices:
    """Keyword Jev, an empty in-memory calendar, and no writer (questions get the fixed text)."""
    booking = CalendarBooking(settings.inspection_rules, MemoryCalendar())
    return ConversationServices(ask_jev=OfflineJev(), booking=lambda: booking)
