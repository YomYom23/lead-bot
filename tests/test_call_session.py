# lead-bot/tests/test_call_session.py
# One call, turn by turn, with Jev and the calendar faked: opt-outs, the full booking path,
# the fixed answers, and every way the call hands off to a person.
from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import timedelta

import pytest
from call_fakes import FIRST_OFFERED_TIME, GUARD_CHECK, ScriptedJev, StubWriter, make_session
from conftest import DANA_PHONE
from test_booking import FakeCalendar

from lead_bot.call_script import (
    CHECKING_LINE,
    INSURANCE_LINE,
    PRICE_LINE,
    QUESTION_FOR_TEAM_LINE,
    opening_greeting,
)
from lead_bot.call_session import Action, CallSession, ContinueTurn, EndCall, Say, is_explicit_opt_out
from lead_bot.google_calendar import GoogleAuthExpired
from lead_bot.jev import JevError
from lead_bot.slots import BusyPeriod, start_from_slot_id
from lead_bot.store import Store
from lead_bot.writer import NOT_IN_FACTS, Turn, WriterError


def said(actions: list[Action]) -> str:
    return " ".join(action.text for action in actions if isinstance(action, Say))


def ended_with(actions: list[Action]) -> str | None:
    return next((action.reason for action in actions if isinstance(action, EndCall)), None)


def reasons(store: Store) -> list[str]:
    return [alert.reason for alert in store.owner_alerts()]


# ---- opting out ----


def test_pressing_9_records_the_opt_out_and_ends_the_call_with_nothing_said(store: Store) -> None:
    session, call, _ = make_session(store, ScriptedJev())  # Jev must not be asked anything

    actions = session.on_keypress("9")

    assert actions == [EndCall("opted_out")]  # no goodbye: the rule says end immediately
    assert store.is_on_do_not_call(DANA_PHONE)
    assert store.lead_status(call.lead_id) == "opted_out"
    assert store.call_result(call.call_id) == "opted_out"
    assert session.on_prompt("wait, actually") == []  # nothing more happens on this call


def test_other_keys_do_nothing(store: Store) -> None:
    session, call, _ = make_session(store, ScriptedJev())
    assert session.on_keypress("1") == []
    assert not store.is_on_do_not_call(DANA_PHONE)
    assert not session.ended


@pytest.mark.parametrize(
    "words",
    ["Stop.", "stop", "Please stop calling me", "Take me off your list", "Don't call this number again"],
)
def test_saying_stop_ends_the_call_without_waiting_for_jev(store: Store, words: str) -> None:
    session, call, _ = make_session(store, ScriptedJev())  # an empty script fails if Jev is asked

    assert session.on_prompt(words) == [EndCall("opted_out")]
    assert store.is_on_do_not_call(DANA_PHONE)


def test_stop_inside_a_roofing_answer_is_not_an_opt_out() -> None:
    assert not is_explicit_opt_out("the leak won't stop when it rains")
    assert not is_explicit_opt_out("yes, the water won't stop")
    assert is_explicit_opt_out("stop, I'm not interested")


def test_opt_outs_in_other_words_are_caught_by_jev(store: Store) -> None:
    session, _, _ = make_session(store, ScriptedJev(("unclear", {"opt_out": 0.92})))

    assert session.on_prompt("I'm not interested, quit bugging me") == [EndCall("opted_out")]
    assert store.is_on_do_not_call(DANA_PHONE)


def test_a_9_pressed_while_jev_is_thinking_ends_the_call_and_jevs_answer_is_dropped(store: Store) -> None:
    """The keypad must not wait behind Jev. The homeowner says something, then presses 9 before
    Jev answers. The call ends on the 9, and when Jev's answer arrives nothing more is said."""
    jev = ScriptedJev("yes")
    jev.hold = threading.Event()
    session, call, _ = make_session(store, jev)
    prompt_result: list[list[Action]] = []
    talker = threading.Thread(target=lambda: prompt_result.append(session.on_prompt("yes, go ahead")))
    talker.start()
    assert jev.inside_jev.wait(timeout=5)

    assert session.on_keypress("9") == [EndCall("opted_out")]
    jev.hold.set()
    talker.join(timeout=5)

    assert prompt_result == [[]]
    assert store.call_result(call.call_id) == "opted_out"
    assert store.lead_answers(call.lead_id) == {}  # Jev's late "yes" wasn't acted on


# ---- the full call ----


