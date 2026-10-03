# lead-bot/tests/test_store.py
# The store: one-transaction intake, the consent record, follow-up scheduling, opt-outs.
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


def test_a_consenting_lead_gets_one_call_due_now(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    assert added.decision == "call"
    assert added.first_call_due == MONDAY_10AM
    assert store.lead_status(added.lead_id) == "following_up"
    assert store.counts()["calls"] == {"scheduled": 1}
    assert store.owner_alerts() == []


def test_a_lead_at_night_is_called_at_eight(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), phoenix(2026, 10, 5, 22, 30), HOURS)
    assert added.first_call_due == phoenix(2026, 10, 6, 8, 0)


def test_no_consent_schedules_nothing_and_alerts_the_owner(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(given=False), MONDAY_10AM, HOURS)
    assert added.decision == "no_call_consent"
    assert added.first_call_due is None
    assert store.counts()["calls"] == {}
    assert [alert.reason for alert in store.owner_alerts()] == ["no_call_consent"]


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
    assert "Desert Ridge Roofing LLC may call me" in text_shown
    assert "artificial (AI-generated) voice" in text_shown
    assert "not a condition of buying anything" in text_shown
    assert ip_address == "203.0.113.7"
    assert recorded_at == MONDAY_10AM.timestamp()


def test_a_second_form_from_the_same_number_starts_no_second_sequence(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    second = store.add_lead(make_lead(), make_consent(), MONDAY_10AM + timedelta(minutes=5), HOURS)
    assert second.decision == "already_in_follow_up"
    assert store.counts()["calls"] == {"scheduled": 1}


def test_simultaneous_duplicate_forms_both_succeed_and_start_one_sequence(
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
    # Neither submission is lost, and only one of them starts calls.
    assert sorted(results) == ["already_in_follow_up", "call"]
    assert Store(database_path).counts()["calls"] == {"scheduled": 1}


def test_an_opted_out_number_is_never_scheduled_again(store: Store) -> None:
    store.add_to_do_not_call(DANA_PHONE, "test", MONDAY_10AM)
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    assert added.decision == "previously_opted_out"
    assert store.counts()["calls"] == {}
    assert [alert.reason for alert in store.owner_alerts()] == ["previously_opted_out"]


def test_opting_out_cancels_scheduled_calls_and_stops_the_lead(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    store.add_to_do_not_call(DANA_PHONE, "pressed 9", MONDAY_10AM)
    assert store.counts()["calls"] == {"cancelled": 1}
    assert store.lead_status(added.lead_id) == "opted_out"
    assert store.is_on_do_not_call(DANA_PHONE)


# ---- outcomes and follow-ups ----


def claim_and_place(store: Store, now_minutes: int = 0) -> int:
    now = MONDAY_10AM + timedelta(minutes=now_minutes)
    due_call = store.claim_due_call(now)
    assert due_call is not None
    store.mark_call_placed(due_call.call_id, f"CA{due_call.call_id}", now)
    return due_call.call_id


def test_no_answer_schedules_the_next_attempt(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    call_id = claim_and_place(store)
    next_due = store.record_call_outcome(call_id, "no_answer", MONDAY_10AM + timedelta(minutes=1), HOURS)
    assert next_due == MONDAY_10AM + timedelta(hours=2)
    assert store.counts()["calls"] == {"completed": 1, "scheduled": 1}


def test_a_repeated_status_update_schedules_no_extra_call(store: Store) -> None:
    # Phone providers retry status updates. The second one must change nothing.
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    call_id = claim_and_place(store)
    store.record_call_outcome(call_id, "no_answer", MONDAY_10AM + timedelta(minutes=1), HOURS)
    repeat = store.record_call_outcome(call_id, "no_answer", MONDAY_10AM + timedelta(minutes=2), HOURS)
    assert repeat is None
    assert store.counts()["calls"] == {"completed": 1, "scheduled": 1}


def test_an_answered_call_ends_the_follow_ups(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    call_id = claim_and_place(store)
    assert store.record_call_outcome(call_id, "answered", MONDAY_10AM, HOURS) is None
    assert store.lead_status(added.lead_id) == "answered"
    assert store.counts()["calls"] == {"completed": 1}


def test_after_four_unanswered_calls_the_owner_takes_over(store: Store) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    now = MONDAY_10AM
    for _ in range(4):
        due_call = store.claim_due_call(now + timedelta(days=10))  # far enough ahead for any due time
        assert due_call is not None
        store.mark_call_placed(due_call.call_id, "CA", now)
        next_due = store.record_call_outcome(due_call.call_id, "no_answer", now, HOURS)
        now = next_due if next_due is not None else now
    assert store.lead_status(added.lead_id) == "unreached"
    assert store.counts()["calls"] == {"completed": 4}
    assert [alert.reason for alert in store.owner_alerts()] == ["not_reached_after_all_attempts"]


def test_an_opt_out_during_the_call_wins_over_its_no_answer_status(store: Store) -> None:
    # The opt-out arrives first; the provider's "call ended" update arrives after. No new call.
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    call_id = claim_and_place(store)
    store.add_to_do_not_call(DANA_PHONE, "pressed 9", MONDAY_10AM)
    assert store.record_call_outcome(call_id, "no_answer", MONDAY_10AM, HOURS) is None
    assert store.counts()["calls"] == {"completed": 1}


def test_a_call_is_claimed_only_once(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    assert store.claim_due_call(MONDAY_10AM) is not None
    assert store.claim_due_call(MONDAY_10AM) is None


def test_a_call_is_not_claimed_before_it_is_due(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), phoenix(2026, 10, 5, 22, 0), HOURS)  # due 8:00 tomorrow
    assert store.claim_due_call(phoenix(2026, 10, 6, 7, 59)) is None
    assert store.claim_due_call(phoenix(2026, 10, 6, 8, 0)) is not None
