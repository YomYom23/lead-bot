# lead-bot/tests/test_store.py
# The store: one-transaction intake, the consent record, opening texts and reminders, incoming
# texts in order, opt-outs, and recovery after a crash.
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import DANA_PHONE, HOURS, MONDAY_10AM, make_consent, make_lead, phoenix

from lead_bot.leads import IntakeDecision, decide_intake
from lead_bot.store import Store
from lead_bot.turn_data import TurnOutcome


def text_counts(store: Store) -> dict[str, int]:
    return store.counts()["texts_out"]


def add_do_not_call_row_by_hand(database_path: Path, phone: str) -> None:
    """A do-not-call entry made outside Store, as the owner might with a database tool."""
    connection = sqlite3.connect(database_path)
    with connection:
        connection.execute(
            "INSERT INTO do_not_call (phone, added_at, source) VALUES (?, ?, 'owner')",
            (phone, MONDAY_10AM.timestamp()),
        )
    connection.close()


# ---- intake ----


def test_a_consenting_lead_gets_its_first_text_due_now(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    assert added.decision == "text"
    assert added.first_text_due == MONDAY_10AM
    assert store.lead_status(added.lead_id) == "following_up"
    assert [due.attempt for due in store.due_openings(MONDAY_10AM)] == [1]
    assert store.owner_alerts() == []


def test_a_lead_at_night_is_texted_at_eight(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), phoenix(2026, 10, 5, 22, 30), HOURS)
    assert added.first_text_due == phoenix(2026, 10, 6, 8, 0)
    assert store.due_openings(phoenix(2026, 10, 6, 7, 59)) == []
    assert len(store.due_openings(phoenix(2026, 10, 6, 8, 0))) == 1


def test_no_consent_schedules_nothing_and_alerts_the_owner(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(given=False), MONDAY_10AM, HOURS)
    assert added.decision == "no_text_consent"
    assert added.first_text_due is None
    assert store.due_openings(MONDAY_10AM + timedelta(days=30)) == []
    assert [alert.reason for alert in store.owner_alerts()] == ["no_text_consent"]


def test_the_consent_record_keeps_what_the_person_saw(store: Store, tmp_path: Path) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    connection = sqlite3.connect(tmp_path / "leads.db")
    row = connection.execute(
        "SELECT given, version, text_shown, ip_address, recorded_at FROM consents WHERE lead_id = ?",
        (added.lead_id,),
    ).fetchone()
    connection.close()
    given, version, text_shown, ip_address, recorded_at = row
    assert given == 1
    assert version == make_consent().version
    assert "Desert Ridge Roofing LLC may send automated text messages" in text_shown
    assert "not a condition of buying anything" in text_shown
    assert "STOP to opt out" in text_shown
    assert ip_address == "203.0.113.7"
    assert recorded_at == MONDAY_10AM.timestamp()


