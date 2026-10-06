# lead-bot/tests/test_conversation.py
# The conversation, driven end to end through the worker and a real database: what each kind of
# homeowner text gets back, what's saved, and who is told. Jev and the writer are scripted.
from __future__ import annotations

from datetime import timedelta

from conftest import DANA_PHONE, make_consent, make_lead, phoenix
from test_booking import FakeCalendar
from text_fakes import (
    FIRST_OFFERED_TIME,
    GUARD_CHECK,
    SETTINGS,
    Clock,
    RecordingSender,
    ScriptedJev,
    StubWriter,
    make_worker,
    reply,
    start_conversation,
)

from lead_bot.jev import JevError
from lead_bot.slots import BusyPeriod
from lead_bot.store import Store
from lead_bot.text_script import (
    INSURANCE_LINE,
    LATER_TIMES_PREFIX,
    NO_TIME_SUITED_TEXT,
    PRICE_LINE,
    QUESTION_FOR_TEAM_LINE,
    first_text,
    inspection_hours_text,
    off_topic_text,
    opt_out_confirmation,
)
from lead_bot.worker import MAX_TRIES_PER_TEXT, RETRY_DELAY, Worker
from lead_bot.writer import NOT_IN_FACTS, WriterError

QUALIFYING_ANSWERS = ("yes", "no", "no", "yes")  # address, leak, insurance, homeowner


def run(
    store: Store,
    jev: ScriptedJev,
    texts: list[str],
    writer: StubWriter | None = None,
    calendar: FakeCalendar | None = None,
) -> tuple[Worker, RecordingSender, Clock, int]:
    clock = Clock()
    worker, sender = make_worker(store, jev, clock, writer=writer, calendar=calendar)
    lead_id = start_conversation(store, worker, clock)
    for text in texts:
        reply(store, worker, clock, DANA_PHONE, text)
    return worker, sender, clock, lead_id


def alerts(store: Store) -> list[str]:
    return [alert.reason for alert in store.owner_alerts()]


# ---- the normal path ----


def test_the_first_text_names_the_business_and_says_how_to_stop(store: Store) -> None:
    _, sender, _, _ = run(store, ScriptedJev(), [])
    assert sender.sent == [(DANA_PHONE, first_text(SETTINGS, "Dana Ruiz", "12 Saguaro Ln, Mesa AZ"))]
    body = sender.bodies[0]
    assert body.startswith("Hi Dana, this is the automated assistant for Desert Ridge Roofing LLC")
    assert "Reply HELP for help, STOP to opt out." in body


def test_four_answers_and_a_pick_book_the_inspection(store: Store) -> None:
    calendar = FakeCalendar()
    jev = ScriptedJev(*QUALIFYING_ANSWERS, FIRST_OFFERED_TIME)
    texts = ["yes", "no", "no", "yes", "the first one"]
    _, sender, _, lead_id = run(store, jev, texts, calendar=calendar)

    assert store.conversation_result(lead_id) == "booked"
    assert store.lead_status(lead_id) == "booked"
    assert store.lead_answers(lead_id) == {
        "address_confirmed": "yes",
        "active_leak": "no",
        "insurance_claim": "no",
        "homeowner": "yes",
    }
    assert len(calendar.events) == 1
    label = store.booking_label(lead_id)
    assert label is not None
    assert sender.bodies[-1].startswith(f"You're all set for {label}.")
    assert alerts(store) == []


def test_a_bare_number_picks_that_time_without_asking_jev(store: Store) -> None:
    jev = ScriptedJev(*QUALIFYING_ANSWERS)  # nothing scripted for the pick: Jev mustn't be asked
    _, sender, _, lead_id = run(store, jev, ["yes", "no", "no", "yes", "2"])
    offer = sender.bodies[-2]
    second_time = offer.split("\n2) ")[1].split("\n")[0]
    assert store.booking_label(lead_id) == second_time


def test_an_active_leak_alerts_the_owner_as_urgent(store: Store) -> None:
    jev = ScriptedJev("yes", "yes")
    _, sender, _, _ = run(store, jev, ["yes", "yes, it's dripping now"])
    assert "URGENT_active_leak" in alerts(store)
    assert sender.bodies[-1].startswith("I'm sorry to hear that. I'll flag this as urgent")


