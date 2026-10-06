# lead-bot/tests/test_owner_alerts.py
# Owner alert emails: every alert reason reads as plain words, the email carries what the owner
# needs, each alert is emailed once, failures are retried and never dropped, and SMTP is used
# correctly. No email leaves this machine.
from __future__ import annotations

import re
import smtplib
import sqlite3
import ssl
import threading
from collections.abc import Callable
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from conftest import DANA_PHONE, MONDAY_10AM
from text_fakes import SETTINGS, Clock, ScriptedJev, make_worker, reply, start_conversation

from lead_bot.leads import pretty_phone
from lead_bot.owner_alerts import (
    ALERT_MEANINGS,
    RETRY_GAPS,
    EmailSettings,
    OwnerAlertNotifier,
    SmtpMailer,
    alert_meaning,
    build_notifier,
    send_test_email,
)
from lead_bot.store import Store

SOURCE_FOLDER = Path(__file__).parent.parent / "src" / "lead_bot"


class RecordingMailer:
    """Keeps each email. `failures` are raised, one per send, before sending starts working."""

    def __init__(self, *failures: Exception) -> None:
        self.sent: list[EmailMessage] = []
        self.failures = list(failures)

    def send(self, message: EmailMessage) -> None:
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(message)


def notifier(store: Store, mailer: RecordingMailer, clock: Clock) -> OwnerAlertNotifier:
    return OwnerAlertNotifier(store, SETTINGS, mailer, "bot@example.com", "owner@example.com", clock=clock)


def urgent_leak_conversation(store: Store) -> Clock:
    """A homeowner who says water is coming in now, which raises URGENT_active_leak."""
    clock = Clock()
    worker, _ = make_worker(store, ScriptedJev("yes", "yes"), clock)
    start_conversation(store, worker, clock)
    reply(store, worker, clock, DANA_PHONE, "yes")
    reply(store, worker, clock, DANA_PHONE, "yes, it's dripping in the kitchen")
    return clock


# ---- every alert reads as plain words ----


def alert_reasons_in_the_source() -> set[str]:
    """Every fixed alert reason the code can raise, found by reading the source."""
    reasons: set[str] = set()
    patterns = [
        r'alert="([A-Za-z_]+)"',
        r'owner_alerts\.append\("([A-Za-z_]+)"\)',
        r'_add_owner_alert\(connection, [^,]+, "([A-Za-z_]+)"',
        r'reason = "([A-Za-z_]+)"',
        r'reason = "[A-Za-z_]+" if [^\n]* else "([A-Za-z_]+)"',
    ]
    for path in SOURCE_FOLDER.glob("*.py"):
        if path.name == "owner_alerts.py":
            continue
        source = path.read_text(encoding="utf-8")
        for pattern in patterns:
            reasons.update(re.findall(pattern, source))
    # Intake decisions that become alerts (store.add_lead).
    reasons.update({"previously_opted_out", "no_text_consent"})
    return reasons


def test_every_alert_the_code_raises_has_plain_words() -> None:
    reasons = alert_reasons_in_the_source()
    assert len(reasons) >= 20  # the scan really found them
    assert sorted(reasons - set(ALERT_MEANINGS)) == []


def test_alerts_with_a_detail_after_the_colon_read_well() -> None:
    question = alert_meaning("homeowner_question_for_the_team:not_in_facts")
    assert "couldn't answer" in question.headline
    assert "not in facts" in question.action
    undelivered = alert_meaning("text_not_delivered:30032")
    assert "30032" in undelivered.headline
    assert "isn't verified" in undelivered.action


def test_only_the_active_leak_is_urgent() -> None:
    assert [reason for reason, meaning in ALERT_MEANINGS.items() if meaning.urgent] == ["URGENT_active_leak"]


# ---- the email ----


def test_an_urgent_leak_email_says_urgent_and_carries_the_lead_and_conversation(store: Store) -> None:
    clock = urgent_leak_conversation(store)
    mailer = RecordingMailer()

    assert notifier(store, mailer, clock).notify_pending() == 1

    (email,) = mailer.sent
    assert email["Subject"].startswith("[Desert Ridge Roofing LLC] URGENT: Water is coming into the house")
    assert "Dana Ruiz" in email["Subject"]
    assert email["Importance"] == "high"
    assert (email["From"], email["To"]) == ("bot@example.com", "owner@example.com")
    body = email.get_content()
    assert body.startswith("URGENT: Water is coming into the house right now.")
    assert f"Phone: {pretty_phone(DANA_PHONE)}" in body
    assert "Address: 12 Saguaro Ln, Mesa AZ" in body
    assert "Leaking now: yes" in body
    assert "Their text:\n  yes, it's dripping in the kitchen" in body
    assert "Homeowner: yes, it's dripping in the kitchen" in body
    assert "Bot: Hi Dana" in body


