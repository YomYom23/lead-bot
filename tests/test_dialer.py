# lead-bot/tests/test_dialer.py
# The dialer's last-moment checks, and what happens when a call can't be placed.
from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

from conftest import DANA_PHONE, HOURS, MONDAY_10AM, make_consent, make_lead, phoenix

from lead_bot.dialer import close_interrupted_calls, dial_next_due_call, run_dialer
from lead_bot.store import DueCall, Store


class RecordingPlacer:
    """Stands in for the phone provider: remembers what it was asked to dial."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.dialed: list[str] = []
        self.fail_with = fail_with

    def place_call(self, call: DueCall) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        self.dialed.append(call.phone)
        return f"CA-test-{call.call_id}"


def test_a_due_call_is_dialed_and_marked_placed(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    placer = RecordingPlacer()
    assert dial_next_due_call(store, placer, HOURS, lambda: MONDAY_10AM)
    assert placer.dialed == [DANA_PHONE]
    assert store.counts()["calls"] == {"placed": 1}


def test_nothing_due_means_nothing_dialed(store: Store) -> None:
    placer = RecordingPlacer()
    assert not dial_next_due_call(store, placer, HOURS, lambda: MONDAY_10AM)
    assert placer.dialed == []


def test_an_opt_out_after_scheduling_is_honored_at_dial_time(store: Store, tmp_path: Path) -> None:
    # Put the number on the list directly, the way a second path (an owner tool, a future
    # import) might, without the cancellation add_to_do_not_call performs.
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    connection = sqlite3.connect(tmp_path / "leads.db")
    connection.execute(
        "INSERT INTO do_not_call (phone, added_at, source) VALUES (?, ?, 'test')",
        (DANA_PHONE, MONDAY_10AM.timestamp()),
    )
    connection.commit()
    connection.close()
    placer = RecordingPlacer()
    dial_next_due_call(store, placer, HOURS, lambda: MONDAY_10AM)
    assert placer.dialed == []
    assert store.counts()["calls"] == {"cancelled": 1}


def test_a_lead_without_consent_on_record_is_never_dialed(store: Store, tmp_path: Path) -> None:
    # Damage the data: the consent row says no, but a call is scheduled anyway.
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    connection = sqlite3.connect(tmp_path / "leads.db")
    connection.execute("UPDATE consents SET given = 0 WHERE lead_id = ?", (added.lead_id,))
    connection.commit()
    connection.close()
    placer = RecordingPlacer()
    dial_next_due_call(store, placer, HOURS, lambda: MONDAY_10AM)
    assert placer.dialed == []
    assert store.lead_status(added.lead_id) == "owner_alerted"
    assert [alert.reason for alert in store.owner_alerts()] == ["no call consent on record"]


def test_a_lead_with_no_consent_row_at_all_is_never_dialed(store: Store, tmp_path: Path) -> None:
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    connection = sqlite3.connect(tmp_path / "leads.db")
    connection.execute("DELETE FROM consents WHERE lead_id = ?", (added.lead_id,))
    connection.commit()
    connection.close()
    placer = RecordingPlacer()
    assert dial_next_due_call(store, placer, HOURS, lambda: MONDAY_10AM)  # claimed, not skipped
    assert placer.dialed == []
    assert store.counts()["calls"] == {"cancelled": 1}


def test_a_call_due_inside_hours_but_reached_after_them_waits(store: Store) -> None:
    # Scheduled at 8:20 p.m.; the dialer only gets to it at 8:40 p.m. (it was down).
    store.add_lead(make_lead(), make_consent(), phoenix(2026, 10, 5, 20, 20), HOURS)
    placer = RecordingPlacer()
    dial_next_due_call(store, placer, HOURS, lambda: phoenix(2026, 10, 5, 20, 40))
    assert placer.dialed == []
    assert store.counts()["calls"] == {"scheduled": 1}
    assert store.claim_due_call(phoenix(2026, 10, 6, 7, 59)) is None
    assert store.claim_due_call(phoenix(2026, 10, 6, 8, 0)) is not None


def test_a_refused_call_counts_as_an_attempt_and_the_next_is_scheduled(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    placer = RecordingPlacer(fail_with=RuntimeError("provider said no"))
    dial_next_due_call(store, placer, HOURS, lambda: MONDAY_10AM)
    assert store.counts()["calls"] == {"completed": 1, "scheduled": 1}


def test_a_dialer_restart_never_redials_an_interrupted_call_at_once(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    assert store.claim_due_call(MONDAY_10AM) is not None  # claimed, then the process "dies"
    restart_time = MONDAY_10AM + timedelta(minutes=1)
    assert close_interrupted_calls(store, HOURS, lambda: restart_time) == 1
    placer = RecordingPlacer()
    dial_next_due_call(store, placer, HOURS, lambda: restart_time)
    assert placer.dialed == []  # the next attempt is the scheduled follow-up, not an instant redial
    assert store.counts()["calls"] == {"completed": 1, "scheduled": 1}


def test_run_dialer_sleeps_only_when_idle(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    store.add_lead(make_lead("+14805550199", "Sam Lee"), make_consent(), MONDAY_10AM, HOURS)
    placer = RecordingPlacer()
    sleeps: list[float] = []
    loops = iter(range(3))
    run_dialer(
        store,
        placer,
        HOURS,
        clock=lambda: MONDAY_10AM,
        should_stop=lambda: next(loops, None) is None,
        sleep=sleeps.append,
    )
    assert placer.dialed == [DANA_PHONE, "+14805550199"]
    assert len(sleeps) == 1  # two calls back to back, then one idle sleep
