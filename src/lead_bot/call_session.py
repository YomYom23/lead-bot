# lead-bot/src/lead_bot/call_session.py
"""One live call: what the bot says after each thing the homeowner says or presses.

No networking here. relay.py receives Twilio's messages and calls on_prompt / on_keypress /
on_hangup; this class answers with what to say and whether to end the call. Keeping it free of
the WebSocket is what lets the tests (and the scenario runner in phase 3) drive whole calls.

The call goes through these stages, one question each:
    permission -> address -> leak -> insurance -> homeowner -> offer -> anything_else
Almost every sentence the bot says comes from this file or call_script.py: Jev classifies what
the homeowner said, and the reply is chosen from the result. The one exception is an off-script
question with no fixed answer ("how long does the inspection take?"). That turn has two parts:
  1. on_prompt says "Let me check." and returns ContinueTurn, so the homeowner isn't left in
     silence while a model works.
  2. continue_turn has writer.py draft an answer, guard.py check it, then speaks the draft, or
     the fixed "I'll pass that to our team" line if the draft failed any check or anything
     failed. Unchecked text is never spoken.

Thread safety: relay.py runs on_prompt in a worker thread (it waits on Jev, which blocks), and
on_keypress can arrive while that's in progress. A 9 must end the call at once, not after Jev
answers, so on_keypress never waits for on_prompt. The short _state_lock guards the fields
both of them touch, and on_prompt re-checks `ended` before acting on Jev's answer.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .booking import CalendarBooking, CalendarUnavailable
from .call_questions import NO_SLOT_OPTION, OFF_SCRIPT_OPTIONS, Stage, build_call_questions
from .call_script import (
    CHECKING_LINE,
    INSURANCE_LINE,
    OPT_OUT_DIGIT,
    PERMISSION_QUESTION,
    PRICE_LINE,
    QUESTION_FOR_TEAM_LINE,
    call_facts,
    opening_greeting,
    speakable_phone_numbers,
)
from .google_calendar import GoogleAuthExpired
from .guard import REPLY_CHECKS, find_reply_problems
from .jev import Answer, JevError, choice_answer, yes_probability
from .leads import PROBLEM_LABELS
from .settings import BusinessSettings
from .slots import Slot, earliest_per_day
from .store import ConnectedCall, LeadStatus, Store
from .writer import NOT_IN_FACTS, Turn, Writer, WriterError

logger = logging.getLogger(__name__)

# A yes/no hazard counts at this probability or above.
HAZARD_THRESHOLD = 0.5
# A pick-one answer below this confidence is treated as "unclear" and the question is asked again.
MINIMUM_CHOICE_CONFIDENCE = 0.5
# Misunderstandings in a row before a person takes over, so a bad line can't loop forever.
MAX_UNCLEAR_IN_A_ROW = 2
TIMES_OFFERED = 3

# Explicit ways of saying "stop calling me", caught without waiting for Jev. Anywhere in what
# they said. A false match only ever costs a call, never sends one, so these lean broad.
OPT_OUT_PHRASES = re.compile(
    r"\b(stop calling|stop these calls|stop the calls|do not call|don'?t call|remove me|"
    r"remove my number|take me off|unsubscribe|opt out|no more calls)\b",
    re.IGNORECASE,
)
# "Stop" as the first word, which is how people follow the opening's "say stop". Not "stop"
# anywhere: "the leak won't stop" is a roofing answer, not an opt-out. Jev's opt_out question
# catches the opt-outs this misses, like "I'm not interested, quit bugging me".
STARTS_WITH_STOP = re.compile(r"^\W*stop\b", re.IGNORECASE)

AskJev = Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Answer]]


def is_explicit_opt_out(words: str) -> bool:
    return bool(OPT_OUT_PHRASES.search(words) or STARTS_WITH_STOP.search(words))


@dataclass(frozen=True)
class Say:
    text: str


@dataclass(frozen=True)
class EndCall:
    reason: str


@dataclass(frozen=True)
class ContinueTurn:
    """Send what came before this, then call continue_turn() for the rest of the turn.

    Used after "Let me check.", so the filler is heard while the writer and guard work.
    """


Action = Say | EndCall | ContinueTurn


@dataclass(frozen=True)
class WriterOutcome:
    """What happened to one off-script question. The scenario report counts these."""

    question: str
    draft: str | None  # None when the writer failed before producing one
    problems: tuple[str, ...]  # failed guard checks, or "writer_failed" / "guard_unavailable"
    seconds: float  # writer plus guard, the silence after "Let me check."

    @property
    def spoken(self) -> bool:
        return self.draft is not None and not self.problems


@dataclass(frozen=True)
class CallServices:
    """What a call needs from outside: Jev, the calendar booking, and the writer.

    `booking` is a function rather than a CalendarBooking because the Google sign-in is only
    read when a call first needs the calendar. A missing or expired sign-in then fails that one
    booking (GoogleAuthExpired, handed to a person) instead of stopping the whole server.

    `writer` is optional: without one, an off-script question gets the fixed "our team will
    answer that" line, as in phase 2. The --offline simulator runs that way.
    """

    ask_jev: AskJev
    booking: Callable[[], CalendarBooking]
    writer: Writer | None = None


class CallSession:
    def __init__(
        self,
        call: ConnectedCall,
        settings: BusinessSettings,
        store: Store,
        services: CallServices,
        clock: Callable[[], datetime],
    ) -> None:
        self._call = call
        self._settings = settings
        self._store = store
        self._services = services
        self._clock = clock
        self._state_lock = threading.Lock()
        self.stage: Stage = "permission"
        self.ended = False
        self.offered_slots: list[Slot] = []
        self.booked_label: str | None = None
        self._unclear_in_a_row = 0
        self._jev_failures_in_a_row = 0
        self._slot_taken_count = 0
        # What was said on the call, for the writer. Filled in by _log.
        self._history: list[Turn] = []
        # The off-script question continue_turn() still has to answer: the words, the stage it
        # was asked in, and the call so far. Set by on_prompt, taken (and cleared) by continue_turn.
        self._pending_question: tuple[str, Stage, list[Turn]] | None = None
        self.writer_outcomes: list[WriterOutcome] = []
        self._last_said = opening_greeting(settings)
        # Twilio speaks the opening itself, before we hear anything, so it's logged here.
        self._log("bot", self._last_said)

    # ---- events from relay.py ----

    def on_keypress(self, digit: str) -> list[Action]:
        """A key on the phone keypad. 9 opts out and ends the call immediately."""
        self._log("keypad", digit)
        if digit != OPT_OUT_DIGIT:
            return []  # other keys mean nothing on this call
        with self._state_lock:
            if self.ended:
                return []
            self.ended = True
        return self._opt_out(f"pressed {OPT_OUT_DIGIT} on call {self._call.call_id}")

    def on_prompt(self, words: str) -> list[Action]:
        """One complete thing the homeowner said, as Twilio transcribed it."""
        self._log("homeowner", words)
        with self._state_lock:
            if self.ended:
                return []
            stage = self.stage
            offered_slots = list(self.offered_slots)
            last_said = self._last_said

        # Explicit opt-outs first, without waiting for Jev. The rule says opting out must end
        # the call immediately, and a Jev round trip is not immediately.
        if is_explicit_opt_out(words):
            return self._end_by_opt_out(f"said stop on call {self._call.call_id}")

        state = {
            "stage": stage,
            "assistant_last_said": last_said,
            "homeowner_latest_words": words,
            "offered_times": [slot.label for slot in offered_slots],
        }
        try:
            answers = self._services.ask_jev(state, build_call_questions(stage, offered_slots))
        except JevError as error:
            logger.warning("call %d: Jev failed: %s", self._call.call_id, error)
            return self._after_jev_failure()

        with self._state_lock:
            if self.ended:  # a 9 was pressed while Jev was thinking: say nothing more
                return []
            self._jev_failures_in_a_row = 0

        # Hazards before anything else, most serious first.
        if yes_probability(answers, "opt_out") >= HAZARD_THRESHOLD:
            return self._end_by_opt_out(f"asked to stop on call {self._call.call_id}")
        if yes_probability(answers, "wrong_person") >= HAZARD_THRESHOLD:
            # Never asked for this, so they never consented: no more calls to this number.
            self._store.add_to_do_not_call(
                self._call.phone, f"wrong person on call {self._call.call_id}", self._clock()
            )
            return self._finish(
                "Sorry about that. We won't call this number again. Goodbye.",
                result="wrong_person",
                owner_alert="wrong_person_on_call",
            )
        if yes_probability(answers, "abusive") >= HAZARD_THRESHOLD:
            return self._finish(
                "I'm going to end the call now. Goodbye.",
                result="abusive",
                lead_status="handed_off",
                owner_alert="abusive_caller",
            )
        if yes_probability(answers, "wants_human") >= HAZARD_THRESHOLD:
            return self._finish(
                f"Of course. Someone from {self._settings.business_name} will call you back shortly. "
                "Goodbye.",
                result="wants_human",
                lead_status="handed_off",
                owner_alert="asked_for_a_person",
            )

        choice = choice_answer(answers, "answer")
        if choice is None or choice.confidence < MINIMUM_CHOICE_CONFIDENCE:
            option = "unclear"
        else:
            option = choice.choice
        return self._handle(stage, option, offered_slots, words)

    def continue_turn(self) -> list[Action]:
        """The second half of an off-script question's turn, after "Let me check." was sent.

        Three gates, the same as booking-bot's pipeline. A draft is spoken only if the writer
        produced one, Jev could check it, and it passed every check. Any failure speaks the fixed
        line and alerts the owner, so a person still answers the question after the call.
        """
        with self._state_lock:
            if self.ended or self._pending_question is None:
                return []  # a 9 or a hang-up arrived during "Let me check."
            question, stage, history = self._pending_question
            self._pending_question = None
            booked_label = self.booked_label

        writer = self._services.writer
        if writer is None:  # build_call_services always sets one; the offline simulator doesn't
            return self._question_for_team(stage, "no_writer")
        facts = call_facts(self._settings, self._call.address, booked_label)
        started = time.monotonic()

        # Gate 1: the writer must produce a draft.
        try:
            draft: str | None = writer.write(facts=facts, history=history, reason="off_script_question")
        except WriterError as error:
            logger.warning("call %d: writer failed: %s", self._call.call_id, error)
            draft = None
        if draft is None:
            problems: list[str] = ["writer_failed"]
        elif NOT_IN_FACTS in draft:
            # The facts don't answer it. The deferral is said by code, which also alerts the
            # owner, so "our team will answer that" is never a promise nobody was told about.
            problems = ["not_in_facts"]
        else:
            # The writer rewrites numbers like 480-555-0199, which a voice can read as "four
            # hundred eighty". Converted before the guard, so it checks exactly what is spoken.
            draft = speakable_phone_numbers(draft)
            # Gate 2: Jev must be reachable to check it. If not, the draft is thrown away.
            check_state = {"FACTS": facts, "HOMEOWNER_QUESTION": question, "REPLY": draft}
            try:
                check_answers = self._services.ask_jev(check_state, REPLY_CHECKS)
            except JevError as error:
                logger.warning("call %d: guard unavailable: %s", self._call.call_id, error)
                problems = ["guard_unavailable"]
            else:
                # Gate 3: the draft must pass every check.
                problems = find_reply_problems(draft, check_answers)
        outcome = WriterOutcome(question, draft, tuple(problems), time.monotonic() - started)
        self.writer_outcomes.append(outcome)

        with self._state_lock:
            if self.ended:  # a 9 arrived while the writer or guard was working: say nothing
                return []
        if not outcome.spoken or draft is None:
            self._log("system", f"writer draft not spoken ({', '.join(problems)}): {draft!r}")
            return self._question_for_team(stage, problems[0])
        # The answer, then the question the call was waiting on, so the script carries on.
        return self._say(f"{draft} {self._question(stage)}")

    def on_hangup(self) -> None:
        """The line closed. Records how the call ended if nothing has yet; safe to call twice."""
        with self._state_lock:
            already_ended = self.ended
            self.ended = True
        if already_ended:
            return
        self._log("system", "the homeowner hung up")
        if self.booked_label is not None:
            self._store.finish_call(self._call.call_id, "booked", self._clock(), lead_status="booked")
        else:
            self._store.finish_call(
                self._call.call_id,
                "hung_up",
                self._clock(),
                lead_status="handed_off",
                owner_alert="hung_up_before_booking",
            )

    # ---- the stages ----

    def _handle(self, stage: Stage, option: str, offered_slots: Sequence[Slot], words: str) -> list[Action]:
        if option in OFF_SCRIPT_OPTIONS:
            return self._off_script(option, words)
        self._unclear_in_a_row = 0

        if stage == "permission":
            if option == "no_time":
                return self._finish(
                    f"No problem. Someone from {self._settings.business_name} will call you at a better "
                    "time. Goodbye.",
                    result="call_back_later",
                    lead_status="handed_off",
                    owner_alert="asked_to_be_called_later",
                )
            return self._ask("address", prefix="Great.")

        if stage == "address":
            self._save_answer("address_confirmed", option)
            if option == "no":
                # Booking still goes ahead; the owner confirms the right address before the visit.
                self._alert_owner("address_needs_correcting")
                return self._ask("leak", prefix="Thanks, our team will confirm the address with you.")
            return self._ask("leak")

        if stage == "leak":
            self._save_answer("active_leak", option)
            if option == "yes":
                self._alert_owner("URGENT_active_leak")
                return self._ask(
                    "insurance", prefix="I'm sorry to hear that. I'll flag this as urgent for our team."
                )
            return self._ask("insurance")

        if stage == "insurance":
            # Asked only to route the lead. Nothing about coverage is ever said back.
            self._save_answer("insurance_claim", option)
            return self._ask("homeowner")

        if stage == "homeowner":
            self._save_answer("homeowner", option)
            if option == "no":
                return self._finish(
                    "Thanks. The inspection needs the owner's OK, so someone from our team will follow "
                    "up with you. Goodbye.",
                    result="not_homeowner",
                    lead_status="handed_off",
                    owner_alert="caller_is_not_the_homeowner",
                )
            return self._offer_times(prefix="Thanks.")

        if stage == "offer":
            if option == NO_SLOT_OPTION:
                return self._finish(
                    "No problem. Someone from our team will call you to find a time that works. Goodbye.",
                    result="no_time_suited",
                    lead_status="handed_off",
                    owner_alert="needs_a_different_inspection_time",
                )
            if option not in {slot.id for slot in offered_slots}:
                return self._off_script("unclear", words)
            return self._book(option)

        # anything_else
        if option == "yes_has_more":
            return self._say("Sure, go ahead.")
        result_status: LeadStatus = "booked" if self.booked_label is not None else "handed_off"
        return self._finish(
            f"Thanks for your time, and thanks for choosing {self._settings.business_name}. Goodbye.",
            result="booked" if self.booked_label is not None else "ended",
            lead_status=result_status,
        )

    def _off_script(self, option: str, words: str) -> list[Action]:
        if option == "insurance_question":
            return self._say(f"{INSURANCE_LINE} {self._question(self.stage)}")
        if option == "price_question":
            return self._say(f"{PRICE_LINE} {self._question(self.stage)}")
        if option == "other_question":
            if self._services.writer is None:
                return self._question_for_team(self.stage, "no_writer")
            # The writer answers it in continue_turn. "Let me check." goes out first; it isn't
            # remembered as the last thing said, so "repeat" still repeats the real question.
            # The history is copied BEFORE the filler is logged: the writer must see the
            # homeowner's question as the last turn, not the bot's own "Let me check."
            self._pending_question = (words, self.stage, list(self._history))
            self._unclear_in_a_row = 0
            return [*self._say(CHECKING_LINE, remember=False), ContinueTurn()]
        if option == "repeat":
            return self._say(self._last_said)
        self._unclear_in_a_row += 1
        if self._unclear_in_a_row >= MAX_UNCLEAR_IN_A_ROW:
            return self._finish(
                f"I'm having trouble understanding. Someone from {self._settings.business_name} will call "
                "you back. Goodbye.",
                result="not_understood",
                lead_status="handed_off",
                owner_alert="bot_could_not_understand",
            )
        return self._say(f"Sorry, I didn't catch that. {self._question(self.stage)}")

    def _question_for_team(self, stage: Stage, why: str) -> list[Action]:
        """The fixed answer to a question the bot can't answer itself: a person follows up."""
        self._alert_owner(f"homeowner_question_for_the_team:{why}")
        return self._say(f"{QUESTION_FOR_TEAM_LINE} {self._question(stage)}")

    def _offer_times(self, prefix: str) -> list[Action]:
        try:
            booking = self._services.booking()
            open_slots = booking.get_open_slots()
        except (CalendarUnavailable, GoogleAuthExpired) as error:
            return self._calendar_failed(error)
        offered = earliest_per_day(open_slots, TIMES_OFFERED, self._settings.timezone)
        if not offered:
            return self._finish(
                "Our inspection calendar is full right now, so someone from our team will call you to "
                "set up a time. Goodbye.",
                result="no_open_times",
                lead_status="handed_off",
                owner_alert="no_open_inspection_times",
            )
        self.offered_slots = offered
        return self._ask("offer", prefix=prefix)

    def _book(self, slot_id: str) -> list[Action]:
        answers = self._store.lead_answers(self._call.lead_id)
        try:
            result = self._services.booking().book(
                slot_id,
                booking_key=f"lead-{self._call.lead_id}",
                summary=f"Roof inspection: {self._call.full_name}",
                description=self._event_description(answers),
                location=self._call.address,
            )
        except (CalendarUnavailable, GoogleAuthExpired) as error:
            return self._calendar_failed(error)
        if not result.ok or result.start is None:
            # Taken between offering and picking. Offer fresh times once; after that, a person.
            self._slot_taken_count += 1
            if self._slot_taken_count > 1:
                return self._finish(
                    "That time was just taken too. Someone from our team will call you to set a time. "
                    "Goodbye.",
                    result="slots_kept_filling",
                    lead_status="handed_off",
                    owner_alert="needs_an_inspection_time",
                )
            return self._offer_times(prefix="Sorry, that time was just taken.")
        self._store.record_booking(
            self._call.lead_id, self._call.call_id, result.start, result.slot_label, self._clock()
        )
        self.booked_label = result.slot_label
        self.offered_slots = []
        confirmation = (
            f"You're all set for {result.slot_label}. The inspector will come to {self._call.address}."
        )
        return self._ask("anything_else", prefix=confirmation)

    def _event_description(self, answers: Mapping[str, str]) -> str:
        problem = next(
            (label for value, label in PROBLEM_LABELS.items() if value == self._call.problem),
            self._call.problem,
        )
        lines = [
            f"Phone: {self._call.phone}",
            f"Problem (from the form): {problem}",
            f"Active leak: {answers.get('active_leak', 'not asked')}",
            f"Insurance claim: {answers.get('insurance_claim', 'not asked')}",
            f"Owns the home: {answers.get('homeowner', 'not asked')}",
        ]
        if answers.get("address_confirmed") == "no":
            lines.append("ADDRESS NOT CONFIRMED: call the homeowner to confirm it before the visit.")
        lines.append(f"Booked by the AI assistant on call {self._call.call_id}.")
        return "\n".join(lines)

    # ---- endings ----

    def _end_by_opt_out(self, source: str) -> list[Action]:
        with self._state_lock:
            if self.ended:
                return []
            self.ended = True
        return self._opt_out(source)

    def _opt_out(self, source: str) -> list[Action]:
        """Record the opt-out, then end at once with nothing more said, as 64.1200(b)(3) requires."""
        self._store.add_to_do_not_call(self._call.phone, source, self._clock())
        # The lead is already 'opted_out' (add_to_do_not_call did it), so no lead_status here.
        self._store.finish_call(self._call.call_id, "opted_out", self._clock())
        self._log("system", f"opted out: {source}")
        return [EndCall("opted_out")]

    def _after_jev_failure(self) -> list[Action]:
        self._jev_failures_in_a_row += 1
        if self._jev_failures_in_a_row == 1:
            return self._say("Sorry, could you say that again?", remember=False)
        return self._finish(
            f"Sorry, I'm having technical trouble. Someone from {self._settings.business_name} will call "
            "you back. Goodbye.",
            result="jev_unavailable",
            lead_status="handed_off",
            owner_alert="jev_unavailable_during_call",
        )

    def _calendar_failed(self, error: Exception) -> list[Action]:
        logger.error("call %d: calendar failed: %s", self._call.call_id, error)
        reason = "google_sign_in_expired" if isinstance(error, GoogleAuthExpired) else "calendar_unavailable"
        return self._finish(
            "I can't reach our scheduling system right now. Someone from our team will call you back to "
            "set up the inspection. Goodbye.",
            result=reason,
            lead_status="handed_off",
            owner_alert=reason,
        )

    def _finish(
        self,
        goodbye: str,
        *,
        result: str,
        lead_status: LeadStatus | None = None,
        owner_alert: str | None = None,
    ) -> list[Action]:
        with self._state_lock:
            if self.ended:
                return []
            self.ended = True
        # A booking made earlier in the call stands whatever happens after it.
        if self.booked_label is not None and lead_status == "handed_off":
            lead_status = "booked"
        self._store.finish_call(
            self._call.call_id, result, self._clock(), lead_status=lead_status, owner_alert=owner_alert
        )
        self._log("bot", goodbye)
        return [Say(goodbye), EndCall(result)]

    # ---- helpers ----

    def _question(self, stage: Stage) -> str:
        match stage:
            case "permission":
                return PERMISSION_QUESTION
            case "address":
                return f"Is the inspection for {self._call.address}?"
            case "leak":
                return "Is water coming into the house right now?"
            case "insurance":
                return "Are you planning to file an insurance claim for this?"
            case "homeowner":
                return "And are you the owner of the home?"
            case "offer":
                labels = [slot.label for slot in self.offered_slots]
                times = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + f", or {labels[-1]}"
                return f"The earliest inspection times I have are {times}. Which works best for you?"
            case "anything_else":
                return "Is there anything else I can help with?"

    def _ask(self, stage: Stage, prefix: str = "") -> list[Action]:
        self.stage = stage
        return self._say(f"{prefix} {self._question(stage)}".strip())

    def _say(self, text: str, remember: bool = True) -> list[Action]:
        # remember=False keeps "say that again?" from replacing the real question, so a later
        # "repeat" repeats the question.
        if remember:
            self._last_said = text
        self._log("bot", text)
        return [Say(text)]

    def _save_answer(self, question: str, answer: str) -> None:
        self._store.save_answer(self._call.lead_id, question, answer, self._clock())

    def _alert_owner(self, reason: str) -> None:
        self._store.add_owner_alert(self._call.lead_id, reason, self._clock())

    def _log(self, speaker: str, text: str) -> None:
        self._store.add_call_turn(self._call.call_id, speaker, text, self._clock())
        # The writer sees only what was said aloud, not keypresses or system notes.
        if speaker == "bot":
            self._history.append(Turn("assistant", text))
        elif speaker == "homeowner":
            self._history.append(Turn("user", text))
