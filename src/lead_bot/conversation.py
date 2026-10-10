# lead-bot/src/lead_bot/conversation.py
"""What the bot texts back after one text from the homeowner.

No database and no Twilio here. worker.py loads the conversation from the store, and this class
decides what one new text changes: the replies, the next stage, saved answers, owner alerts, a
booking. worker.py then saves all of it in one transaction (Store.save_turn). Keeping the
decisions apart from the saving is what lets the tests and the scenario runner drive whole
conversations.

The stages, one question each:
    address -> leak -> insurance -> homeowner -> offer -> booked
The first text asks the address question, so the homeowner's first reply answers it.

Every text the bot sends comes from text_script.py, except the writer's answers to off-script
questions ("how long does the inspection take?"). A draft is sent only if guard.py passes it;
otherwise the fixed "I'll pass that question to our team" line goes, with an owner alert.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .booking import CalendarBooking, CalendarUnavailable
from .google_calendar import GoogleAuthExpired
from .guard import REPLY_CHECKS, find_reply_problems
from .jev import Answer, JevError, choice_answer, yes_probability
from .leads import PROBLEM_LABELS
from .settings import BusinessSettings
from .slots import earliest_per_day, local_day
from .text_questions import NO_SLOT_OPTION, OFF_SCRIPT_OPTIONS, Stage, build_questions
from .text_script import (
    ADDRESS_WRONG_PREFIX,
    CALENDAR_DOWN_TEXT,
    CALENDAR_FULL_TEXT,
    CHANGE_BOOKING_TEXT,
    CONFUSION_PREFIX,
    INSURANCE_LINE,
    LATER_TEXT,
    LATER_TIMES_PREFIX,
    NO_TIME_SUITED_TEXT,
    NOT_CAUGHT_PREFIX,
    NOT_HOMEOWNER_TEXT,
    PRICE_LINE,
    QUESTION_FOR_TEAM_LINE,
    SLOT_TAKEN_PREFIX,
    SLOTS_KEPT_FILLING_TEXT,
    URGENT_LEAK_PREFIX,
    WRONG_PERSON_TEXT,
    booked_text,
    conversation_facts,
    later_times_text,
    not_understood_text,
    off_topic_text,
    opt_out_confirmation,
    picked_number,
    plain_punctuation,
    stage_question,
    technical_trouble_text,
    twilio_keyword,
    wants_human_text,
)
from .turn_data import ConversationState, LeadStatus, NewBooking, TurnOutcome, WriterOutcome
from .writer import NOT_IN_FACTS, Turn, Writer, WriterError

logger = logging.getLogger(__name__)

# A yes/no hazard counts at this probability or above.
HAZARD_THRESHOLD = 0.5
# A pick-one answer below this confidence is treated as "unclear" and the question is asked again.
MINIMUM_CHOICE_CONFIDENCE = 0.5
# Misunderstandings in a row before a person takes over, so a bad exchange can't loop forever.
MAX_UNCLEAR_IN_A_ROW = 2
TIMES_OFFERED = 3

# Clear ways of saying "stop texting me", caught without asking Jev. A bare STOP is handled
# before this (it's one of Twilio's keywords). A false match only ever costs texts, never sends
# one, so these lean broad. "Stop" counts only as the first word: "the leak won't stop" is a
# roofing answer. Jev's opt_out question catches what these miss.
OPT_OUT_PHRASES = re.compile(
    r"\b(stop texting|stop messaging|stop contacting|stop sending|stop these|do not text|don'?t text|"
    r"do not contact|don'?t contact|remove me|remove my number|take me off|unsubscribe|opt me out|"
    r"opt out|no more texts|no more messages)\b",
    re.IGNORECASE,
)
STARTS_WITH_STOP = re.compile(r"^\W*stop\b", re.IGNORECASE)

AskJev = Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Answer]]


def is_explicit_opt_out(text: str) -> bool:
    return bool(OPT_OUT_PHRASES.search(text) or STARTS_WITH_STOP.search(text))


@dataclass(frozen=True)
class ConversationServices:
    """What a conversation needs from outside: Jev, the calendar booking, and the writer.

    `booking` is a function rather than a CalendarBooking because the Google sign-in is only
    read when a conversation first needs the calendar. A missing or expired sign-in then fails
    that one booking (handed to a person) instead of stopping the whole worker.

    `writer` is optional: without one, an off-script question gets the fixed "I'll pass that
    question to our team" line. The --offline simulator and scenario runs work that way.
    """

    ask_jev: AskJev
    booking: Callable[[], CalendarBooking]
    writer: Writer | None = None


class Conversation:
    """Handles one homeowner text. Make a new one for each text; it isn't reused."""

    def __init__(
        self,
        state: ConversationState,
        history: Sequence[Turn],
        settings: BusinessSettings,
        services: ConversationServices,
    ) -> None:
        self._state = state
        self._history = list(history)  # the conversation before this text, oldest first
        self._settings = settings
        self._services = services
        self._booked_label = state.booked_label
        # Starts as "nothing changes" and is filled in as the text is handled.
        self._outcome = TurnOutcome(
            stage=state.stage,
            offered_slots=state.offered_slots,
            unclear_in_a_row=state.unclear_in_a_row,
            slot_taken_count=state.slot_taken_count,
        )

    def handle(self, text: str, last_try: bool = True) -> TurnOutcome:
        """What `text` changes.

        Raises JevError if Jev fails and this isn't the last try: the worker tries the same text
        again shortly. On the last try a person takes over instead.
        """
        stage = self._state.stage
        offered = self._state.offered_slots

        # Twilio acts on these single words itself (text_script.twilio_keyword), so the bot
        # never answers them. A bare STOP still opts the number out here too: Twilio's block
        # covers only the number it was sent to, while our do-not-call list covers every lead.
        keyword = twilio_keyword(text)
        if keyword == "stop":
            # No confirmation from us: Twilio has already sent its own.
            self._stop_texting(None, source=f"replied {text.strip().upper()}")
            return self._outcome
        if keyword is not None:
            # START (they weren't opted out, so nothing changes) or HELP (Twilio replies).
            return self._outcome

        # A clear "stop texting me" needs no model, and must work even when Jev is down.
        if is_explicit_opt_out(text):
            self._stop_texting(
                opt_out_confirmation(self._settings), source=f"asked to stop by text: {text[:80]!r}"
            )
            return self._outcome

        # "2" in reply to the numbered times needs no model to understand.
        if stage == "offer":
            number = picked_number(text, len(offered))
            if number is not None:
                self._book(offered[number - 1].id)
                return self._outcome

        last_bot_text = next((turn.text for turn in reversed(self._history) if turn.role == "assistant"), "")
        jev_state = {
            "stage": stage,
            "assistant_last_text": last_bot_text,
            "homeowner_latest_text": text,
            "offered_times": [slot.label for slot in offered],
        }
        try:
            answers = self._services.ask_jev(jev_state, build_questions(stage, offered))
        except JevError as error:
            if not last_try:
                raise
            logger.warning("lead %d: Jev failed on the last try: %s", self._state.lead_id, error)
            self._hand_off(
                technical_trouble_text(self._settings), result="jev_unavailable", alert="jev_unavailable"
            )
            return self._outcome

        # Hazards before anything else, most serious first.
        if yes_probability(answers, "opt_out") >= HAZARD_THRESHOLD:
            self._stop_texting(
                opt_out_confirmation(self._settings), source=f"asked to stop by text: {text[:80]!r}"
            )
        elif yes_probability(answers, "wrong_person") >= HAZARD_THRESHOLD:
            # Never asked for this, so they never consented: no more texts to this number.
            self._stop_texting(WRONG_PERSON_TEXT, source="wrong person", alert="wrong_person_texted")
        elif yes_probability(answers, "abusive") >= HAZARD_THRESHOLD:
            # No reply: answering abuse invites more. A person decides what, if anything, to send.
            self._close(result="abusive", lead_status="handed_off", alert="abusive_texts")
        elif yes_probability(answers, "wants_human") >= HAZARD_THRESHOLD:
            self._hand_off(wants_human_text(self._settings), result="wants_human", alert="asked_for_a_person")
        else:
            choice = choice_answer(answers, "answer")
            confident = choice is not None and choice.confidence >= MINIMUM_CHOICE_CONFIDENCE
            option = choice.choice if choice is not None and confident else "unclear"
            self._handle_answer(stage, option, text)
        return self._outcome

    # ---- the stages ----

    def _handle_answer(self, stage: Stage, option: str, text: str) -> None:
        if option in OFF_SCRIPT_OPTIONS:
            self._off_script(option, text)
            return
        self._outcome.unclear_in_a_row = 0

        if stage == "address":
            self._outcome.answers["address_confirmed"] = option
            if option == "no":
                # Booking still goes ahead; the owner confirms the right address before the visit.
                self._outcome.owner_alerts.append("address_needs_correcting")
                self._ask("leak", prefix=ADDRESS_WRONG_PREFIX)
            else:
                self._ask("leak", prefix="Thanks.")
        elif stage == "leak":
            self._outcome.answers["active_leak"] = option
            if option == "yes":
                self._outcome.owner_alerts.append("URGENT_active_leak")
                self._ask("insurance", prefix=URGENT_LEAK_PREFIX)
            else:
                self._ask("insurance")
        elif stage == "insurance":
            # Asked only to route the lead. Nothing about coverage is ever said back.
            self._outcome.answers["insurance_claim"] = option
            self._ask("homeowner")
        elif stage == "homeowner":
            self._outcome.answers["homeowner"] = option
            if option == "no":
                self._hand_off(NOT_HOMEOWNER_TEXT, result="not_homeowner", alert="not_the_homeowner")
            else:
                self._offer_times(prefix="Thanks.")
        elif stage == "offer":
            if option == NO_SLOT_OPTION:
                self._no_time_suited()
            elif option in {slot.id for slot in self._state.offered_slots}:
                self._book(option)
            else:
                self._off_script("unclear", text)
        else:  # booked
            if option == "change_booking":
                self._hand_off(CHANGE_BOOKING_TEXT, result="booked", alert="wants_to_change_booking")
            # "thanks" needs no reply: texting back "you're welcome" invites another round.

    def _off_script(self, option: str, text: str) -> None:
        if option == "insurance_question":
            self._reply_then_ask(INSURANCE_LINE)
        elif option == "price_question":
            self._reply_then_ask(PRICE_LINE)
        elif option == "other_question":
            self._outcome.unclear_in_a_row = 0
            self._answer_with_writer(text)
        elif option == "repeat":
            if self._outcome.stage == "booked" and self._booked_label is not None:
                self._reply(booked_text(self._booked_label, self._state.address))
            else:
                self._reply_then_ask(CONFUSION_PREFIX)
        elif option == "later":
            # Nothing more is asked. If they never come back, the worker hands the lead to a
            # person after a day (Store.close_quiet_conversations).
            self._outcome.unclear_in_a_row = 0
            self._reply(LATER_TEXT)
        elif option == "off_topic":
            # Counts like a misunderstanding, so flirting that won't stop ends with a person
            # rather than the bot redirecting forever. No owner alert for a single one.
            self._outcome.unclear_in_a_row += 1
            if self._outcome.unclear_in_a_row >= MAX_UNCLEAR_IN_A_ROW:
                self._hand_off(
                    not_understood_text(self._settings),
                    result="not_understood",
                    alert="bot_could_not_understand",
                )
            else:
                self._reply_then_ask(off_topic_text(self._settings))
        else:  # unclear
            self._outcome.unclear_in_a_row += 1
            if self._outcome.unclear_in_a_row >= MAX_UNCLEAR_IN_A_ROW:
                self._hand_off(
                    not_understood_text(self._settings),
                    result="not_understood",
                    alert="bot_could_not_understand",
                )
            else:
                self._reply_then_ask(NOT_CAUGHT_PREFIX)

    def _answer_with_writer(self, question: str) -> None:
        """An off-script question: a writer draft if it passes every check, else the fixed line.

        Three gates, the same as booking-bot's pipeline. A draft is sent only if the writer
        produced one, Jev could check it, and it passed every check. Any failure sends the fixed
        line and alerts the owner, so a person still answers the question.
        """
        writer = self._services.writer
        if writer is None:
            self._question_for_team("no_writer")
            return
        facts = conversation_facts(self._settings, self._state.address, self._booked_label)
        history = [*self._history, Turn("user", question)]
        started = time.monotonic()

        # Gate 1: the writer must produce a draft.
        draft: str | None
        try:
            draft = writer.write(facts=facts, history=history, reason="off_script_question")
        except WriterError as error:
            logger.warning("lead %d: writer failed: %s", self._state.lead_id, error)
            draft = None
        if draft is None:
            problems = ["writer_failed"]
        elif NOT_IN_FACTS in draft:
            # The facts don't answer it. The deferral is sent by code, which also alerts the
            # owner, so "our team will answer that" is never a promise nobody was told about.
            problems = ["not_in_facts"]
        else:
            # Curly quotes and long dashes would make the whole text UCS-2 (2-3x the cost).
            # Replaced before the guard, so it checks exactly what is sent.
            draft = plain_punctuation(draft)
            # Gate 2: Jev must be reachable to check it. If not, the draft is thrown away.
            check_state = {"FACTS": facts, "HOMEOWNER_QUESTION": question, "REPLY": draft}
            try:
                check_answers = self._services.ask_jev(check_state, REPLY_CHECKS)
            except JevError as error:
                logger.warning("lead %d: guard unavailable: %s", self._state.lead_id, error)
                problems = ["guard_unavailable"]
            else:
                # Gate 3: the draft must pass every check.
                problems = find_reply_problems(draft, check_answers)
        outcome = WriterOutcome(question, draft, tuple(problems), time.monotonic() - started)
        self._outcome.writer_outcomes.append(outcome)

        if not outcome.sent or draft is None:
            self._outcome.notes.append(f"writer draft not sent ({', '.join(problems)}): {draft!r}")
            self._question_for_team(problems[0])
            return
        # The answer, then the question the conversation was waiting on, so the script carries on.
        self._reply_then_ask(draft)

    def _question_for_team(self, why: str) -> None:
        """The fixed answer to a question the bot can't answer itself: a person follows up."""
        self._outcome.owner_alerts.append(f"homeowner_question_for_the_team:{why}")
        self._reply_then_ask(QUESTION_FOR_TEAM_LINE)

    def _no_time_suited(self) -> None:
        """None of the offered times work, or they asked for one that wasn't offered.

        The first time, say when inspections happen and offer times on later days: "Saturday at
        noon" or "none of those" usually means those days, not that no time could ever work.
        After that one re-offer, or when there are no later times, a person takes over.
        """
        already_reoffered = any(
            turn.role == "assistant" and turn.text.startswith(LATER_TIMES_PREFIX) for turn in self._history
        )
        if already_reoffered:
            self._hand_off(
                NO_TIME_SUITED_TEXT, result="no_time_suited", alert="needs_a_different_inspection_time"
            )
            return
        try:
            open_slots = self._services.booking().get_open_slots()
        except (CalendarUnavailable, GoogleAuthExpired) as error:
            self._calendar_failed(error)
            return
        timezone = self._settings.timezone
        offered_days = {local_day(slot, timezone) for slot in self._state.offered_slots}
        last_offered_day = max((day for day in offered_days if day is not None), default=None)
        later_slots = [
            slot
            for slot in open_slots
            if (day := local_day(slot, timezone)) is not None
            and (last_offered_day is None or day > last_offered_day)
        ]
        later = earliest_per_day(later_slots, TIMES_OFFERED, timezone)
        if not later:
            self._hand_off(
                NO_TIME_SUITED_TEXT, result="no_time_suited", alert="needs_a_different_inspection_time"
            )
            return
        self._outcome.offered_slots = tuple(later)
        self._ask("offer", prefix=later_times_text(self._settings))

    def _offer_times(self, prefix: str) -> None:
        try:
            open_slots = self._services.booking().get_open_slots()
        except (CalendarUnavailable, GoogleAuthExpired) as error:
            self._calendar_failed(error)
            return
        offered = earliest_per_day(open_slots, TIMES_OFFERED, self._settings.timezone)
        if not offered:
            self._hand_off(CALENDAR_FULL_TEXT, result="no_open_times", alert="no_open_inspection_times")
            return
        self._outcome.offered_slots = tuple(offered)
        self._ask("offer", prefix=prefix)

    def _book(self, slot_id: str) -> None:
        try:
            result = self._services.booking().book(
                slot_id,
                booking_key=f"lead-{self._state.lead_id}",
                summary=f"Roof inspection: {self._state.full_name}",
                description=self._event_description(),
                location=self._state.address,
            )
        except (CalendarUnavailable, GoogleAuthExpired) as error:
            self._calendar_failed(error)
            return
        if not result.ok or result.start is None:
            # Taken between offering and picking. Offer fresh times once; after that, a person.
            self._outcome.slot_taken_count += 1
            if self._outcome.slot_taken_count > 1:
                self._hand_off(
                    SLOTS_KEPT_FILLING_TEXT, result="slots_kept_filling", alert="needs_an_inspection_time"
                )
            else:
                self._offer_times(prefix=SLOT_TAKEN_PREFIX)
            return
        self._booked_label = result.slot_label
        self._outcome.booking = NewBooking(result.start, result.slot_label)
        self._outcome.offered_slots = ()
        self._outcome.stage = "booked"
        self._outcome.result = "booked"
        self._outcome.lead_status = "booked"
        self._outcome.unclear_in_a_row = 0
        # Not a problem, but the owner wants to know the moment a job lands on their calendar.
        self._outcome.owner_alerts.append("inspection_booked")
        self._reply(booked_text(result.slot_label, self._state.address))

    def _event_description(self) -> str:
        answers = self._state.answers
        problem = next(
            (label for value, label in PROBLEM_LABELS.items() if value == self._state.problem),
            self._state.problem,
        )
        lines = [
            f"Phone: {self._state.phone}",
            f"Problem (from the form): {problem}",
            f"Active leak: {answers.get('active_leak', 'not asked')}",
            f"Insurance claim: {answers.get('insurance_claim', 'not asked')}",
            f"Owns the home: {answers.get('homeowner', 'not asked')}",
        ]
        if answers.get("address_confirmed") == "no":
            lines.append("ADDRESS NOT CONFIRMED: contact the homeowner to confirm it before the visit.")
        lines.append(f"Booked by the AI text assistant for lead {self._state.lead_id}.")
        return "\n".join(lines)

    # ---- endings ----

    def _stop_texting(self, confirmation: str | None, source: str, alert: str | None = None) -> None:
        """Put the number on the do-not-call list, with at most one last text confirming it."""
        self._outcome.do_not_contact_source = source
        self._outcome.stop_confirmation = confirmation
        self._close(result="wrong_person" if alert == "wrong_person_texted" else "opted_out", alert=alert)

    def _calendar_failed(self, error: Exception) -> None:
        logger.error("lead %d: calendar failed: %s", self._state.lead_id, error)
        reason = "google_sign_in_expired" if isinstance(error, GoogleAuthExpired) else "calendar_unavailable"
        self._hand_off(CALENDAR_DOWN_TEXT, result=reason, alert=reason)

    def _hand_off(self, text: str, result: str, alert: str) -> None:
        self._reply(text)
        self._close(result=result, lead_status="handed_off", alert=alert)

    def _close(self, result: str, lead_status: LeadStatus | None = None, alert: str | None = None) -> None:
        self._outcome.close_conversation = True
        # A booking made earlier stands whatever happens after it, and the result says so.
        self._outcome.result = "booked" if self._booked_label is not None else result
        if lead_status is not None and self._booked_label is None:
            self._outcome.lead_status = lead_status
        if alert is not None:
            self._outcome.owner_alerts.append(alert)

    # ---- helpers ----

    def _ask(self, stage: Stage, prefix: str = "") -> None:
        self._outcome.stage = stage
        self._reply_then_ask(prefix)

    def _reply_then_ask(self, text: str) -> None:
        """`text`, then the question the current stage is waiting on (if it has one)."""
        question = stage_question(self._outcome.stage, self._state.address, self._outcome.offered_slots)
        self._reply(f"{text} {question}".strip())

    def _reply(self, text: str) -> None:
        self._outcome.replies.append(text)