def test_a_second_form_from_the_same_number_starts_no_second_conversation(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    second = store.add_lead(make_lead(), make_consent(), MONDAY_10AM + timedelta(minutes=5), HOURS)
    assert second.decision == "already_in_follow_up"
    assert len(store.due_openings(MONDAY_10AM + timedelta(minutes=5))) == 1


def test_simultaneous_duplicate_forms_both_succeed_and_start_one_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two submissions of the same number at the same moment, from two threads. A pause between
    # add_lead's read and its write makes the threads overlap there every time; without it they
    # almost never would, and the test would pass whether or not the lock works.
    real_decide_intake = decide_intake

    def slow_decide_intake(consent_given: bool, on_do_not_call: bool, already: bool) -> IntakeDecision:
        time.sleep(0.3)
        return real_decide_intake(consent_given, on_do_not_call, already)

    # Replace the name store.py looks up, not the original in leads.py.
    monkeypatch.setattr("lead_bot.store.decide_intake", slow_decide_intake)
    database_path = tmp_path / "race.db"
    Store(database_path)  # create the schema once, up front
    results: list[str] = []

    def submit() -> None:
        try:
            added = Store(database_path).add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
            results.append(added.decision)
        except Exception as error:  # recorded, so the assertion below shows it
            results.append(f"{type(error).__name__}: {error}")

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    # Neither submission is lost, and only one of them starts texting.
    assert sorted(results) == ["already_in_follow_up", "text"]
    assert len(Store(database_path).due_openings(MONDAY_10AM)) == 1


def test_an_opted_out_number_is_never_scheduled_again(store: Store) -> None:
    store.add_to_do_not_call(DANA_PHONE, "test", MONDAY_10AM)
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    assert added.decision == "previously_opted_out"
    assert store.due_openings(MONDAY_10AM) == []
    assert [alert.reason for alert in store.owner_alerts()] == ["previously_opted_out"]


# ---- opening texts and reminders ----


def queue_first_text(store: Store, lead_id: int) -> None:
    assert store.queue_opening_text(lead_id, 1, "first text", MONDAY_10AM, HOURS) == "queued"


def test_queuing_the_first_text_schedules_the_first_reminder(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    queue_first_text(store, added.lead_id)
    assert text_counts(store) == {"queued": 1}
    assert store.due_openings(MONDAY_10AM + timedelta(hours=1, minutes=59)) == []
    assert [due.attempt for due in store.due_openings(MONDAY_10AM + timedelta(hours=2))] == [2]


def test_the_same_opening_text_is_queued_only_once(store: Store) -> None:
    # Two worker passes (or two workers) both saw attempt 1 due; only one may queue it.
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    queue_first_text(store, added.lead_id)
    assert store.queue_opening_text(added.lead_id, 1, "first text", MONDAY_10AM, HOURS) == "not_due"
    assert text_counts(store) == {"queued": 1}


def test_an_opening_text_outside_texting_hours_is_moved_to_the_morning(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    late = phoenix(2026, 10, 5, 21, 0)  # the worker was down until after the window closed
    assert store.queue_opening_text(added.lead_id, 1, "first text", late, HOURS) == "rescheduled"
    assert text_counts(store) == {}
    assert len(store.due_openings(phoenix(2026, 10, 6, 8, 0))) == 1


def test_an_opening_text_is_not_queued_once_the_number_opted_out(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    store.add_to_do_not_call(DANA_PHONE, "test", MONDAY_10AM)
    assert store.queue_opening_text(added.lead_id, 1, "first text", MONDAY_10AM, HOURS) == "not_due"
    assert text_counts(store) == {}
    assert store.lead_status(added.lead_id) == "opted_out"


def test_a_do_not_call_row_added_by_hand_still_stops_the_next_opening_text(
    store: Store, tmp_path: Path
) -> None:
    # The owner (or a future import) adds the number straight into the table, skipping the
    # helper that would also stop the reminders. The check when queuing still catches it.
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    add_do_not_call_row_by_hand(tmp_path / "leads.db", DANA_PHONE)
    assert store.queue_opening_text(added.lead_id, 1, "first text", MONDAY_10AM, HOURS) == "stopped"
    assert text_counts(store) == {}
    assert store.lead_status(added.lead_id) == "opted_out"


def test_any_reply_stops_the_reminders(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    queue_first_text(store, added.lead_id)
    store.record_incoming_text(DANA_PHONE, "yes", "SMIN1", MONDAY_10AM + timedelta(minutes=5))
    incoming = store.claim_incoming_text(MONDAY_10AM + timedelta(minutes=5))
    assert incoming is not None
    store.save_turn(incoming.text_id, added.lead_id, TurnOutcome("leak", (), 0, 0), MONDAY_10AM)
    assert store.due_openings(MONDAY_10AM + timedelta(days=10)) == []
    assert store.lead_status(added.lead_id) == "in_conversation"


def test_after_every_opening_text_goes_unanswered_the_owner_is_told(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    now = MONDAY_10AM
    for attempt in range(1, 5):
        due = store.due_openings(now + timedelta(days=10))
        assert [opening.attempt for opening in due] == [attempt]
        assert store.queue_opening_text(added.lead_id, attempt, f"text {attempt}", now, HOURS) == "queued"
        now += timedelta(days=1, hours=1)
    assert store.due_openings(now + timedelta(days=30)) == []
    store.close_quiet_conversations(now + timedelta(days=2), quiet_for=timedelta(hours=24))
    assert store.lead_status(added.lead_id) == "unreached"
    assert [alert.reason for alert in store.owner_alerts()] == ["no_reply_to_any_text"]


# ---- incoming texts ----


def test_the_same_incoming_message_saved_twice_is_answered_once(store: Store) -> None:
    # Twilio sends the webhook again when our answer was slow. The second copy changes nothing.
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    assert store.record_incoming_text(DANA_PHONE, "yes", "SMIN1", MONDAY_10AM) is not None
    assert store.record_incoming_text(DANA_PHONE, "yes", "SMIN1", MONDAY_10AM) is None
    assert store.claim_incoming_text(MONDAY_10AM) is not None
    assert store.claim_incoming_text(MONDAY_10AM) is None


def test_two_quick_texts_from_one_number_are_answered_in_order(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    store.record_incoming_text(DANA_PHONE, "first", "SMIN1", MONDAY_10AM)
    store.record_incoming_text(DANA_PHONE, "second", "SMIN2", MONDAY_10AM)
    first = store.claim_incoming_text(MONDAY_10AM)
    assert first is not None and first.body == "first"
    # The second waits while the first is being handled, so it sees what the first changed.
    assert store.claim_incoming_text(MONDAY_10AM) is None
    store.save_turn(first.text_id, added.lead_id, TurnOutcome("leak", (), 0, 0), MONDAY_10AM)
    second = store.claim_incoming_text(MONDAY_10AM)
    assert second is not None and second.body == "second"
    assert second.conversation is not None and second.conversation.stage == "leak"
    assert [turn.text for turn in second.history] == ["first"]


def test_a_text_retried_later_is_not_claimed_before_then(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    store.record_incoming_text(DANA_PHONE, "yes", "SMIN1", MONDAY_10AM)
    incoming = store.claim_incoming_text(MONDAY_10AM)
    assert incoming is not None and incoming.tries == 1
    store.retry_incoming_later(incoming.text_id, MONDAY_10AM + timedelta(minutes=1))
    assert store.claim_incoming_text(MONDAY_10AM + timedelta(seconds=59)) is None
    again = store.claim_incoming_text(MONDAY_10AM + timedelta(minutes=1))
    assert again is not None and again.tries == 2


def test_a_text_from_an_unknown_number_has_no_conversation(store: Store) -> None:
    store.record_incoming_text("+15550000000", "who is this?", "SMIN1", MONDAY_10AM)
    incoming = store.claim_incoming_text(MONDAY_10AM)
    assert incoming is not None
    assert incoming.conversation is None
    assert incoming.history == []


# ---- opt-outs ----


def test_opting_out_cancels_queued_texts_and_stops_the_lead(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    queue_first_text(store, added.lead_id)
    store.add_to_do_not_call(DANA_PHONE, "replied STOP", MONDAY_10AM)
    assert text_counts(store) == {"cancelled": 1}
    assert store.lead_status(added.lead_id) == "opted_out"
    assert store.conversation_result(added.lead_id) == "opted_out"
    assert store.is_on_do_not_call(DANA_PHONE)
    assert store.due_openings(MONDAY_10AM + timedelta(days=30)) == []


def test_an_opt_out_turn_queues_only_the_confirmation(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    store.record_incoming_text(DANA_PHONE, "please stop texting me", "SMIN1", MONDAY_10AM)
    incoming = store.claim_incoming_text(MONDAY_10AM)
    assert incoming is not None
    outcome = TurnOutcome(
        "address",
        (),
        0,
        0,
        replies=["a reply that must not go out"],
        stop_confirmation="You're opted out.",
        do_not_contact_source="asked to stop",
        close_conversation=True,
        result="opted_out",
    )
    store.save_turn(incoming.text_id, added.lead_id, outcome, MONDAY_10AM)
    outgoing = store.claim_queued_text()
    assert outgoing is not None
    assert (outgoing.kind, outgoing.body) == ("stop_confirmation", "You're opted out.")
    assert store.claim_queued_text() is None


# ---- sending and recovery ----


def test_a_queued_text_is_claimed_once(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    queue_first_text(store, added.lead_id)
    assert store.claim_queued_text() is not None
    assert store.claim_queued_text() is None


def test_after_a_crash_a_half_sent_text_is_never_resent(store: Store) -> None:
    # The worker stopped between claiming the text and recording the send. Twilio may have it,
    # so it's marked unknown and the owner checks; sending again could deliver it twice.
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    queue_first_text(store, added.lead_id)
    assert store.claim_queued_text() is not None
    assert store.mark_interrupted_sends_unknown(MONDAY_10AM) == 1
    assert text_counts(store) == {"unknown": 1}
    assert store.claim_queued_text() is None
    assert [alert.reason for alert in store.owner_alerts()] == ["text_may_not_have_sent"]


def test_after_a_crash_a_half_answered_text_is_answered_again(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    store.record_incoming_text(DANA_PHONE, "yes", "SMIN1", MONDAY_10AM)
    assert store.claim_incoming_text(MONDAY_10AM) is not None
    assert store.requeue_interrupted_incoming() == 1
    assert store.claim_incoming_text(MONDAY_10AM) is not None
