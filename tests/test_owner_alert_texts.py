# lead-bot/tests/test_owner_alert_texts.py
# Alert texts to the owner's phone: only the alerts that need action today, only while fresh,
# each once, never resent when it may have gone, and the email still goes whatever happens to
# the text. Also: the owner replying to an alert text doesn't raise an alert about themselves.
from __future__ import annotations

import dataclasses
from datetime import timedelta

import pytest
from conftest import DANA_PHONE, OWNER_PHONE
from test_conversation import QUALIFYING_ANSWERS, run
from test_owner_alerts import RecordingMailer, urgent_leak_conversation
from text_fakes import (
    FIRST_OFFERED_TIME,
    SETTINGS,
    Clock,
    RecordingSender,
    ScriptedJev,
    make_services,
    reply,
)

from lead_bot.leads import pretty_phone
from lead_bot.owner_alerts import (
    MAX_ALERT_TEXT_CHARACTERS,
    RETRY_GAPS,
    TEXTED_ALERT_REASONS,
    AlertTexter,
    OwnerAlertNotifier,
    build_alert_text,
)
from lead_bot.settings import _optional_phone
from lead_bot.store import AlertForText, Store
from lead_bot.text_script import is_gsm7
from lead_bot.worker import SenderCredentialsRejected, Worker

SETTINGS_WITH_OWNER_PHONE = dataclasses.replace(SETTINGS, owner_alert_phone=OWNER_PHONE)


def owner_aware_worker(store: Store, clock: Clock) -> Worker:
    """A worker whose settings know the owner's phone, as with OWNER_ALERT_PHONE in .env."""
    services = make_services(ScriptedJev())
    return Worker(store, SETTINGS_WITH_OWNER_PHONE, services, RecordingSender(), clock=clock)


def texting_notifier(
    store: Store, clock: Clock, texts: RecordingSender, mailer: RecordingMailer | None = None
) -> OwnerAlertNotifier:
    return OwnerAlertNotifier(
        store,
        SETTINGS_WITH_OWNER_PHONE,
        mailer if mailer is not None else RecordingMailer(),
        "bot@example.com",
        "owner@example.com",
        clock=clock,
        texter=AlertTexter(texts, OWNER_PHONE),
    )


# ---- which alerts, and what the text says ----


def test_only_the_alerts_that_need_action_today_are_texted() -> None:
    assert sorted(TEXTED_ALERT_REASONS) == [
        "URGENT_active_leak",
        "asked_for_a_person",
        "calendar_unavailable",
        "google_sign_in_expired",
        "inspection_booked",
        "wants_to_change_booking",
    ]


def test_an_urgent_leak_is_texted_to_the_owner_and_emailed(store: Store) -> None:
    clock = urgent_leak_conversation(store)
    texts, mailer = RecordingSender(), RecordingMailer()

    assert texting_notifier(store, clock, texts, mailer).notify_pending() == 2  # one email, one text

    assert len(mailer.sent) == 1
    ((phone, body),) = texts.sent
    assert phone == OWNER_PHONE
    assert body.startswith("Lead bot: URGENT: Water is coming into the house right now.")
    assert f"Dana Ruiz, {pretty_phone(DANA_PHONE)}, 12 Saguaro Ln, Mesa AZ." in body
    assert "Call them as soon as you can." in body
    assert is_gsm7(body) and len(body) <= MAX_ALERT_TEXT_CHARACTERS


def test_a_booking_is_texted_with_its_time(store: Store) -> None:
    jev = ScriptedJev(*QUALIFYING_ANSWERS, FIRST_OFFERED_TIME)
    _, _, clock, lead_id = run(store, jev, ["yes", "no", "no", "yes", "the first one"])
    texts = RecordingSender()

    texting_notifier(store, clock, texts).notify_pending()

    ((_, body),) = texts.sent
    assert body.startswith("Lead bot: A homeowner booked an inspection.")
    assert f"Inspection: {store.booking_label(lead_id)}." in body


def test_an_alert_that_can_wait_is_only_emailed(store: Store) -> None:
    clock = Clock()
    worker = owner_aware_worker(store, clock)
    reply(store, worker, clock, "+1" + "555" + "000" + "1111", "who is this?")
    texts, mailer = RecordingSender(), RecordingMailer()

    texting_notifier(store, clock, texts, mailer).notify_pending()

    assert len(mailer.sent) == 1
    assert texts.sent == []


def test_a_long_alert_text_points_to_the_email_instead_of_overflowing() -> None:
    alert = AlertForText(
        alert_id=1,
        reason="URGENT_active_leak",
        tries=0,
        full_name="Dana Ruiz",
        phone=DANA_PHONE,
        address="Unit 4, " + "very long street name " * 8 + "Mesa AZ",
        booking=None,
    )
    body = build_alert_text(alert)
    assert len(body) <= MAX_ALERT_TEXT_CHARACTERS
    assert body.endswith("Details are in your email.")