def test_a_text_from_a_stranger_shows_their_number_and_text(store: Store) -> None:
    clock = Clock()
    worker, _ = make_worker(store, ScriptedJev(), clock)
    reply(store, worker, clock, "+15550001111", "who is this?")
    mailer = RecordingMailer()

    notifier(store, mailer, clock).notify_pending()

    body = mailer.sent[0].get_content()
    assert "Someone the bot never texted sent a text" in body
    assert "From: (555) 000-1111" in body
    assert "Their text:\n  who is this?" in body


def test_a_text_the_homeowner_never_got_is_marked_in_the_conversation(store: Store) -> None:
    clock = Clock()
    worker, sender = make_worker(store, ScriptedJev(), clock)
    lead_id = start_conversation(store, worker, clock)
    store.record_delivery_report(sender.message_ids[0], "undelivered", "30032", clock())
    mailer = RecordingMailer()

    notifier(store, mailer, clock).notify_pending()

    assert store.transcript(lead_id)[0].status == "undelivered"
    body = mailer.sent[0].get_content()
    assert "Twilio error 30032" in mailer.sent[0]["Subject"]
    assert "Bot [undelivered]: Hi Dana" in body


# ---- each alert once, failures retried ----


def test_each_alert_is_emailed_once(store: Store) -> None:
    clock = urgent_leak_conversation(store)
    mailer = RecordingMailer()
    alert_notifier = notifier(store, mailer, clock)
    assert alert_notifier.notify_pending() == 1
    assert alert_notifier.notify_pending() == 0
    assert len(mailer.sent) == 1


def test_a_failed_email_waits_then_is_retried_and_never_dropped(store: Store) -> None:
    clock = urgent_leak_conversation(store)
    mailer = RecordingMailer(*[OSError("network down")] * (len(RETRY_GAPS) + 1))
    alert_notifier = notifier(store, mailer, clock)

    # One failure per retry gap, then once more at the last gap: it never gives up.
    for gap in [*RETRY_GAPS, RETRY_GAPS[-1]]:
        assert alert_notifier.notify_pending() == 0
        clock.now += gap - timedelta(seconds=1)
        assert store.alerts_to_email(clock()) == []  # not before its retry time
        clock.now += timedelta(seconds=1)
        assert len(store.alerts_to_email(clock())) == 1  # and still there afterwards
    assert alert_notifier.notify_pending() == 1
    assert len(mailer.sent) == 1


def test_a_refused_login_stops_the_pass_and_keeps_every_alert(store: Store) -> None:
    clock = urgent_leak_conversation(store)  # raises one alert
    worker, _ = make_worker(store, ScriptedJev(), clock)
    reply(store, worker, clock, "+15550001111", "who is this?")  # and a second
    mailer = RecordingMailer(smtplib.SMTPAuthenticationError(535, b"Username and Password not accepted"))

    assert notifier(store, mailer, clock).notify_pending() == 0

    assert mailer.sent == []
    clock.now += RETRY_GAPS[0]
    assert len(store.alerts_to_email(clock())) == 2


def test_the_notifier_thread_survives_a_crash_in_one_pass(store: Store) -> None:
    class Crashes(OwnerAlertNotifier):
        passes = 0

        def notify_pending(self) -> int:
            Crashes.passes += 1
            if Crashes.passes == 1:
                raise RuntimeError("bug")
            stop.set()
            return 0

    stop = threading.Event()
    crashing = Crashes(store, SETTINGS, RecordingMailer(), "a", "b")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("lead_bot.owner_alerts.CHECK_EVERY_SECONDS", 0.01)
        crashing.run_forever(stop)
    assert Crashes.passes == 2