def test_a_full_call_books_the_inspection(store: Store) -> None:
    jev = ScriptedJev("yes", "yes", "no", "yes", "yes", FIRST_OFFERED_TIME, "no")
    session, call, calendar = make_session(store, jev)

    turns = [
        session.on_prompt("Sure, I have a minute"),
        session.on_prompt("Yes that's the address"),
        session.on_prompt("No, nothing coming in"),
        session.on_prompt("Yeah, I'm filing a claim"),
        session.on_prompt("Yes I own it"),
    ]
    offer = said(turns[-1])
    assert "The earliest inspection times I have are" in offer
    first_time = session.offered_slots[0].label
    assert first_time in offer and len(session.offered_slots) == 3

    booked = session.on_prompt(f"{first_time} works")
    assert said(booked).startswith(f"You're all set for {first_time}. The inspector will come to")
    goodbye = session.on_prompt("No, that's everything")

    assert ended_with(goodbye) == "booked"
    [event] = calendar.events
    assert event.location == call.address
    assert store.booking_label(call.lead_id) == first_time
    assert store.lead_status(call.lead_id) == "booked"
    assert store.call_result(call.call_id) == "booked"
    assert store.lead_answers(call.lead_id) == {
        "address_confirmed": "yes",
        "active_leak": "no",
        "insurance_claim": "yes",
        "homeowner": "yes",
    }
    transcript = store.call_transcript(call.call_id)
    assert transcript[0] == ("bot", opening_greeting(session._settings))
    assert ("homeowner", "Yes I own it") in transcript
    assert reasons(store) == []  # a clean booking needs nothing from the owner


def test_jev_is_told_where_the_call_is_and_what_was_just_asked(store: Store) -> None:
    jev = ScriptedJev("yes")
    session, call, _ = make_session(store, jev)
    session.on_prompt("sure")
    [state] = jev.states
    assert state["stage"] == "permission"
    assert state["homeowner_latest_words"] == "sure"
    assert state["assistant_last_said"].startswith("Hello, this call is from Desert Ridge Roofing LLC.")


def test_an_active_leak_alerts_the_owner_as_urgent(store: Store) -> None:
    session, call, _ = make_session(store, ScriptedJev("yes", "yes", "yes"))
    session.on_prompt("yes")
    session.on_prompt("yes")
    reply = session.on_prompt("Yes, it's dripping into the bedroom")

    assert "urgent" in said(reply)
    assert reasons(store) == ["URGENT_active_leak"]
    assert session.stage == "insurance"


# ---- fixed answers: insurance and price ----


def test_an_insurance_question_gets_the_fixed_line_and_the_question_again(store: Store) -> None:
    session, _, _ = make_session(store, ScriptedJev("yes", "yes", "no", "insurance_question"))
    for words in ("yes", "yes", "no"):
        session.on_prompt(words)

    reply = said(session.on_prompt("Will my insurance cover a new roof?"))

    assert reply == f"{INSURANCE_LINE} Are you planning to file an insurance claim for this?"
    assert session.stage == "insurance"  # still waiting for the actual answer


def test_a_price_question_gets_the_fixed_line(store: Store) -> None:
    session, _, _ = make_session(store, ScriptedJev("price_question"))
    assert said(session.on_prompt("How much is a new roof?")).startswith(PRICE_LINE)


def test_repeat_says_the_last_thing_again(store: Store) -> None:
    session, _, _ = make_session(store, ScriptedJev("yes", "repeat"))
    question = said(session.on_prompt("yes"))
    assert said(session.on_prompt("sorry, what?")) == question


# ---- handing off to a person ----


def test_two_misunderstandings_in_a_row_hand_off(store: Store) -> None:
    session, call, _ = make_session(store, ScriptedJev("unclear", "unclear"))
    assert "didn't catch that" in said(session.on_prompt("mmhm the thing"))
    final = session.on_prompt("the other thing")

    assert ended_with(final) == "not_understood"
    assert store.lead_status(call.lead_id) == "handed_off"
    assert reasons(store) == ["bot_could_not_understand"]


def test_jev_failing_once_asks_again_and_twice_hands_off(store: Store) -> None:
    session, call, _ = make_session(store, ScriptedJev(JevError("timeout"), JevError("timeout")))
    assert said(session.on_prompt("yes")) == "Sorry, could you say that again?"
    final = session.on_prompt("yes")

    assert ended_with(final) == "jev_unavailable"
    assert "will call you back" in said(final)
    assert reasons(store) == ["jev_unavailable_during_call"]


def test_asking_for_a_person_hands_off(store: Store) -> None:
    session, call, _ = make_session(store, ScriptedJev(("unclear", {"wants_human": 0.8})))
    final = session.on_prompt("Can I just talk to a real person?")
    assert ended_with(final) == "wants_human"
    assert store.lead_status(call.lead_id) == "handed_off"


def test_a_wrong_number_is_never_called_again(store: Store) -> None:
    session, call, _ = make_session(store, ScriptedJev(("unclear", {"wrong_person": 0.9})))
    final = session.on_prompt("I never asked for any roof inspection")
    assert ended_with(final) == "wrong_person"
    assert store.is_on_do_not_call(DANA_PHONE)