def test_a_renter_is_handed_to_a_person(store: Store) -> None:
    jev = ScriptedJev("yes", "no", "no", "no")
    _, _, _, lead_id = run(store, jev, ["yes", "no", "no", "I rent"])
    assert store.conversation_result(lead_id) == "not_homeowner"
    assert store.lead_status(lead_id) == "handed_off"
    assert "not_the_homeowner" in alerts(store)


# ---- opting out ----


def test_a_bare_stop_is_left_to_twilio(store: Store) -> None:
    # Twilio blocks the number and sends its own confirmation, so the bot sends nothing at all.
    _, sender, _, lead_id = run(store, ScriptedJev(), ["STOP"])
    assert store.is_on_do_not_call(DANA_PHONE)
    assert store.conversation_result(lead_id) == "opted_out"
    assert len(sender.sent) == 1  # only the first text, from before


def test_an_opt_out_in_their_own_words_gets_one_confirmation_and_no_model(store: Store) -> None:
    _, sender, _, lead_id = run(store, ScriptedJev(), ["Please stop texting me"])
    assert store.is_on_do_not_call(DANA_PHONE)
    assert sender.bodies[1:] == [opt_out_confirmation(SETTINGS)]
    assert store.lead_status(lead_id) == "opted_out"


def test_stop_inside_a_roofing_answer_is_not_an_opt_out(store: Store) -> None:
    jev = ScriptedJev("yes", "yes")
    run(store, jev, ["yes", "yes and it won't stop dripping"])
    assert not store.is_on_do_not_call(DANA_PHONE)


def test_an_opt_out_only_jev_catches_still_stops_the_texts(store: Store) -> None:
    jev = ScriptedJev(("unclear", {"opt_out": 0.9}))
    _, sender, _, _ = run(store, jev, ["not interested, quit bugging me"])
    assert store.is_on_do_not_call(DANA_PHONE)
    assert sender.bodies[1:] == [opt_out_confirmation(SETTINGS)]


def test_nothing_is_ever_sent_after_an_opt_out(store: Store) -> None:
    jev = ScriptedJev()
    worker, sender, clock, _ = run(store, jev, ["STOP"])
    reply(store, worker, clock, DANA_PHONE, "hello? can I still book?")
    for _ in range(10):  # ten days pass: no reminder may go out either
        clock.now += timedelta(days=1)
        worker.run_once()
    assert len(sender.sent) == 1
    assert "text_from_an_opted_out_number" in alerts(store)


def test_an_opt_out_after_booking_keeps_the_booking(store: Store) -> None:
    jev = ScriptedJev(*QUALIFYING_ANSWERS, FIRST_OFFERED_TIME)
    _, _, _, lead_id = run(store, jev, ["yes", "no", "no", "yes", "first", "now stop texting me"])
    assert store.conversation_result(lead_id) == "booked"
    assert store.lead_status(lead_id) == "booked"
    assert store.is_on_do_not_call(DANA_PHONE)
    assert "opted_out_after_booking" in alerts(store)


def test_a_wrong_number_is_never_texted_again(store: Store) -> None:
    jev = ScriptedJev(("unclear", {"wrong_person": 0.9}))
    _, _, _, lead_id = run(store, jev, ["you have the wrong number"])
    assert store.conversation_result(lead_id) == "wrong_person"
    assert store.is_on_do_not_call(DANA_PHONE)


# ---- questions ----


def test_insurance_and_price_questions_get_the_fixed_text_then_the_question_again(store: Store) -> None:
    jev = ScriptedJev("insurance_question", "price_question")
    _, sender, _, _ = run(store, jev, ["will insurance cover it?", "how much?"])
    address_question = "Is the inspection for 12 Saguaro Ln, Mesa AZ?"
    assert sender.bodies[1] == f"{INSURANCE_LINE} {address_question}"
    assert sender.bodies[2] == f"{PRICE_LINE} {address_question}"


