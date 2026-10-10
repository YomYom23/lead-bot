# lead-bot/tests/test_report.py
# The monthly results email: what's counted for a month (and what from the month before isn't),
# the email's wording, and when it's sent: once, on or after 8 AM on the 1st, never for a month
# before the database existed, and again after a failed send.
from __future__ import annotations

import smtplib
from datetime import date

import pytest
from conftest import HOURS, PHOENIX, make_consent, make_lead, phoenix
from test_owner_alerts import RecordingMailer
from text_fakes import SETTINGS

from lead_bot.leads import ReactivationContact
from lead_bot.report import (
    MonthlyReportSender,
    build_report_sender,
    month_bounds,
    month_label,
    previous_month,
    report_text,
)
from lead_bot.store import Store


def phone(last_two: int) -> str:
    # 555-01xx numbers are reserved for fiction; built from parts so none is typed whole.
    return "+1" + "480" + "555" + f"01{last_two:02d}"


def add_booking(store: Store, lead_id: int, booked_at_day: int, month: int = 10) -> None:
    # Test setup: a booking made by text, written as save_turn writes it.
    booked_at = phoenix(2026, month, booked_at_day, 11)
    with store._write_transaction() as connection:
        connection.execute(
            "INSERT INTO bookings (lead_id, starts_at, slot_label, booked_at) VALUES (?, ?, ?, ?)",
            (lead_id, booked_at.timestamp(), f"October {booked_at_day + 2} at 8 AM", booked_at.timestamp()),
        )


def seed_october(store: Store) -> None:
    """A month of mixed activity, plus a little in late September that mustn't count."""
    # Web form: booked after replying; no consent; texted with no reply.
    booked_lead = store.add_lead(
        make_lead(phone(1), "Ana Booked"), make_consent(), phoenix(2026, 10, 5, 10), HOURS
    )
    store.record_incoming_text(phone(1), "yes", "SM-1", phoenix(2026, 10, 5, 10, 5))
    add_booking(store, booked_lead.lead_id, 5)
    store.add_lead(make_lead(phone(2)), make_consent(given=False), phoenix(2026, 10, 6, 10), HOURS)
    quiet_lead = store.add_lead(make_lead(phone(3)), make_consent(), phoenix(2026, 10, 7, 10), HOURS)
    store.add_owner_alert(quiet_lead.lead_id, "URGENT_active_leak", phoenix(2026, 10, 7, 10, 30))

    # Calls: one booked, one message, one hang-up.
    booked_call = store.start_incoming_call("CA-1", phone(5), phoenix(2026, 10, 8, 10))
    call_lead = store.add_lead_from_call(
        booked_call.call_id, "Cal Caller", phone(5), "1 Elm St", phoenix(2026, 10, 8, 10, 5)
    )
    store.record_call_booking(
        booked_call.call_id,
        call_lead,
        phoenix(2026, 10, 9, 8),
        "Friday October 9 at 8 AM",
        phoenix(2026, 10, 8, 10, 10),
    )
    store.finish_call(booked_call.call_id, "booked", phoenix(2026, 10, 8, 10, 11), lead_status="booked")
    message_call = store.start_incoming_call("CA-2", phone(6), phoenix(2026, 10, 9, 10))
    store.finish_call(
        message_call.call_id, "message", phoenix(2026, 10, 9, 10, 2), alert="caller_left_a_message"
    )
    hung_up_call = store.start_incoming_call("CA-3", phone(7), phoenix(2026, 10, 10, 10))
    store.finish_call(hung_up_call.call_id, "hung_up", phoenix(2026, 10, 10, 10, 1), alert="caller_hung_up")

    # A reactivation contact started and replied.
    contact = ReactivationContact(
        "Ray Old", phone(8), "2 Oak St", date(2025, 3, 1), "https://example.com", "I agree"
    )
    store.add_campaign("spring", "a.csv", 50, [contact], phoenix(2026, 10, 1, 7), HOURS)
    store.start_campaign_contacts(phoenix(2026, 10, 12, 10), HOURS)
    store.record_incoming_text(phone(8), "yes", "SM-8", phoenix(2026, 10, 12, 10, 30))

    store.add_to_do_not_call(phone(9), "replied STOP", phoenix(2026, 10, 15, 9))

    # Late September in Phoenix (already October in UTC): none of this is October's.
    september_lead = store.add_lead(make_lead(phone(4)), make_consent(), phoenix(2026, 9, 30, 23, 30), HOURS)
    add_booking(store, september_lead.lead_id, 30, month=9)
    store.add_to_do_not_call(phone(10), "replied STOP", phoenix(2026, 9, 30, 23, 45))


# ---- what's counted ----