# ---- once each, only while fresh ----


def test_each_alert_is_texted_once(store: Store) -> None:
    clock = urgent_leak_conversation(store)
    texts = RecordingSender()
    alert_notifier = texting_notifier(store, clock, texts)
    alert_notifier.notify_pending()
    alert_notifier.notify_pending()
    assert len(texts.sent) == 1


def test_an_alert_older_than_the_limit_is_emailed_but_not_texted(store: Store) -> None:
    # E.g. the first start after OWNER_ALERT_PHONE is added to a database full of old alerts.
    # Fixed times, not ALERT_TEXT_MAX_AGE: a test that moved with the constant would pass
    # whatever the limit was changed to.
    clock = urgent_leak_conversation(store)
    clock.now += timedelta(hours=3)
    texts, mailer = RecordingSender(), RecordingMailer()

    texting_notifier(store, clock, texts, mailer).notify_pending()

    assert len(mailer.sent) == 1
    assert texts.sent == []


def test_an_alert_an_hour_old_is_still_texted(store: Store) -> None:
    clock = urgent_leak_conversation(store)
    clock.now += timedelta(hours=1)
    texts = RecordingSender()
    texting_notifier(store, clock, texts).notify_pending()
    assert len(texts.sent) == 1


# ---- failures ----


def test_a_refused_text_is_not_retried_and_the_email_still_goes(store: Store) -> None:
    clock = urgent_leak_conversation(store)
    texts, mailer = RecordingSender(), RecordingMailer()
    texts.refuse = "Twilio error 21211: invalid To number"
    alert_notifier = texting_notifier(store, clock, texts, mailer)

    alert_notifier.notify_pending()
    texts.refuse = None
    clock.now += RETRY_GAPS[-1]
    alert_notifier.notify_pending()

    assert texts.sent == []
    assert len(mailer.sent) == 1


def test_a_text_that_may_have_gone_is_never_sent_again(store: Store) -> None:
    clock = urgent_leak_conversation(store)
    texts = RecordingSender()
    texts.fail = TimeoutError("read timed out")
    alert_notifier = texting_notifier(store, clock, texts)

    alert_notifier.notify_pending()
    texts.fail = None
    clock.now += RETRY_GAPS[-1]
    alert_notifier.notify_pending()

    assert texts.sent == []


def test_refused_credentials_wait_then_the_text_goes(store: Store) -> None:
    clock = urgent_leak_conversation(store)
    texts = RecordingSender()
    texts.fail = SenderCredentialsRejected("Twilio error 20003")
    alert_notifier = texting_notifier(store, clock, texts)

    alert_notifier.notify_pending()
    texts.fail = None
    clock.now += RETRY_GAPS[0] - timedelta(seconds=1)
    alert_notifier.notify_pending()
    assert texts.sent == []  # not before its retry time
    clock.now += timedelta(seconds=1)
    alert_notifier.notify_pending()
    assert len(texts.sent) == 1


def test_printed_alert_texts_are_not_recorded_so_real_texts_still_go(store: Store) -> None:
    clock = urgent_leak_conversation(store)
    printed = RecordingSender()
    # As build_notifier makes it without email settings, but on the test's clock: the real one
    # is days past the test alert, so the age limit would skip it.
    printing = OwnerAlertNotifier(
        store,
        SETTINGS_WITH_OWNER_PHONE,
        RecordingMailer(),
        "lead-bot",
        "the owner",
        clock=clock,
        record_sent=False,
        texter=AlertTexter(printed, OWNER_PHONE),
    )
    printing.notify_pending()
    printing.notify_pending()
    assert len(printed.sent) == 1  # once per run
    # Real texting set up later, within the age limit: the alert still goes.
    real = RecordingSender()
    texting_notifier(store, clock, real).notify_pending()
    assert len(real.sent) == 1


# ---- the owner's replies ----


def test_the_owner_replying_to_an_alert_text_raises_no_alert(store: Store) -> None:
    clock = Clock()
    worker = owner_aware_worker(store, clock)

    reply(store, worker, clock, OWNER_PHONE, "ok, calling her now")

    assert store.owner_alerts() == []


# ---- settings ----


def test_owner_alert_phone_is_optional_but_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OWNER_ALERT_PHONE", raising=False)
    assert _optional_phone("OWNER_ALERT_PHONE") is None
    monkeypatch.setenv("OWNER_ALERT_PHONE", pretty_phone(OWNER_PHONE))
    assert _optional_phone("OWNER_ALERT_PHONE") == OWNER_PHONE
    monkeypatch.setenv("OWNER_ALERT_PHONE", "call me")
    with pytest.raises(RuntimeError, match="OWNER_ALERT_PHONE must be a 10-digit US mobile number"):
        _optional_phone("OWNER_ALERT_PHONE")


def test_real_alert_texts_need_twilio_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from lead_bot.worker import _build_alert_sender

    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="OWNER_ALERT_PHONE with --send needs"):
        _build_alert_sender(True)