def test_a_writer_answer_that_passes_the_guard_is_sent(store: Store) -> None:
    jev = ScriptedJev("other_question", GUARD_CHECK)
    writer = StubWriter("Inspections take about 45 minutes.")
    _, sender, _, _ = run(store, jev, ["how long does it take?"], writer=writer)
    assert sender.bodies[1].startswith("Inspections take about 45 minutes. Is the inspection for")
    assert alerts(store) == []
    facts, history = writer.calls[0]
    assert "Desert Ridge Roofing LLC" in facts
    assert history[-1].text == "how long does it take?"


def test_a_blocked_draft_is_never_sent_and_the_owner_is_told(store: Store) -> None:
    jev = ScriptedJev("other_question", (GUARD_CHECK, {"unsupported_fact": 0.9}))
    writer = StubWriter("Yes, the inspector always calls ahead.")
    _, sender, _, _ = run(store, jev, ["will they call first?"], writer=writer)
    assert "always calls ahead" not in " ".join(sender.bodies)
    assert sender.bodies[1].startswith(QUESTION_FOR_TEAM_LINE)
    assert alerts(store) == ["homeowner_question_for_the_team:unsupported_fact"]


def test_not_in_facts_gets_the_fixed_text_without_asking_the_guard(store: Store) -> None:
    jev = ScriptedJev("other_question")  # no guard call scripted
    writer = StubWriter(NOT_IN_FACTS)
    _, sender, _, _ = run(store, jev, ["do I need to be home?"], writer=writer)
    assert sender.bodies[1].startswith(QUESTION_FOR_TEAM_LINE)
    assert alerts(store) == ["homeowner_question_for_the_team:not_in_facts"]


def test_a_writer_failure_gets_the_fixed_text(store: Store) -> None:
    jev = ScriptedJev("other_question")
    writer = StubWriter(WriterError("timeout"))
    _, sender, _, _ = run(store, jev, ["any warranty?"], writer=writer)
    assert sender.bodies[1].startswith(QUESTION_FOR_TEAM_LINE)
    assert alerts(store) == ["homeowner_question_for_the_team:writer_failed"]


def test_curly_quotes_in_a_draft_are_made_plain_before_it_is_checked_and_sent(store: Store) -> None:
    jev = ScriptedJev("other_question", GUARD_CHECK)
    writer = StubWriter("It\u2019s about 45 minutes \u2014 quick.")
    _, sender, _, _ = run(store, jev, ["how long?"], writer=writer)
    assert sender.bodies[1].startswith("It's about 45 minutes - quick.")
    guard_state = jev.states[-1]
    assert guard_state["REPLY"] == "It's about 45 minutes - quick."


# ---- when things go wrong ----


def test_two_misunderstandings_in_a_row_hand_off(store: Store) -> None:
    jev = ScriptedJev("unclear", "unclear")
    _, _, _, lead_id = run(store, jev, ["banana", "helicopter"])
    assert store.conversation_result(lead_id) == "not_understood"
    assert "bot_could_not_understand" in alerts(store)


def test_jev_down_is_retried_then_handed_off(store: Store) -> None:
    jev = ScriptedJev(*[JevError("down")] * MAX_TRIES_PER_TEXT)
    clock = Clock()
    worker, sender = make_worker(store, jev, clock)
    lead_id = start_conversation(store, worker, clock)
    reply(store, worker, clock, DANA_PHONE, "yes")
    assert len(sender.sent) == 1  # nothing yet: the text waits for a retry
    for _ in range(MAX_TRIES_PER_TEXT - 1):
        clock.now += RETRY_DELAY
        worker.run_once()
    assert store.conversation_result(lead_id) == "jev_unavailable"
    assert sender.bodies[-1].startswith("Sorry, I'm having technical trouble.")
    assert "jev_unavailable" in alerts(store)


def test_a_text_from_an_unknown_number_gets_no_reply_but_the_owner_hears(store: Store) -> None:
    clock = Clock()
    worker, sender = make_worker(store, ScriptedJev(), clock)
    reply(store, worker, clock, "+15550000000", "who is this?")
    assert sender.sent == []
    assert alerts(store) == ["text_from_a_number_without_a_conversation"]


