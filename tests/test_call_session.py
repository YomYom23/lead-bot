# lead-bot/tests/test_call_session.py
# Answered phone calls, driven end to end through CallSession and a real database: what each kind
# of caller hears, what's saved (the lead, answers, booking, consent), what the owner is told,
# and what happens on a hang-up. Jev and the writer are scripted; no network.
from __future__ import annotations

from datetime import timedelta

from conftest import DANA_PHONE, HOURS, MONDAY_10AM, make_consent, make_lead
from test_booking import FakeCalendar
from test_owner_alerts import RecordingMailer
from text_fakes import (
    FIRST_OFFERED_TIME,
    GUARD_CHECK,
    SETTINGS,
    Clock,
    ScriptedJev,
    StubWriter,
    make_services,
)

from lead_bot.call_script import TEXT_CONSENT_QUESTION, greeting
from lead_bot.call_session import Action, CallSession, ContinueTurn, EndCall, Say
from lead_bot.jev import JevError
from lead_bot.owner_alerts import OwnerAlertNotifier
from lead_bot.store import VERBAL_CONSENT_VERSION, ConnectedCall, Store
from lead_bot.writer import NOT_IN_FACTS

# A caller nobody has heard from, built so no phone-like string is typed whole.
NEW_CALLER = "+1" + "480" + "555" + "0166"
BOOKING_ANSWERS = ("roof", "gave_name", "gave_address", "yes", "no", "no", "yes", FIRST_OFFERED_TIME)
BOOKING_WORDS = (
    "Hi, I've got hail damage on my roof",
    "My name is Pat Moreno",
    "It's 8 Mesquite Road, Tempe.",
    "Yes",
    "No",
    "No",
    "Yes, it's my house",
    "The first one",
)


def connect(store: Store, phone: str = NEW_CALLER, call_sid: str = "CA" + "0" * 32) -> ConnectedCall:
    incoming = store.start_incoming_call(call_sid, phone, MONDAY_10AM)
    connected = store.connect_call(incoming.call_id, incoming.relay_token, MONDAY_10AM)
    assert connected is not None
    return connected


def make_session(
    store: Store,
    jev: ScriptedJev,
    phone: str = NEW_CALLER,
    calendar: FakeCalendar | None = None,
    writer: StubWriter | None = None,
) -> CallSession:
    return CallSession(connect(store, phone), SETTINGS, store, make_services(jev, calendar, writer), Clock())


def spoken(actions: list[Action]) -> list[str]:
    return [action.text for action in actions if isinstance(action, Say)]


def say_all(session: CallSession, words: tuple[str, ...]) -> list[Action]:
    """Say each line in turn; returns the bot's actions after the last one."""
    actions: list[Action] = []
    for line in words:
        actions = session.on_prompt(line)
    return actions


def alerts(store: Store) -> list[str]:
    return [alert.reason for alert in store.owner_alerts()]


def bot_turns(store: Store, call_id: int) -> list[str]:
    return [turn.text for turn in store.call_transcript(call_id) if turn.speaker == "bot"]


def queued_texts(store: Store) -> list[tuple[str, str]]:
    with store._read() as connection:
        rows = connection.execute("SELECT phone, body FROM texts WHERE direction = 'out' ORDER BY text_id")
        return [(row["phone"], row["body"]) for row in rows.fetchall()]


# ---- a new caller books ----


def test_the_greeting_is_the_first_line_of_the_transcript(store: Store) -> None:
    session = make_session(store, ScriptedJev())
    assert store.call_transcript(session._call.call_id)[0].text == greeting(SETTINGS)
    assert greeting(SETTINGS).startswith("Thanks for calling Desert Ridge Roofing LLC. Sorry we missed you.")