def test_someone_who_is_not_the_owner_is_handed_off(store: Store) -> None:
    session, call, calendar = make_session(store, ScriptedJev("yes", "yes", "no", "no", "no"))
    for words in ("yes", "yes", "no", "no"):
        session.on_prompt(words)
    final = session.on_prompt("No, I rent it")
    assert ended_with(final) == "not_homeowner"
    assert calendar.events == []


def test_a_calendar_outage_hands_off_instead_of_inventing_times(store: Store) -> None:
    jev = ScriptedJev("yes", "yes", "no", "no", "yes")
    session, call, _ = make_session(store, jev)

    def expired_sign_in() -> None:
        raise GoogleAuthExpired("no Google sign-in")

    session._services = type(session._services)(ask_jev=jev, booking=expired_sign_in)  # type: ignore[arg-type]
    for words in ("yes", "yes", "no", "no"):
        session.on_prompt(words)
    final = session.on_prompt("yes")

    assert ended_with(final) == "google_sign_in_expired"
    assert "can't reach our scheduling system" in said(final)
    assert reasons(store) == ["google_sign_in_expired"]


def test_a_time_taken_mid_call_is_replaced_with_fresh_times(store: Store) -> None:
    jev = ScriptedJev("yes", "yes", "no", "no", "yes", FIRST_OFFERED_TIME)
    calendar = FakeCalendar()
    session, call, _ = make_session(store, jev, calendar)
    for words in ("yes", "yes", "no", "no", "yes"):
        session.on_prompt(words)
    taken = session.offered_slots[0]
    taken_start = start_from_slot_id(taken.id)
    assert taken_start is not None
    # Someone else got it first.
    calendar.other_busy.append(BusyPeriod(taken_start, taken_start + timedelta(hours=1)))

    reply = said(session.on_prompt("the first one"))

    assert reply.startswith("Sorry, that time was just taken. The earliest inspection times I have are")
    assert taken.id not in [slot.id for slot in session.offered_slots]
    assert calendar.events == []


# ---- hanging up ----


def test_hanging_up_before_booking_tells_the_owner(store: Store) -> None:
    session, call, _ = make_session(store, ScriptedJev("yes"))
    session.on_prompt("yes")
    session.on_hangup()
    session.on_hangup()  # Twilio and the socket can both report it: the second does nothing

    assert store.call_result(call.call_id) == "hung_up"
    assert store.lead_status(call.lead_id) == "handed_off"
    assert reasons(store) == ["hung_up_before_booking"]


def test_hanging_up_after_booking_keeps_the_booking(store: Store) -> None:
    session, call, _ = make_session(
        store, ScriptedJev("yes", "yes", "no", "no", "yes", FIRST_OFFERED_TIME)
    )
    for words in ("yes", "yes", "no", "no", "yes", "the first one"):
        session.on_prompt(words)
    session.on_hangup()

    assert store.lead_status(call.lead_id) == "booked"
    assert store.call_result(call.call_id) == "booked"
    assert reasons(store) == []


# ---- off-script questions: the writer, then the guard ----

HOW_LONG = "How long does the inspection take?"
DRAFT = "It takes about an hour."
PERMISSION_AGAIN = "Do you have a couple of minutes to set up the inspection?"


def test_an_off_script_question_says_let_me_check_then_speaks_the_checked_draft(store: Store) -> None:
    jev = ScriptedJev("other_question", GUARD_CHECK)
    writer = StubWriter(DRAFT)
    session, call, _ = make_session(store, jev, writer=writer)

    first_part = session.on_prompt(HOW_LONG)
    assert first_part == [Say(CHECKING_LINE), ContinueTurn()]
    answer = session.continue_turn()

    # The draft, then the question the call was waiting on, so the script carries on.
    assert said(answer) == f"{DRAFT} {PERMISSION_AGAIN}"
    facts, history = writer.calls[0]
    assert "12 Saguaro Ln, Mesa AZ" in facts
    assert history[-1] == Turn("user", HOW_LONG)
    guard_state = jev.states[-1]
    assert guard_state == {"FACTS": facts, "HOMEOWNER_QUESTION": HOW_LONG, "REPLY": DRAFT}
    assert reasons(store) == []  # answered on the call: nothing for the owner
    [outcome] = session.writer_outcomes
    assert outcome.spoken and outcome.problems == ()


def test_a_draft_the_guard_blocks_is_never_spoken(store: Store) -> None:
    jev = ScriptedJev("other_question", (GUARD_CHECK, {"price_talk": 0.8}))
    session, call, _ = make_session(store, jev, writer=StubWriter("Most repairs are around four hundred."))
    session.on_prompt("Is it expensive to fix a few shingles?")

    answer = said(session.continue_turn())

    assert "four hundred" not in answer
    assert answer == f"{QUESTION_FOR_TEAM_LINE} {PERMISSION_AGAIN}"
    assert reasons(store) == ["homeowner_question_for_the_team:price_talk"]
    assert ("bot", answer) in store.call_transcript(call.call_id)