def test_a_reply_after_a_hand_off_goes_to_the_owner_not_the_bot(store: Store) -> None:
    jev = ScriptedJev(("unclear", {"wants_human": 0.9}))
    worker, sender, clock, _ = run(store, jev, ["can I talk to a person?"])
    sent_before = len(sender.sent)
    reply(store, worker, clock, DANA_PHONE, "hello?")
    assert len(sender.sent) == sent_before
    assert alerts(store) == ["asked_for_a_person", "homeowner_texted_after_handoff"]


def test_a_second_form_while_a_conversation_runs_sends_no_second_first_text(store: Store) -> None:
    clock = Clock()
    worker, sender = make_worker(store, ScriptedJev(), clock)
    start_conversation(store, worker, clock)
    store.add_lead(make_lead(), make_consent(), clock(), SETTINGS.texting_hours)
    worker.run_once()
    assert len(sender.sent) == 1


# ---- small talk, and times that don't suit ----


def test_flirting_gets_a_fixed_redirect_with_no_team_promise_and_no_alert(store: Store) -> None:
    _, sender, _, _ = run(store, ScriptedJev("off_topic"), ["You seem cute, are you single?"])
    assert sender.bodies[1] == f"{off_topic_text(SETTINGS)} Is the inspection for 12 Saguaro Ln, Mesa AZ?"
    assert QUESTION_FOR_TEAM_LINE not in sender.bodies[1]
    assert alerts(store) == []


def test_small_talk_twice_in_a_row_goes_to_a_person(store: Store) -> None:
    _, _, _, lead_id = run(
        store, ScriptedJev("off_topic", "off_topic"), ["you're cute", "so are you single?"]
    )
    assert store.conversation_result(lead_id) == "not_understood"
    assert alerts(store) == ["bot_could_not_understand"]


def offered_times(body: str) -> list[str]:
    return [line.split(") ", 1)[1] for line in body.splitlines() if line[:2] in ("1)", "2)", "3)")]


def test_none_of_the_times_gets_the_hours_and_later_days_once(store: Store) -> None:
    jev = ScriptedJev(*QUALIFYING_ANSWERS, "none", FIRST_OFFERED_TIME)
    texts = ["yes", "no", "no", "yes", "Can you do Saturday at noon instead?", "the first one"]
    _, sender, _, lead_id = run(store, jev, texts)

    first_offer, later_offer = sender.bodies[-3], sender.bodies[-2]
    assert later_offer.startswith(f"{LATER_TIMES_PREFIX} {inspection_hours_text(SETTINGS)}")
    assert "We do inspections Monday to Friday, 8 AM to 5 PM." in later_offer
    # Every later time is on a day after the last one first offered.
    first_days = [label.rsplit(" at ", 1)[0] for label in offered_times(first_offer)]
    later_days = [label.rsplit(" at ", 1)[0] for label in offered_times(later_offer)]
    assert len(later_days) == 3 and not set(first_days) & set(later_days)
    assert store.booking_label(lead_id) == offered_times(later_offer)[0]
    assert alerts(store) == []


def test_turning_down_the_later_times_too_goes_to_a_person(store: Store) -> None:
    jev = ScriptedJev(*QUALIFYING_ANSWERS, "none", "none")
    _, sender, _, lead_id = run(
        store, jev, ["yes", "no", "no", "yes", "none of those", "none of those either"]
    )
    assert sender.bodies[-1] == NO_TIME_SUITED_TEXT
    assert store.conversation_result(lead_id) == "no_time_suited"
    assert alerts(store) == ["needs_a_different_inspection_time"]


def test_with_no_later_times_open_it_goes_straight_to_a_person(store: Store) -> None:
    # Everything from Thursday on is taken, so only Monday to Wednesday were ever open.
    thursday = phoenix(2026, 10, 8, 0, 0)
    calendar = FakeCalendar(busy=[BusyPeriod(thursday, thursday + timedelta(days=30))])
    jev = ScriptedJev(*QUALIFYING_ANSWERS, "none")
    _, sender, _, lead_id = run(store, jev, ["yes", "no", "no", "yes", "none of those"], calendar=calendar)
    assert sender.bodies[-1] == NO_TIME_SUITED_TEXT
    assert store.conversation_result(lead_id) == "no_time_suited"