def test_a_new_caller_books_and_agrees_to_a_text(store: Store) -> None:
    jev = ScriptedJev(*BOOKING_ANSWERS, "yes", "no")
    calendar = FakeCalendar()
    session = make_session(store, jev, calendar=calendar)

    actions = say_all(session, BOOKING_WORDS)
    assert spoken(actions)[0].startswith("You're all set for ")
    assert spoken(actions)[0].endswith(TEXT_CONSENT_QUESTION)
    session.on_prompt("Sure")
    last = session.on_prompt("No, that's everything")

    assert isinstance(last[-1], EndCall) and last[-1].reason == "booked"
    lead_id = store.call_lead_id(session._call.call_id)
    assert lead_id is not None
    assert store.lead_status(lead_id) == "booked"
    assert len(calendar.events) == 1
    assert store.lead_answers(lead_id) == {
        "address_confirmed": "yes",
        "active_leak": "no",
        "insurance_claim": "no",
        "homeowner": "yes",
    }
    with store._read() as connection:
        lead = connection.execute(
            "SELECT full_name, address FROM leads WHERE lead_id = ?", (lead_id,)
        ).fetchone()
        consent = connection.execute("SELECT * FROM consents WHERE lead_id = ?", (lead_id,)).fetchone()
    assert (lead["full_name"], lead["address"]) == ("Pat Moreno", "8 Mesquite Road, Tempe")
    assert (consent["given"], consent["version"], consent["text_shown"]) == (
        1,
        VERBAL_CONSENT_VERSION,
        TEXT_CONSENT_QUESTION,
    )
    ((phone, body),) = queued_texts(store)
    assert (
        phone == NEW_CALLER and "8 Mesquite Road, Tempe" in body and body.endswith("Reply STOP to opt out.")
    )
    assert store.call_result(session._call.call_id) == "booked"
    assert alerts(store) == ["inspection_booked"]


def test_the_address_is_read_back_before_it_is_used(store: Store) -> None:
    session = make_session(store, ScriptedJev("roof", "gave_name", "gave_address"))
    actions = say_all(session, BOOKING_WORDS[:3])
    assert spoken(actions) == ["I heard 8 Mesquite Road, Tempe. Is that right?"]


def test_a_no_to_texts_is_recorded_and_nothing_is_texted(store: Store) -> None:
    session = make_session(store, ScriptedJev(*BOOKING_ANSWERS, "no"))
    say_all(session, BOOKING_WORDS)
    session.on_prompt("No thanks")
    lead_id = store.call_lead_id(session._call.call_id)
    with store._read() as connection:
        given = connection.execute("SELECT given FROM consents WHERE lead_id = ?", (lead_id,)).fetchone()[0]
    assert given == 0
    assert queued_texts(store) == []


def test_a_hidden_number_is_never_asked_about_texts(store: Store) -> None:
    session = make_session(store, ScriptedJev(*BOOKING_ANSWERS), phone="anonymous")
    actions = say_all(session, BOOKING_WORDS)
    assert spoken(actions)[0].endswith("Is there anything else I can help with?")


def test_a_number_on_the_do_not_call_list_is_never_asked_about_texts(store: Store) -> None:
    store.add_to_do_not_call(NEW_CALLER, "replied STOP", MONDAY_10AM)
    session = make_session(store, ScriptedJev(*BOOKING_ANSWERS))
    actions = say_all(session, BOOKING_WORDS)
    assert TEXT_CONSENT_QUESTION not in spoken(actions)[0]
    assert queued_texts(store) == []


def test_a_misheard_address_twice_books_anyway_and_tells_the_owner(store: Store) -> None:
    jev = ScriptedJev("roof", "gave_name", "gave_address", "no", "gave_address", "no", "no")
    session = make_session(store, jev)
    actions = say_all(
        session,
        ("roof leak", "Pat", "8 Mesquite", "No", "8 Mesquite Road", "No, that's still wrong", "No"),
    )
    assert spoken(actions)[0].startswith("Are you planning to file an insurance claim")
    lead_id = store.call_lead_id(session._call.call_id)
    assert lead_id is not None
    assert store.lead_answers(lead_id)["address_confirmed"] == "no"
    assert "address_needs_correcting" in alerts(store)