def test_a_month_is_counted_in_the_business_timezone(store: Store) -> None:
    seed_october(store)
    numbers = store.monthly_numbers(*month_bounds("2026-10", PHOENIX))

    assert (
        numbers.form_leads,
        numbers.form_leads_texted,
        numbers.form_leads_replied,
        numbers.form_leads_booked,
    ) == (
        3,
        2,
        1,
        1,
    )
    assert numbers.bookings == 2
    assert numbers.bookings_by_source == {"form": 1, "phone": 1}
    assert (numbers.calls_answered, numbers.calls_booked, numbers.calls_messages, numbers.calls_hung_up) == (
        3,
        1,
        1,
        1,
    )
    assert (numbers.reactivation_started, numbers.reactivation_replied, numbers.reactivation_booked) == (
        1,
        1,
        0,
    )
    assert numbers.urgent_leaks == 1
    # no_text_consent, the urgent leak, the message and the hang-up; not "inspection booked".
    assert numbers.alerts_for_owner == 4
    assert numbers.opted_out == 1
    assert [booking.full_name for booking in numbers.recent_bookings] == ["Cal Caller", "Ana Booked"]
    assert [booking.source for booking in numbers.recent_bookings] == ["phone", "form"]


def test_the_report_reads_as_plain_numbers(store: Store) -> None:
    seed_october(store)
    text = report_text(
        store.monthly_numbers(*month_bounds("2026-10", PHOENIX)), "Desert Ridge Roofing LLC", "2026-10"
    )
    assert text.startswith("Here's what the lead bot did for Desert Ridge Roofing LLC in October 2026.")
    assert "Inspections booked: 2 (web form 1, phone calls 1, old-lead campaigns 0)" in text
    assert "  Replied: 1 (50% of those texted)" in text
    assert "Missed calls the bot answered: 3" in text
    assert "  Cal Caller: Friday October 9 at 8 AM (from a phone call)" in text


def test_an_empty_month_says_so_without_dividing_by_zero(store: Store) -> None:
    text = report_text(
        store.monthly_numbers(*month_bounds("2026-10", PHOENIX)), "Desert Ridge Roofing LLC", "2026-10"
    )
    assert "Replied: 0 (n/a of those texted)" in text
    assert "No inspections were booked this month." in text


def test_month_arithmetic_crosses_the_year() -> None:
    start, end = month_bounds("2026-12", PHOENIX)
    assert (start, end) == (phoenix(2026, 12, 1, 0), phoenix(2027, 1, 1, 0))
    assert previous_month(phoenix(2027, 1, 1, 9), PHOENIX) == "2026-12"
    assert month_label("2026-12") == "December 2026"


# ---- when it's sent ----


def sender(store: Store, mailer: RecordingMailer, now_values: list) -> MonthlyReportSender:  # type: ignore[type-arg]
    return MonthlyReportSender(
        store, SETTINGS, mailer, "bot@example.com", "owner@example.com", clock=lambda: now_values[0]
    )


def test_it_goes_once_on_or_after_8_am_on_the_1st(store: Store) -> None:
    seed_october(store)
    mailer = RecordingMailer()
    now = [phoenix(2026, 11, 1, 7, 59)]
    report_sender = sender(store, mailer, now)

    assert report_sender.send_if_due() is None
    now[0] = phoenix(2026, 11, 1, 8, 0)
    assert report_sender.send_if_due() == "2026-10"
    now[0] = phoenix(2026, 11, 20, 9, 0)
    assert report_sender.send_if_due() is None  # already sent

    (email,) = mailer.sent
    assert email["Subject"] == "[Desert Ridge Roofing LLC] Lead bot results for October 2026"
    assert email["To"] == "owner@example.com"


def test_a_new_install_sends_no_report_for_the_month_before_it_existed(store: Store) -> None:
    store.add_lead(make_lead(phone(1)), make_consent(), phoenix(2026, 11, 3, 10), HOURS)
    mailer = RecordingMailer()
    assert sender(store, mailer, [phoenix(2026, 12, 1, 9)]).send_if_due() == "2026-11"
    assert sender(store, RecordingMailer(), [phoenix(2026, 11, 5, 9)]).send_if_due() is None
    assert len(mailer.sent) == 1


def test_an_empty_database_sends_nothing(store: Store) -> None:
    mailer = RecordingMailer()
    assert sender(store, mailer, [phoenix(2026, 11, 1, 9)]).send_if_due() is None
    assert mailer.sent == []


def test_a_failed_send_is_tried_again(store: Store) -> None:
    seed_october(store)
    mailer = RecordingMailer(OSError("network down"))
    report_sender = sender(store, mailer, [phoenix(2026, 11, 1, 9)])
    with pytest.raises(OSError):
        report_sender.send_if_due()
    assert not store.monthly_report_sent("2026-10")
    assert report_sender.send_if_due() == "2026-10"


def test_a_printed_report_is_not_recorded_so_real_email_still_sends_it(store: Store) -> None:
    seed_october(store)
    printing = build_report_sender(store, SETTINGS, None)
    printing._clock = lambda: phoenix(2026, 11, 1, 9)  # as if run on the 1st
    assert printing.send_if_due() == "2026-10"
    assert printing.send_if_due() is None  # printed once per run
    assert not store.monthly_report_sent("2026-10")


def test_a_refused_login_leaves_the_month_unsent(store: Store) -> None:
    seed_october(store)
    mailer = RecordingMailer(smtplib.SMTPAuthenticationError(535, b"no"))
    with pytest.raises(smtplib.SMTPAuthenticationError):
        sender(store, mailer, [phoenix(2026, 11, 1, 9)]).send_if_due()
    assert not store.monthly_report_sent("2026-10")