def test_alerts_made_before_alert_emails_existed_are_still_emailed(tmp_path: Path) -> None:
    # A database from before this feature: owner_alerts without text_id, no alert_emails table.
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        "CREATE TABLE owner_alerts (alert_id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER, "
        "reason TEXT NOT NULL, created_at REAL NOT NULL);"
        f"INSERT INTO owner_alerts (lead_id, reason, created_at) VALUES (NULL, 'jev_unavailable', "
        f"{MONDAY_10AM.timestamp()});"
    )
    connection.close()
    store = Store(path)
    mailer = RecordingMailer()
    assert notifier(store, mailer, Clock()).notify_pending() == 1


# ---- settings and SMTP ----


def test_no_email_values_means_alerts_are_printed(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OWNER_ALERT_EMAIL", "SMTP_USERNAME", "SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    assert EmailSettings.from_environment() is None


def test_a_half_filled_env_names_every_missing_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_ALERT_EMAIL", "owner@example.com")
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="SMTP_USERNAME, SMTP_PASSWORD"):
        EmailSettings.from_environment()


def test_the_spaces_google_shows_in_an_app_password_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OWNER_ALERT_EMAIL", "owner@example.com")
    monkeypatch.setenv("SMTP_USERNAME", "bot@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_PORT", raising=False)
    settings = EmailSettings.from_environment()
    assert settings is not None
    assert (settings.password, settings.host, settings.port) == ("abcdefghijklmnop", "smtp.gmail.com", 587)


class FakeSmtp:
    """Stands in for smtplib.SMTP and records the order of calls."""

    instances: list[FakeSmtp] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.calls: list[tuple[str, Any]] = [("connect", (host, port, timeout))]
        FakeSmtp.instances.append(self)

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.calls.append(("quit", None))

    def starttls(self, *, context: ssl.SSLContext) -> None:
        self.calls.append(("starttls", context.verify_mode))

    def ehlo(self) -> None:
        self.calls.append(("ehlo", None))

    def auth(self, mechanism: str, authobject: Callable[..., str]) -> None:
        self.calls.append(("auth", (mechanism, authobject())))

    def send_message(self, msg: EmailMessage) -> None:
        self.calls.append(("send", msg["Subject"]))


def test_smtp_encrypts_before_logging_in_and_uses_one_login_method() -> None:
    FakeSmtp.instances.clear()
    email = EmailSettings("owner@example.com", "bot@example.com", "app-password")
    send_test_email(email, SETTINGS, SmtpMailer(email, smtp_factory=FakeSmtp))
    (smtp,) = FakeSmtp.instances
    assert [name for name, _ in smtp.calls] == ["connect", "starttls", "ehlo", "auth", "send", "quit"]
    assert smtp.calls[0][1][:2] == ("smtp.gmail.com", 587)
    assert smtp.calls[1][1] == ssl.CERT_REQUIRED  # the server's certificate is checked
    assert smtp.calls[3][1] == ("PLAIN", "\0bot@example.com\0app-password")


def test_a_wrong_password_is_a_clear_refusal_not_a_dropped_connection() -> None:
    # Against real Gmail, smtplib.login() hid a 535 behind "connection unexpectedly closed".
    class RefusingSmtp(FakeSmtp):
        def auth(self, mechanism: str, authobject: Callable[..., str]) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")

    email = EmailSettings("owner@example.com", "bot@example.com", "wrong")
    with pytest.raises(smtplib.SMTPAuthenticationError):
        send_test_email(email, SETTINGS, SmtpMailer(email, smtp_factory=RefusingSmtp))


# ---- the worker won't text for real without alert email ----


def test_printed_alerts_are_not_recorded_so_real_email_still_sends_them(store: Store) -> None:
    clock = urgent_leak_conversation(store)
    printing = build_notifier(store, SETTINGS, None)
    assert printing.notify_pending() == 1
    assert printing.notify_pending() == 0  # printed once per run
    # Email set up later: the alert still goes.
    mailer = RecordingMailer()
    assert notifier(store, mailer, clock).notify_pending() == 1


def test_real_texting_needs_alert_email(monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    from lead_bot.worker import _build_alert_notifier

    for name in ("OWNER_ALERT_EMAIL", "SMTP_USERNAME", "SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="--send needs owner alert email"):
        _build_alert_notifier(True, store, SETTINGS)
    # The dry run is fine without it: alerts are printed.
    assert _build_alert_notifier(False, store, SETTINGS) is not None