def test_an_active_leak_alerts_the_owner_during_the_call(store: Store) -> None:
    session = make_session(store, ScriptedJev("roof", "gave_name", "gave_address", "yes", "yes"))
    actions = say_all(session, ("my roof is leaking", "Pat", "8 Mesquite Road", "yes", "yes, right now"))
    assert spoken(actions)[0].startswith("I'm sorry to hear that. I'll flag this as urgent")
    assert alerts(store) == ["URGENT_active_leak"]


# ---- messages ----


def test_a_caller_about_something_else_leaves_a_message(store: Store) -> None:
    session = make_session(store, ScriptedJev("message", "gave_name"))
    say_all(session, ("I'm calling about my invoice from last week",))
    last = session.on_prompt("This is Sam Lee")
    assert spoken(last) == [
        "Thanks, Sam. I'll pass that on to the team, and someone will call you back. Goodbye."
    ]
    assert store.call_result(session._call.call_id) == "message"
    assert store.call_lead_id(session._call.call_id) is None  # not a roofing lead
    assert alerts(store) == ["caller_left_a_message"]
    with store._read() as connection:
        message = connection.execute("SELECT message FROM calls").fetchone()[0]
    assert message == "Sam Lee: I'm calling about my invoice from last week"


def test_asking_for_a_person_takes_a_message(store: Store) -> None:
    session = make_session(store, ScriptedJev(("unclear", {"wants_human": 0.9}), "gave_name"))
    first = session.on_prompt("Can I just talk to somebody?")
    assert spoken(first) == ["Of course. I'll have someone call you back. Can I get your name?"]
    session.on_prompt("Sam")
    assert alerts(store) == ["caller_left_a_message"]


def test_the_message_alert_text_and_email_carry_the_message_and_the_call(store: Store) -> None:
    session = make_session(store, ScriptedJev("message", "gave_name"))
    say_all(session, ("Is my gutter job still on for Friday?", "Sam Lee"))
    mailer = RecordingMailer()
    OwnerAlertNotifier(
        store, SETTINGS, mailer, "bot@example.com", "owner@example.com", clock=Clock()
    ).notify_pending()
    body = mailer.sent[0].get_content()
    assert "Their message:\n  Sam Lee: Is my gutter job still on for Friday?" in body
    assert "The call:\n  Bot: Thanks for calling" in body
    assert "  Caller: Sam Lee" in body
    texts = store.alerts_to_text(MONDAY_10AM, ("caller_left_a_message",), timedelta(hours=2))
    assert texts[0].call_message == "Sam Lee: Is my gutter job still on for Friday?"


# ---- callers already on file ----


