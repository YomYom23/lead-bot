# lead-bot/src/lead_bot/call_session.py
"""One answered phone call: what the bot says after each thing the caller says.

The business's line forwards calls nobody picked up to the Twilio number, and the bot answers.
No networking here. relay.py receives Twilio's messages and calls on_prompt / continue_turn /
on_hangup; this class answers with what to say and whether to end the call. Keeping it free of
the WebSocket is what lets the tests, the simulator and the scenario runner drive whole calls.

A caller with a roof to inspect goes through these stages, one question each:
    reason -> name -> address -> address_confirm -> leak -> insurance -> homeowner -> offer
    -> text_consent -> anything_else
  - A caller whose number and first name match a lead on file is asked "Is this about the roof
    at <address>?" (address_check) instead of for the address. The address is read out only
    after the name matched, so a stranger holding that phone doesn't hear it.
  - A caller who wants something else (an existing job, a bill, a person) leaves a message: the
    bot asks their name, and the owner gets a text alert with what they said.
  - The address is read back ("I heard ... Is that right?") because speech recognition gets
    addresses wrong. After two misses it books anyway and tells the owner to confirm it.
  - After booking, a new caller is asked whether they may be texted the details. A yes is stored
    as consent with the exact question, and the confirmation text goes out through the worker.

Almost every sentence comes from this file or call_script.py: Jev classifies what the caller
said, and the reply is chosen from the result. The one exception is an off-script question with
no fixed answer ("how long does the inspection take?"). That turn has two parts:
  1. on_prompt says "Let me check." and returns ContinueTurn, so the caller isn't left in
     silence while a model works.
  2. continue_turn has writer.py draft an answer, guard.py check it, then speaks the draft, or
     the fixed "I'll pass that to our team" line if the draft failed any check or anything
     failed. Unchecked text is never spoken.

Every call ends with an owner alert carrying the transcript: a booking, a message, a hang-up, or
whatever handed the caller to a person.

Thread safety: relay.py runs on_prompt in a worker thread (it waits on Jev, which blocks), and a
hang-up can arrive while that's in progress. The short _state_lock guards the fields both touch,
and on_prompt re-checks `ended` before acting on Jev's answer.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .booking import CalendarUnavailable
from .call_questions import NO_SLOT_OPTION, OFF_SCRIPT_OPTIONS, CallStage, build_call_questions
from .call_script import (
    CHECKING_LINE,
    INSURANCE_LINE,
    PRICE_LINE,
    QUESTION_FOR_TEAM_LINE,
    TEXT_CONSENT_QUESTION,
    booking_confirmation_text,
    greeting,
    same_first_name,
    speakable_phone_numbers,
    spoken_address,
    spoken_name,
)
from .conversation import ConversationServices
from .google_calendar import GoogleAuthExpired
from .guard import REPLY_CHECKS, find_reply_problems
from .jev import JevError, choice_answer, yes_probability
from .settings import BusinessSettings
from .slots import Slot, earliest_per_day
from .store import ConnectedCall, Store
from .text_script import conversation_facts, first_name
from .turn_data import LeadStatus, WriterOutcome
from .writer import NOT_IN_FACTS, Turn, WriterError

logger = logging.getLogger(__name__)

# A yes/no hazard counts at this probability or above.
HAZARD_THRESHOLD = 0.5
# A pick-one answer below this confidence is treated as "unclear" and the question is asked again.
MINIMUM_CHOICE_CONFIDENCE = 0.5
# Misunderstandings in a row before a person takes over, so a bad line can't loop forever.
MAX_UNCLEAR_IN_A_ROW = 2
TIMES_OFFERED = 3
# How many times the address is asked before the bot books with its best guess and tells the
# owner to confirm it. A third "what's the address?" loses more callers than a wrong guess costs.
MAX_ADDRESS_TRIES = 2

# Clear ways of saying "don't contact me", caught without waiting for Jev. Not a bare "stop"
# as on an outbound call: someone who rang the business and says "stop, wait" means wait.
OPT_OUT_PHRASES = re.compile(
    r"\b(stop calling|don'?t call|do not call|stop texting|don'?t text|do not text|remove me|"
    r"remove my number|take me off|unsubscribe|opt out|never contact)\b",
    re.IGNORECASE,
)


def is_explicit_opt_out(words: str) -> bool:
    return OPT_OUT_PHRASES.search(words) is not None


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

# What the caller is after, once known: an inspection, or to leave a message for the team.
Route = Literal["undecided", "roof", "message"]


class CallSession:
    def __init__(
        self,
        call: ConnectedCall,
        settings: BusinessSettings,
        store: Store,
        services: ConversationServices,
        clock: Callable[[], datetime],
    ) -> None:
        self._call = call
        self._settings = settings
        self._store = store
        self._services = services
        self._clock = clock
        self._state_lock = threading.Lock()
        self.stage: CallStage = "reason"
        self.ended = False
        self.offered_slots: list[Slot] = []
        self.booked_label: str | None = None
        self._route: Route = "undecided"
        self._caller_name: str | None = None
        self._message: str | None = None  # what to pass on, for a caller who isn't booking
        self._lead_id: int | None = None  # set once the caller is matched to, or made, a lead
        self._address: str | None = None  # the inspection address, once settled
        self._heard_address: str | None = None  # waiting for "Is that right?"
        self._address_tries = 0
        # A caller matched to a lead on file whose consent covers texts gets the confirmation
        # without being asked; a new caller is asked (text_consent).
        self._known_texts_allowed = False
        self._unclear_in_a_row = 0
        self._jev_failures_in_a_row = 0
        self._slot_taken_count = 0
        # What was said on the call, for the writer. Filled in by _log.
        self._history: list[Turn] = []
        # The off-script question continue_turn() still has to answer: the words, the stage it
        # was asked in, and the call so far. Set by on_prompt, taken (and cleared) by continue_turn.
        self._pending_question: tuple[str, CallStage, list[Turn]] | None = None
        self.writer_outcomes: list[WriterOutcome] = []
        self._last_said = greeting(settings)
        # Twilio speaks the greeting itself, before we hear anything, so it's logged here.
        self._log("bot", self._last_said)

    @property
    def can_text(self) -> bool:
        """Whether this caller could be texted at all: a real number Twilio could see (not a
        hidden one), and not on the do-not-call list."""
        return self._call.phone.startswith("+1") and not self._call.on_do_not_call

    # ---- events from relay.py ----

    def on_prompt(self, words: str) -> list[Action]:
        """One complete thing the caller said, as Twilio transcribed it."""
        self._log("caller", words)
        with self._state_lock:
            if self.ended:
                return []
            stage = self.stage
            offered_slots = list(self.offered_slots)
            last_said = self._last_said

        # A clear "don't contact me" needs no model, and must work even when Jev is down.
        if is_explicit_opt_out(words):
            return self._opt_out(words)
        if stage == "message":
            # Whatever they say now is the message; there's nothing to classify.
            return self._take_message(words)

        state = {
            "channel": "phone call",
            "stage": stage,
            "assistant_last_text": last_said,
            "homeowner_latest_text": words,
            "offered_times": [slot.label for slot in offered_slots],
        }
        try:
            answers = self._services.ask_jev(state, build_call_questions(stage, offered_slots))
        except JevError as error:
            logger.warning("call %d: Jev failed: %s", self._call.call_id, error)
            return self._after_jev_failure()

        with self._state_lock:
            if self.ended:  # the caller hung up while Jev was thinking: say nothing more
                return []
            self._jev_failures_in_a_row = 0

        # Hazards before anything else, most serious first.
        if yes_probability(answers, "opt_out") >= HAZARD_THRESHOLD:
            return self._opt_out(words)
        if yes_probability(answers, "abusive") >= HAZARD_THRESHOLD:
            return self._finish(
                "I'm going to end the call now. Goodbye.",
                result="abusive",
                lead_status="handed_off",
                alert="abusive_caller",
            )
        if yes_probability(answers, "wants_human") >= HAZARD_THRESHOLD:
            return self._wants_a_person(words)

        choice = choice_answer(answers, "answer")
        if choice is None or choice.confidence < MINIMUM_CHOICE_CONFIDENCE:
            option = "unclear"
        else:
            option = choice.choice
        return self._handle(stage, option, words)

    def continue_turn(self) -> list[Action]:
        """The second half of an off-script question's turn, after "Let me check." was sent.

        Three gates, the same as for texts. A draft is spoken only if the writer produced one,
        Jev could check it, and it passed every check. Any failure speaks the fixed line and
        alerts the owner, so a person still answers the question after the call.
        """
        with self._state_lock:
            if self.ended or self._pending_question is None:
                return []  # the caller hung up during "Let me check."
            question, stage, history = self._pending_question
            self._pending_question = None
            booked_label = self.booked_label
            address = self._address

        writer = self._services.writer
        if writer is None:  # the offline simulator and scenarios run without one
            return self._question_for_team(stage, "no_writer")
        facts = conversation_facts(self._settings, address or "not given yet", booked_label, on_a_call=True)
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
            # A voice reads 480-555-0199 as "four hundred eighty". Converted before the guard,
            # so it checks exactly what is spoken.
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
            if self.ended:  # the caller hung up while the writer or guard was working
                return []
        if not outcome.sent or draft is None:
            self._log("system", f"writer draft not spoken ({', '.join(problems)}): {draft!r}")
            return self._question_for_team(stage, problems[0])
        # The answer, then the question the call was waiting on, so the script carries on.
        return self._say(f"{draft} {self._question(stage)}")

    def on_hangup(self) -> None:
        """The line closed. Records how the call ended if nothing has yet; safe to call twice.

        A caller who hung up without booking gets no text (they never agreed to one); the owner
        gets the transcript and calls them back.
        """
        with self._state_lock:
            already_ended = self.ended
            self.ended = True
        if already_ended:
            return
        self._log("system", "the caller hung up")
        now = self._clock()
        if self.booked_label is not None:
            self._store.finish_call(self._call.call_id, "booked", now, lead_status="booked")
        elif self._route == "message" and self._caller_name is not None:
            self._store.finish_call(
                self._call.call_id,
                "message",
                now,
                alert="caller_left_a_message",
                message=self._message_for_owner(),
            )
        else:
            self._store.finish_call(
                self._call.call_id,
                "hung_up",
                now,
                lead_status="handed_off",
                alert="caller_hung_up",
                message=self._message,
            )

    # ---- the stages ----

    def _handle(self, stage: CallStage, option: str, words: str) -> list[Action]:
        if option in OFF_SCRIPT_OPTIONS:
            return self._off_script(option, words)
        self._unclear_in_a_row = 0

        if stage == "reason":
            if option == "message":
                self._route = "message"
                self._message = words
                return self._ask("name", prefix="Sure, I'll pass that on.")
            self._route = "roof"
            return self._ask("name", prefix="Sure, I can help with that.")

        if stage == "name":
            return self._after_name(spoken_name(words) or words)

        if stage == "address_check":
            if option == "no":
                # Another property: it gets its own lead once the address is settled, and the
                # consent on the old lead doesn't carry over, so they're asked about texts.
                self._lead_id = None
                self._address = None
                self._known_texts_allowed = False
                return self._ask("address", prefix="No problem.")
            self._save_answer("address_confirmed", "yes")
            return self._ask("leak", prefix="Great.")

        if stage == "address":
            self._heard_address = spoken_address(words) or words
            return self._ask("address_confirm")

        if stage == "address_confirm":
            if option == "yes":
                return self._settle_address(confirmed=True)
            self._address_tries += 1
            if self._address_tries < MAX_ADDRESS_TRIES:
                return self._ask("address", prefix="Sorry about that.")
            return self._settle_address(confirmed=False)

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
                    alert="not_the_homeowner",
                )
            return self._offer_times(prefix="Thanks.")

        if stage == "offer":
            if option == NO_SLOT_OPTION:
                return self._finish(
                    "No problem. Someone from our team will call you to find a time that works. Goodbye.",
                    result="no_time_suited",
                    lead_status="handed_off",
                    alert="needs_a_different_inspection_time",
                )
            if option not in {slot.id for slot in self.offered_slots}:
                return self._off_script("unclear", words)
            return self._book(option)

        if stage == "text_consent":
            lead_id = self._require_lead()
            agreed = option == "yes"
            # Stored either way: a no is a record too, and it stops any text to this lead.
            self._store.record_text_consent(
                lead_id, self._call.call_sid, agreed, TEXT_CONSENT_QUESTION, self._clock()
            )
            if agreed:
                self._queue_confirmation()
                return self._ask("anything_else", prefix="Great, I'll text you the details.")
            return self._ask("anything_else", prefix="No problem.")

        # anything_else
        if option == "yes_has_more":
            return self._say("Sure, go ahead.")
        return self._finish(
            f"Thanks for calling {self._settings.business_name}. Goodbye.",
            result="booked" if self.booked_label is not None else "ended",
            lead_status="booked" if self.booked_label is not None else "handed_off",
        )

    def _after_name(self, name: str) -> list[Action]:
        self._caller_name = name
        thanks = f"Thanks, {first_name(name)}."
        if self._route == "message":
            if self._message is None:
                return self._ask("message", prefix=thanks)
            return self._finish_message()
        known = self._call.known
        if known is not None and same_first_name(name, known.full_name):
            # The same person as the lead on file: only now is their address read out.
            self._store.link_call_to_lead(self._call.call_id, known.lead_id)
            self._lead_id = known.lead_id
            self._known_texts_allowed = known.texts_allowed
            if known.booked_label is not None:
                # Already booked: a change or a question goes to a person, as it does by text.
                self._route = "message"
                return self._ask(
                    "message", prefix=f"{thanks} I see your inspection is booked for {known.booked_label}."
                )
            self._address = known.address
            return self._ask("address_check", prefix=thanks)
        return self._ask("address", prefix=thanks)

    def _settle_address(self, confirmed: bool) -> list[Action]:
        """The address is as good as it gets: make the lead, and go on to the questions."""
        address = self._heard_address or ""
        name = self._caller_name or "Unknown caller"
        self._lead_id = self._store.add_lead_from_call(
            self._call.call_id, name, self._call.phone, address, self._clock()
        )
        self._address = address
        self._save_answer("address_confirmed", "yes" if confirmed else "no")
        if not confirmed:
            # Booking still goes ahead; the owner confirms the right address before the visit.
            self._alert_owner("address_needs_correcting")
            return self._ask("leak", prefix="Thanks, our team will confirm the address with you.")
        return self._ask("leak", prefix="Got it.")

    def _wants_a_person(self, words: str) -> list[Action]:
        """Nobody can pick up right now (that's why the bot answered), so a person calls back."""
        self._route = "message"
        if self._message is None:
            self._message = f"Asked for a person to call back: {words}"
        if self._caller_name is None:
            return self._ask("name", prefix="Of course. I'll have someone call you back.")
        return self._finish_message()

    def _take_message(self, words: str) -> list[Action]:
        self._message = words if self._message is None else f"{self._message} / {words}"
        return self._finish_message()

    def _finish_message(self) -> list[Action]:
        first = first_name(self._caller_name or "")
        return self._finish(
            f"Thanks, {first}. I'll pass that on to the team, and someone will call you back. Goodbye.",
            result="message",
            alert="caller_left_a_message",
            message=self._message_for_owner(),
        )

    def _message_for_owner(self) -> str:
        return f"{self._caller_name or 'Caller'}: {self._message or '(no message)'}"

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
            # caller's question as the last turn, not the bot's own "Let me check."
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
                alert="bot_could_not_understand",
            )
        return self._say(f"Sorry, I didn't catch that. {self._question(self.stage)}")

    def _question_for_team(self, stage: CallStage, why: str) -> list[Action]:
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
                alert="no_open_inspection_times",
            )
        self.offered_slots = offered
        return self._ask("offer", prefix=prefix)

    def _book(self, slot_id: str) -> list[Action]:
        lead_id = self._require_lead()
        answers = self._store.lead_answers(lead_id)
        try:
            result = self._services.booking().book(
                slot_id,
                booking_key=f"lead-{lead_id}",
                summary=f"Roof inspection: {self._caller_name}",
                description=self._event_description(answers),
                location=self._address or "",
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
                    alert="needs_an_inspection_time",
                )
            return self._offer_times(prefix="Sorry, that time was just taken.")
        self._store.record_call_booking(
            self._call.call_id, lead_id, result.start, result.slot_label, self._clock()
        )
        self.booked_label = result.slot_label
        self.offered_slots = []
        confirmation = f"You're all set for {result.slot_label}. The inspector will come to {self._address}."
        if self._known_texts_allowed and self.can_text:
            # Their consent on file already covers texts about the inspection.
            self._queue_confirmation()
            return self._ask("anything_else", prefix=f"{confirmation} I'll text you the details.")
        if self.can_text and self._lead_has_no_consent_yet():
            return self._ask("text_consent", prefix=confirmation)
        return self._ask("anything_else", prefix=confirmation)

    def _lead_has_no_consent_yet(self) -> bool:
        """A new caller, or a known caller whose earlier consent didn't cover texts, isn't asked
        twice; only a lead made on this call has no consent row yet."""
        return self._call.known is None or self._lead_id != self._call.known.lead_id

    def _queue_confirmation(self) -> None:
        if self.booked_label is None or self._address is None:
            return
        body = booking_confirmation_text(self._settings, self.booked_label, self._address)
        self._store.queue_call_confirmation(self._require_lead(), body, self._clock())

    def _event_description(self, answers: Mapping[str, str]) -> str:
        lines = [
            f"Phone: {self._call.phone}",
            "Came in as a phone call the office missed.",
            f"Active leak: {answers.get('active_leak', 'not asked')}",
            f"Insurance claim: {answers.get('insurance_claim', 'not asked')}",
            f"Owns the home: {answers.get('homeowner', 'not asked')}",
        ]
        if answers.get("address_confirmed") == "no":
            lines.append("ADDRESS NOT CONFIRMED: call the homeowner to confirm it before the visit.")
        lines.append(f"Booked by the AI phone assistant on call {self._call.call_id}.")
        return "\n".join(lines)

    # ---- endings ----

    def _opt_out(self, words: str) -> list[Action]:
        """They asked never to be contacted: the number goes on the do-not-call list (so no text
        or campaign reaches it either), and the call ends politely."""
        self._store.add_to_do_not_call(
            self._call.phone, f"asked on call {self._call.call_id}: {words[:80]!r}", self._clock()
        )
        return self._finish("Understood. We won't contact you again. Goodbye.", result="opted_out")

    def _after_jev_failure(self) -> list[Action]:
        self._jev_failures_in_a_row += 1
        if self._jev_failures_in_a_row == 1:
            return self._say("Sorry, could you say that again?", remember=False)
        return self._finish(
            f"Sorry, I'm having technical trouble. Someone from {self._settings.business_name} will call "
            "you back. Goodbye.",
            result="jev_unavailable",
            lead_status="handed_off",
            alert="jev_unavailable",
        )

    def _calendar_failed(self, error: Exception) -> list[Action]:
        logger.error("call %d: calendar failed: %s", self._call.call_id, error)
        reason = "google_sign_in_expired" if isinstance(error, GoogleAuthExpired) else "calendar_unavailable"
        return self._finish(
            "I can't reach our scheduling system right now. Someone from our team will call you back to "
            "set up the inspection. Goodbye.",
            result=reason,
            lead_status="handed_off",
            alert=reason,
        )

    def _finish(
        self,
        goodbye: str,
        *,
        result: str,
        lead_status: LeadStatus | None = None,
        alert: str | None = None,
        message: str | None = None,
    ) -> list[Action]:
        with self._state_lock:
            if self.ended:
                return []
            self.ended = True
        # A booking made earlier in the call stands whatever happens after it.
        if self.booked_label is not None and lead_status == "handed_off":
            lead_status = "booked"
        self._store.finish_call(
            self._call.call_id, result, self._clock(), lead_status=lead_status, alert=alert, message=message
        )
        self._log("bot", goodbye)
        return [Say(goodbye), EndCall(result)]

    # ---- helpers ----

    def _question(self, stage: CallStage) -> str:
        match stage:
            case "reason":
                return "What can I do for you?"
            case "name":
                return "Can I get your name?"
            case "message":
                return "What would you like me to pass on to the team?"
            case "address_check":
                return f"Is this about the roof at {self._address}?"
            case "address":
                return "What's the address of the property?"
            case "address_confirm":
                return f"I heard {self._heard_address}. Is that right?"
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
            case "text_consent":
                return TEXT_CONSENT_QUESTION
            case "anything_else":
                return "Is there anything else I can help with?"

    def _ask(self, stage: CallStage, prefix: str = "") -> list[Action]:
        self.stage = stage
        return self._say(f"{prefix} {self._question(stage)}".strip())

    def _say(self, text: str, remember: bool = True) -> list[Action]:
        # remember=False keeps "say that again?" from replacing the real question, so a later
        # "repeat" repeats the question.
        if remember:
            self._last_said = text
        self._log("bot", text)
        return [Say(text)]

    def _require_lead(self) -> int:
        # The stages that need a lead come after the address, which makes or links one.
        if self._lead_id is None:
            raise RuntimeError(f"call {self._call.call_id} reached stage {self.stage} with no lead")
        return self._lead_id

    def _save_answer(self, question: str, answer: str) -> None:
        if self._lead_id is not None:
            self._store.save_answer(self._lead_id, question, answer, self._clock())

    def _alert_owner(self, reason: str) -> None:
        self._store.add_call_alert(self._call.call_id, reason, self._clock())

    def _log(self, speaker: str, text: str) -> None:
        self._store.add_call_turn(self._call.call_id, speaker, text, self._clock())
        # The writer sees only what was said aloud, not system notes.
        if speaker == "bot":
            self._history.append(Turn("assistant", text))
        elif speaker == "caller":
            self._history.append(Turn("user", text))
