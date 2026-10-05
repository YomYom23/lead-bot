# lead-bot/tests/test_worker.py
# The worker's own rules, separate from what the conversation says: the texting window for texts
# the bot starts, the last checks before a send, what a failed send means, and restart recovery.
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from conftest import DANA_PHONE, make_consent, make_lead, phoenix
from test_store import add_do_not_call_row_by_hand
from text_fakes import SETTINGS, Clock, ScriptedJev, make_worker, reply, start_conversation

from lead_bot.store import Store
from lead_bot.text_script import opt_out_confirmation
from lead_bot.worker import OPT_OUT_CONFIRMATION_LIMIT


def text_counts(store: Store) -> dict[str, int]:
    return store.counts()["texts_out"]


def test_a_lead_at_night_gets_nothing_until_eight(store: Store) -> None:
    clock = Clock(phoenix(2026, 10, 5, 22, 0))
    worker, sender = make_worker(store, ScriptedJev(), clock)
    store.add_lead(make_lead(), make_consent(), clock(), SETTINGS.texting_hours)
    worker.run_once()
    clock.now = phoenix(2026, 10, 6, 7, 59)
    worker.run_once()
    assert sender.sent == []
    clock.now = phoenix(2026, 10, 6, 8, 0)
    worker.run_once()
    assert len(sender.sent) == 1


def test_reminders_follow_the_schedule_and_stop_at_the_last(store: Store) -> None:
    clock = Clock()
    worker, sender = make_worker(store, ScriptedJev(), clock)
    start_conversation(store, worker, clock)
    for _ in range(24 * 10):  # ten quiet days, an hour at a time
        clock.now += timedelta(hours=1)
        worker.run_once()
    assert len(sender.sent) == 4  # the first text and three reminders
    assert "no_reply_to_any_text" in [alert.reason for alert in store.owner_alerts()]


def test_a_reply_at_night_is_answered_at_once(store: Store) -> None:
    # The window is for texts the bot starts. A reply to the homeowner's own text isn't held.
    clock = Clock()
    worker, sender = make_worker(store, ScriptedJev("yes"), clock)
    start_conversation(store, worker, clock)
    clock.now = phoenix(2026, 10, 5, 23, 30)
    reply(store, worker, clock, DANA_PHONE, "yes")
    assert len(sender.sent) == 2


def test_a_reminder_queued_just_before_the_window_closes_waits_for_morning(store: Store) -> None:
    clock = Clock()
    worker, sender = make_worker(store, ScriptedJev(), clock)
    lead_id = start_conversation(store, worker, clock)
    # The reminder is queued just before the window closes, but the send loop reaches it after.
    clock.now = phoenix(2026, 10, 5, 20, 29)
    assert store.queue_opening_text(lead_id, 2, "reminder", clock(), SETTINGS.texting_hours) == "queued"
    clock.now = phoenix(2026, 10, 5, 20, 31)
    while worker.send_next_queued():
        pass
    assert len(sender.sent) == 1
    assert [due.attempt for due in store.due_openings(phoenix(2026, 10, 6, 8, 0))] == [2]


def test_a_queued_text_is_cancelled_if_the_number_opts_out_before_it_goes(store: Store) -> None:
    clock = Clock()
    worker, sender = make_worker(store, ScriptedJev(), clock)
    store.add_lead(make_lead(), make_consent(), clock(), SETTINGS.texting_hours)
    worker.queue_due_openings()
    store.add_to_do_not_call(DANA_PHONE, "replied STOP", clock())
    while worker.send_next_queued():
        pass
    assert sender.sent == []


def test_a_do_not_call_row_added_by_hand_stops_a_text_already_queued(store: Store, tmp_path: Path) -> None:
    # Added outside Store, so nothing cancelled the queued text. The check at send time must.
    clock = Clock()
    worker, sender = make_worker(store, ScriptedJev(), clock)
    store.add_lead(make_lead(), make_consent(), clock(), SETTINGS.texting_hours)
    worker.queue_due_openings()
    add_do_not_call_row_by_hand(tmp_path / "leads.db", DANA_PHONE)
    while worker.send_next_queued():
        pass
    assert sender.sent == []
    assert text_counts(store) == {"cancelled": 1}


def test_an_opt_out_confirmation_goes_out_despite_the_do_not_call_entry(store: Store) -> None:
    clock = Clock()
    worker, sender = make_worker(store, ScriptedJev(), clock)
    start_conversation(store, worker, clock)
    reply(store, worker, clock, DANA_PHONE, "please stop texting me")
    assert sender.bodies[-1] == opt_out_confirmation(SETTINGS)


def test_a_stale_opt_out_confirmation_is_dropped(store: Store) -> None:
    # The FCC rule allows the one confirmation within five minutes. Later than that, none.
    clock = Clock()
    worker, sender = make_worker(store, ScriptedJev(), clock)
    start_conversation(store, worker, clock)
    clock.now += timedelta(seconds=30)
    store.record_incoming_text(DANA_PHONE, "please stop texting me", "SMIN1", clock())
    worker.answer_next_incoming()  # the confirmation is queued but the worker stops before sending
    clock.now += OPT_OUT_CONFIRMATION_LIMIT + timedelta(seconds=1)
    while worker.send_next_queued():
        pass
    assert len(sender.sent) == 1
    assert text_counts(store) == {"sent": 1, "cancelled": 1}


def test_a_refused_send_is_failed_and_not_retried(store: Store) -> None:
    clock = Clock()
    worker, sender = make_worker(store, ScriptedJev(), clock)
    sender.refuse = "invalid number"
    start_conversation(store, worker, clock)
    sender.refuse = None
    worker.run_once()
    assert sender.sent == []
    assert text_counts(store) == {"failed": 1}


def test_a_send_that_may_have_gone_is_never_retried_and_the_owner_checks(store: Store) -> None:
    clock = Clock()
    worker, sender = make_worker(store, ScriptedJev(), clock)
    sender.fail = TimeoutError("read timed out")
    start_conversation(store, worker, clock)
    sender.fail = None
    worker.run_once()
    assert sender.sent == []
    assert text_counts(store) == {"unknown": 1}
    assert "text_may_not_have_sent" in [alert.reason for alert in store.owner_alerts()]


def test_after_a_restart_a_half_answered_text_is_answered(store: Store) -> None:
    clock = Clock()
    worker, sender = make_worker(store, ScriptedJev("yes"), clock)
    start_conversation(store, worker, clock)
    store.record_incoming_text(DANA_PHONE, "yes", "SMIN1", clock())
    assert store.claim_incoming_text(clock()) is not None  # the old worker died holding it
    restarted, restarted_sender = make_worker(store, ScriptedJev("yes"), clock, sender=sender)
    restarted.recover_after_restart()
    restarted.run_once()
    assert len(restarted_sender.sent) == 2


def test_a_quiet_conversation_is_handed_off_after_a_day(store: Store) -> None:
    clock = Clock()
    worker, _ = make_worker(store, ScriptedJev("yes"), clock)
    lead_id = start_conversation(store, worker, clock)
    reply(store, worker, clock, DANA_PHONE, "yes")
    clock.now += timedelta(hours=23, minutes=59)
    worker.run_once()
    assert store.conversation_result(lead_id) is None
    clock.now += timedelta(minutes=2)
    worker.run_once()
    assert store.conversation_result(lead_id) == "stopped_replying"
    assert store.lead_status(lead_id) == "handed_off"