def test_a_known_caller_hears_their_address_only_after_giving_their_name(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    session = make_session(store, ScriptedJev("roof", "gave_name"), phone=DANA_PHONE)
    first = session.on_prompt("I need my roof looked at")
    assert "Saguaro" not in spoken(first)[0]
    second = session.on_prompt("It's Dana")
    assert spoken(second) == ["Thanks, Dana. Is this about the roof at 12 Saguaro Ln, Mesa AZ?"]


def test_a_different_name_on_a_known_number_is_asked_for_the_address(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    session = make_session(store, ScriptedJev("roof", "gave_name"), phone=DANA_PHONE)
    say_all(session, ("roof leak", "This is Jordan"))
    assert all("Saguaro" not in line for line in bot_turns(store, session._call.call_id))


def test_a_known_caller_books_on_their_lead_and_gets_the_text_without_being_asked(store: Store) -> None:
    lead_id = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS).lead_id
    jev = ScriptedJev("roof", "gave_name", "yes", "no", "no", "yes", FIRST_OFFERED_TIME)
    session = make_session(store, jev, phone=DANA_PHONE)
    actions = say_all(session, ("roof inspection please", "Dana Ruiz", "yes", "no", "no", "yes", "the first"))
    assert spoken(actions)[0].endswith("I'll text you the details. Is there anything else I can help with?")
    assert store.call_lead_id(session._call.call_id) == lead_id
    assert store.lead_status(lead_id) == "booked"
    assert [phone for phone, _ in queued_texts(store)][-1] == DANA_PHONE


def test_a_known_caller_already_booked_leaves_a_message_for_the_team(store: Store) -> None:
    lead_id = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS).lead_id
    with store._write_transaction() as connection:
        connection.execute(
            "INSERT INTO bookings (lead_id, starts_at, slot_label, booked_at) "
            "VALUES (?, ?, 'Tuesday at 8 AM', ?)",
            (lead_id, MONDAY_10AM.timestamp(), MONDAY_10AM.timestamp()),
        )
    session = make_session(store, ScriptedJev("roof", "gave_name"), phone=DANA_PHONE)
    asked = say_all(session, ("about my inspection", "Dana"))
    assert spoken(asked) == [
        "Thanks, Dana. I see your inspection is booked for Tuesday at 8 AM. "
        "What would you like me to pass on to the team?"
    ]
    session.on_prompt("Can we move it to Thursday?")
    assert alerts(store) == ["caller_left_a_message"]
    assert store.owner_alerts()[0].lead_id == lead_id


# ---- endings ----


def test_take_me_off_your_list_ends_the_call_without_asking_jev(store: Store) -> None:
    session = make_session(store, ScriptedJev())  # nothing scripted: Jev must not be asked
    last = session.on_prompt("Please take me off your list")
    assert spoken(last) == ["Understood. We won't contact you again. Goodbye."]
    assert store.is_on_do_not_call(NEW_CALLER)
    assert store.call_result(session._call.call_id) == "opted_out"


def test_a_hang_up_before_booking_alerts_the_owner_and_texts_nobody(store: Store) -> None:
    session = make_session(store, ScriptedJev("roof", "gave_name"))
    say_all(session, ("my roof leaks", "Pat"))
    session.on_hangup()
    assert store.call_result(session._call.call_id) == "hung_up"
    assert alerts(store) == ["caller_hung_up"]
    assert queued_texts(store) == []
    session.on_hangup()  # a second hang-up changes nothing
    assert alerts(store) == ["caller_hung_up"]


def test_a_hang_up_after_giving_a_name_on_the_message_path_is_a_message(store: Store) -> None:
    session = make_session(store, ScriptedJev(("unclear", {"wants_human": 0.9})))
    session.on_prompt("I need to talk to the owner")
    session._caller_name = "Sam"  # as if the name was given just before the line dropped
    session.on_hangup()
    assert alerts(store) == ["caller_left_a_message"]


def test_a_hang_up_after_booking_keeps_the_booking(store: Store) -> None:
    session = make_session(store, ScriptedJev(*BOOKING_ANSWERS))
    say_all(session, BOOKING_WORDS)
    session.on_hangup()
    assert store.call_result(session._call.call_id) == "booked"
    assert alerts(store) == ["inspection_booked"]


def test_jev_down_twice_hands_the_caller_to_a_person(store: Store) -> None:
    session = make_session(store, ScriptedJev(JevError("down"), JevError("down")))
    first = session.on_prompt("hello?")
    assert spoken(first) == ["Sorry, could you say that again?"]
    last = session.on_prompt("hello??")
    assert isinstance(last[-1], EndCall)
    assert alerts(store) == ["jev_unavailable"]


def test_nothing_is_said_after_the_call_ended(store: Store) -> None:
    session = make_session(store, ScriptedJev())
    session.on_hangup()
    assert session.on_prompt("hello") == []


# ---- off-script questions ----