def test_insurance_talk_is_blocked_by_the_backstop_when_jev_scores_it_low(store: Store) -> None:
    jev = ScriptedJev("other_question", GUARD_CHECK)  # Jev passes it; the word check must not
    session, _, _ = make_session(store, jev, writer=StubWriter("Hail damage is usually covered."))
    session.on_prompt("Do you see a lot of hail damage around here?")

    assert said(session.continue_turn()).startswith(QUESTION_FOR_TEAM_LINE)
    assert reasons(store) == ["homeowner_question_for_the_team:insurance_words"]


def test_a_writer_failure_falls_back_without_asking_the_guard(store: Store) -> None:
    jev = ScriptedJev("other_question")  # no GUARD_CHECK scripted: asking it fails the test
    session, _, _ = make_session(store, jev, writer=StubWriter(WriterError("insufficient_quota")))
    session.on_prompt(HOW_LONG)

    assert said(session.continue_turn()).startswith(QUESTION_FOR_TEAM_LINE)
    assert reasons(store) == ["homeowner_question_for_the_team:writer_failed"]


def test_a_draft_is_thrown_away_when_the_guard_cannot_be_reached(store: Store) -> None:
    jev = ScriptedJev("other_question", JevError("timeout"))
    session, _, _ = make_session(store, jev, writer=StubWriter(DRAFT))
    session.on_prompt(HOW_LONG)

    answer = said(session.continue_turn())

    assert DRAFT not in answer
    assert reasons(store) == ["homeowner_question_for_the_team:guard_unavailable"]


def test_without_a_writer_the_fixed_line_is_said_straight_away(store: Store) -> None:
    session, _, _ = make_session(store, ScriptedJev("other_question"))
    answer = session.on_prompt(HOW_LONG)
    assert said(answer) == f"{QUESTION_FOR_TEAM_LINE} {PERMISSION_AGAIN}"
    assert not any(isinstance(action, ContinueTurn) for action in answer)


def test_a_9_pressed_while_the_writer_works_ends_the_call_and_the_draft_is_dropped(store: Store) -> None:
    session_holder: list[CallSession] = []

    class WriterInterruptedByA9:
        def write(self, *, facts: str, history: Sequence[Turn], reason: str) -> str:
            # The homeowner presses 9 while the draft is being written.
            assert session_holder[0].on_keypress("9") == [EndCall("opted_out")]
            return DRAFT

    jev = ScriptedJev("other_question", GUARD_CHECK)
    session, call, _ = make_session(store, jev, writer=WriterInterruptedByA9())
    session_holder.append(session)
    session.on_prompt(HOW_LONG)

    assert session.continue_turn() == []
    assert store.call_result(call.call_id) == "opted_out"
    assert DRAFT not in [text for _, text in store.call_transcript(call.call_id)]


def test_repeat_after_let_me_check_repeats_the_real_question(store: Store) -> None:
    jev = ScriptedJev("other_question", GUARD_CHECK, "repeat")
    session, _, _ = make_session(store, jev, writer=StubWriter(DRAFT))
    session.on_prompt(HOW_LONG)
    answer = said(session.continue_turn())
    assert said(session.on_prompt("sorry, what was that?")) == answer


def test_a_question_the_facts_dont_answer_gets_the_fixed_line_and_an_owner_alert(store: Store) -> None:
    """The writer replies NOT_IN_FACTS; code says the deferral, so a person is always told."""
    jev = ScriptedJev("other_question")  # no GUARD_CHECK: a NOT_IN_FACTS reply isn't checked
    session, _, _ = make_session(store, jev, writer=StubWriter(NOT_IN_FACTS))
    session.on_prompt("Do I need to be home for it?")

    assert said(session.continue_turn()) == f"{QUESTION_FOR_TEAM_LINE} {PERMISSION_AGAIN}"
    assert reasons(store) == ["homeowner_question_for_the_team:not_in_facts"]


def test_a_written_phone_number_is_spoken_digit_by_digit(store: Store) -> None:
    jev = ScriptedJev("other_question", GUARD_CHECK)
    session, _, _ = make_session(store, jev, writer=StubWriter("You can call us at 480-555-0199."))
    session.on_prompt("What's your number?")

    answer = said(session.continue_turn())

    assert answer.startswith("You can call us at 4 8 0, 5 5 5, 0 1 9 9.")
    # The guard checks exactly what is spoken, not the writer's original.
    assert jev.states[-1]["REPLY"] == "You can call us at 4 8 0, 5 5 5, 0 1 9 9."