def test_a_question_the_facts_answer_is_spoken_after_let_me_check(store: Store) -> None:
    writer = StubWriter("An inspection takes about 60 minutes.")
    session = make_session(store, ScriptedJev("other_question", GUARD_CHECK), writer=writer)
    first = session.on_prompt("How long does an inspection take?")
    assert spoken(first) == ["Let me check."] and isinstance(first[-1], ContinueTurn)
    second = session.continue_turn()
    assert spoken(second) == ["An inspection takes about 60 minutes. What can I do for you?"]
    facts, _ = writer.calls[0]
    assert "answers the business's phone, and the call is transcribed" in facts


def test_a_question_the_facts_dont_answer_goes_to_the_team(store: Store) -> None:
    session = make_session(store, ScriptedJev("other_question"), writer=StubWriter(NOT_IN_FACTS))
    session.on_prompt("Do you install solar panels?")
    reply = session.continue_turn()
    assert spoken(reply)[0].startswith("I'll pass that question to our team")
    assert alerts(store) == ["homeowner_question_for_the_team:not_in_facts"]


# ---- the call's one-time token ----


def test_a_call_connects_once_and_only_with_its_token(store: Store) -> None:
    incoming = store.start_incoming_call("CA" + "1" * 32, NEW_CALLER, MONDAY_10AM)
    assert (
        store.start_incoming_call("CA" + "1" * 32, NEW_CALLER, MONDAY_10AM) == incoming
    )  # a retried webhook
    assert store.connect_call(incoming.call_id, "wrong", MONDAY_10AM) is None
    assert store.connect_call(incoming.call_id, incoming.relay_token, MONDAY_10AM) is not None
    assert store.connect_call(incoming.call_id, incoming.relay_token, MONDAY_10AM) is None  # only once


# ---- the confirmation text's own checks ----
# The call never asks the store to text without consent, so these call it directly: the store's
# re-check must hold on its own, as for a lead whose consent row says no or who opted out since.


def lead_made_on_a_call(store: Store) -> int:
    call = connect(store)
    return store.add_lead_from_call(call.call_id, "Pat Moreno", NEW_CALLER, "8 Mesquite Road", MONDAY_10AM)


def test_the_store_texts_no_one_whose_consent_says_no(store: Store) -> None:
    lead_id = lead_made_on_a_call(store)
    store.record_text_consent(lead_id, "CA-test", False, TEXT_CONSENT_QUESTION, MONDAY_10AM)
    store.queue_call_confirmation(lead_id, "booked", MONDAY_10AM)
    assert queued_texts(store) == []


def test_the_store_texts_no_one_without_a_consent_row(store: Store) -> None:
    store.queue_call_confirmation(lead_made_on_a_call(store), "booked", MONDAY_10AM)
    assert queued_texts(store) == []


def test_the_store_texts_no_one_on_the_do_not_call_list(store: Store) -> None:
    lead_id = lead_made_on_a_call(store)
    store.record_text_consent(lead_id, "CA-test", True, TEXT_CONSENT_QUESTION, MONDAY_10AM)
    store.add_to_do_not_call(NEW_CALLER, "replied STOP", MONDAY_10AM)
    store.queue_call_confirmation(lead_id, "booked", MONDAY_10AM)
    assert queued_texts(store) == []


def test_a_known_caller_booking_another_property_is_asked_about_texts(store: Store) -> None:
    # Their form consent was for the old address's lead; the new property's lead has none yet,
    # so promising "I'll text you the details" would be a text that never comes.
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    jev = ScriptedJev("roof", "gave_name", "no", "gave_address", "yes", "no", "no", "yes", FIRST_OFFERED_TIME)
    session = make_session(store, jev, phone=DANA_PHONE)
    words = (
        "roof inspection",
        "Dana",
        "No, my other house",
        "40 Palm Drive, Gilbert",
        "yes",
        "no",
        "no",
        "yes",
        "first",
    )
    actions = say_all(session, words)
    assert spoken(actions)[0].endswith(TEXT_CONSENT_QUESTION)
    assert queued_texts(store) == []
