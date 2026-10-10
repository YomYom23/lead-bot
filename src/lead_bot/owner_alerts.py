# lead-bot/src/lead_bot/owner_alerts.py
"""Tells the business owner about every owner alert the bot raises: by email, and by text for
the ones that need action today.

The bot raises an alert whenever a person has to act: an active leak, a homeowner who asked for
a person, a question the bot couldn't answer, a text that wasn't delivered. Until now those were
rows in the database that nobody saw, while the homeowner had been told "I'll flag this for our
team". This module makes that true.

    uv run python -m lead_bot.owner_alerts --test   # send one test email to check the settings

How it works: the worker (worker.py) runs OwnerAlertNotifier on its own thread, so a slow mail
server never delays a reply to a homeowner. Each pass emails the alerts not emailed yet, one
email per alert, and records each one sent (store.alert_emails). A failed email is retried
later with growing gaps and never dropped. A crash between sending and recording means that
email goes twice, which is better than an urgent alert lost.

Texts to the owner (store.alert_texts) are an extra, not a replacement: only the alerts marked
text_owner below are texted, only within ALERT_TEXT_MAX_AGE of being raised, and a text that
was refused or may have gone is never retried. The email carries the alert either way. Roofers
are on roofs, not in their inbox, so a leak or a homeowner asking for a call reaches them as a
text.

Settings (.env, all three or none):
    OWNER_ALERT_EMAIL   where alerts go
    SMTP_USERNAME       the account that sends them, e.g. a Gmail address
    SMTP_PASSWORD       for Gmail, an app password (needs 2-Step Verification), not the login
    SMTP_HOST, SMTP_PORT  optional: smtp.gmail.com and 587 (STARTTLS) by default
    OWNER_ALERT_PHONE   optional: the owner's mobile, for alerts that need action today
"""

from __future__ import annotations

import argparse
import logging
import os
import smtplib
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from types import TracebackType
from typing import Protocol

from dotenv import load_dotenv

from .leads import PROBLEM_LABELS, pretty_phone
from .settings import BusinessSettings, database_path_from_environment
from .store import AlertForEmail, AlertForText, Store
from .text_script import plain_punctuation
from .timing import clock_label, utc_now
from .worker import Sender, SenderCredentialsRejected, SendRefused

logger = logging.getLogger(__name__)

EMAIL_VARIABLES = ("OWNER_ALERT_EMAIL", "SMTP_USERNAME", "SMTP_PASSWORD")
DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 587  # STARTTLS
SMTP_TIMEOUT_SECONDS = 20.0
# How often the notifier looks for new alerts. An urgent leak is emailed within this long.
CHECK_EVERY_SECONDS = 15.0
# The gap before trying a failed email again, by how many times it has failed. After the last,
# it keeps trying hourly: an alert is never dropped.
RETRY_GAPS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=1))
# An alert older than this is not texted, only emailed. A text about a leak from yesterday helps
# nobody, and without a limit the first start with OWNER_ALERT_PHONE set would text the owner
# every old urgent alert in the database at once.
ALERT_TEXT_MAX_AGE = timedelta(hours=2)
# Two GSM-7 segments. A longer alert text drops "what to do" and points to the email instead.
MAX_ALERT_TEXT_CHARACTERS = 306


# ---- what each alert means ----


@dataclass(frozen=True)
class AlertMeaning:
    headline: str  # what happened, for the subject line and the first line
    action: str  # what the owner should do
    urgent: bool = False
    # Also texted to the owner's phone (OWNER_ALERT_PHONE), not just emailed. Only for alerts
    # that need action today, so the owner doesn't learn to ignore the texts.
    text_owner: bool = False


# Every alert reason the bot raises, in plain words. tests/test_owner_alerts.py checks that each
# reason in the source code has an entry here, so a new alert can't go out as a raw code.
ALERT_MEANINGS: dict[str, AlertMeaning] = {
    "URGENT_active_leak": AlertMeaning(
        "Water is coming into the house right now",
        "Call them as soon as you can. The bot told them it flagged this as urgent.",
        urgent=True,
        text_owner=True,
    ),
    "address_needs_correcting": AlertMeaning(
        "The homeowner says the address on the form is wrong",
        "Get the right address before the inspection. The bot carried on with the other questions.",
    ),
    "asked_for_a_person": AlertMeaning(
        "The homeowner asked to talk to a person",
        "Contact them. The bot has stopped texting them.",
        text_owner=True,
    ),
    "abusive_texts": AlertMeaning(
        "The homeowner sent abusive texts",
        "Decide whether to contact them. The bot didn't reply and has stopped texting them.",
    ),
    "not_the_homeowner": AlertMeaning(
        "The person isn't the homeowner",
        "The inspection needs the owner's OK. Contact them to find out who that is.",
    ),
    "wrong_person_texted": AlertMeaning(
        "Wrong number: the person never asked for an inspection",
        "Nothing to do. The number is on the do-not-text list. Check the form if this repeats.",
    ),
    "bot_could_not_understand": AlertMeaning(
        "The bot couldn't understand the homeowner twice in a row",
        "Read the conversation below and contact them. The bot has stopped texting them.",
    ),
    "jev_unavailable": AlertMeaning(
        "The bot's language service was down, so it couldn't answer",
        "Contact the homeowner. The bot told them someone would follow up.",
    ),
    "needs_a_different_inspection_time": AlertMeaning(
        "None of the offered inspection times suited the homeowner",
        "Contact them to find a time that works.",
    ),
    "needs_an_inspection_time": AlertMeaning(
        "The offered inspection times kept being taken before they could pick one",
        "Contact them to book a time by hand.",
    ),
    "no_open_inspection_times": AlertMeaning(
        "There were no open inspection times to offer", "Free up some times and contact the homeowner."
    ),
    "wants_to_change_booking": AlertMeaning(
        "The homeowner wants to change their booked inspection", "Contact them to move it.", text_owner=True
    ),
    "inspection_booked": AlertMeaning(
        "A homeowner booked an inspection",
        "Nothing to do: it's on the calendar. The details are below.",
        text_owner=True,
    ),
    "google_sign_in_expired": AlertMeaning(
        "The bot couldn't book: its Google Calendar sign-in has expired",
        "Run `uv run python -m lead_bot.google_login` again, then contact this homeowner to book.",
        text_owner=True,
    ),
    "calendar_unavailable": AlertMeaning(
        "The bot couldn't reach Google Calendar to book",
        "Contact the homeowner to book the inspection by hand.",
        text_owner=True,
    ),
    "opted_out_after_booking": AlertMeaning(
        "The homeowner opted out of texts after booking",
        "The inspection still stands. Don't text them; call if anything changes.",
    ),
    "no_text_consent": AlertMeaning(
        "A lead came in without consent to texts", "Contact them yourself. The bot won't text them."
    ),
    "previously_opted_out": AlertMeaning(
        "A lead came in from a number that opted out before",
        "Contact them yourself if appropriate. The bot won't text them.",
    ),
    "no_reply_to_any_text": AlertMeaning(
        "The homeowner never replied to the first text or any reminder",
        "Try calling them. The bot will still answer if they reply later.",
    ),
    "stopped_replying": AlertMeaning(
        "The homeowner stopped replying partway through", "Contact them to finish booking."
    ),
    "bot_could_not_answer_a_text": AlertMeaning(
        "A text from the homeowner failed every try to answer it", "Read it below and reply to them yourself."
    ),
    "text_failed_to_send": AlertMeaning(
        "Twilio refused a text to the homeowner", "Contact them another way. The text below never went."
    ),
    "text_may_not_have_sent": AlertMeaning(
        "A text may or may not have reached the homeowner",
        "Check the message log in Twilio. The bot won't resend it, so it can't arrive twice.",
    ),
    "text_from_a_number_without_a_conversation": AlertMeaning(
        "Someone the bot never texted sent a text", "Read it below and reply if it needs one."
    ),
    "text_from_an_opted_out_number": AlertMeaning(
        "A number that opted out texted again",
        "Read it below. The bot won't reply; you may, if they're asking for something.",
    ),
    "homeowner_texted_after_handoff": AlertMeaning(
        "The homeowner texted after the bot handed them to you", "Read it below and reply to them."
    ),
    "caller_left_a_message": AlertMeaning(
        "A caller left a message with the phone assistant",
        "Call them back. Their message is below.",
        text_owner=True,
    ),
    "caller_hung_up": AlertMeaning(
        "A caller hung up before booking",
        "Call them back: they rang the business and nobody could answer. The call is below.",
    ),
    "abusive_caller": AlertMeaning(
        "A caller was abusive, so the phone assistant ended the call",
        "Decide whether to contact them. The call is below.",
    ),
}

QUESTION_FOR_TEAM_PREFIX = "homeowner_question_for_the_team:"
NOT_DELIVERED_PREFIX = "text_not_delivered:"

# The alert reasons texted to the owner as well as emailed.
TEXTED_ALERT_REASONS = tuple(reason for reason, meaning in ALERT_MEANINGS.items() if meaning.text_owner)


def alert_meaning(reason: str) -> AlertMeaning:
    """The plain-words meaning of an alert reason. Unknown reasons still read sensibly."""
    if reason in ALERT_MEANINGS:
        return ALERT_MEANINGS[reason]
    if reason.startswith(QUESTION_FOR_TEAM_PREFIX):
        why = reason.removeprefix(QUESTION_FOR_TEAM_PREFIX).replace("_", " ")
        return AlertMeaning(
            "The homeowner asked a question the bot couldn't answer",
            f"Reply to them: the bot told them it passed the question to the team. (Why: {why}.)",
        )
    if reason.startswith(NOT_DELIVERED_PREFIX):
        code = reason.removeprefix(NOT_DELIVERED_PREFIX)
        hint = " 30032 means the texting number isn't verified yet." if code == "30032" else ""
        return AlertMeaning(
            f"A text to the homeowner wasn't delivered (Twilio error {code})",
            f"Contact them another way.{hint}",
        )
    return AlertMeaning(reason.replace("_", " ").capitalize(), "Read the details below.")


# ---- the email ----


ANSWER_LABELS = {
    "address_confirmed": "Address right",
    "active_leak": "Leaking now",
    "insurance_claim": "Filing an insurance claim",
    "homeowner": "Owns the home",
}


def build_alert_email(
    alert: AlertForEmail, settings: BusinessSettings, from_address: str, to_address: str
) -> EmailMessage:
    meaning = alert_meaning(alert.reason)
    lead = alert.lead
    caller = alert.text_phone or alert.call_phone
    who = f"{lead.full_name}, {lead.address}" if lead is not None else (caller or "unknown number")
    subject = f"[{settings.business_name}] {'URGENT: ' if meaning.urgent else ''}{meaning.headline} - {who}"
    raised_at = alert.created_at.astimezone(settings.timezone)

    lines = [
        ("URGENT: " if meaning.urgent else "") + meaning.headline + ".",
        f"What to do: {meaning.action}",
        "",
        f"Raised: {raised_at:%a %b} {raised_at.day}, {clock_label(raised_at, settings.timezone)}",
    ]
    if lead is not None:
        # The leads table stores the form's problem as text, so it's looked up as plain text.
        problem_labels: dict[str, str] = {str(problem): label for problem, label in PROBLEM_LABELS.items()}
        # An old lead from a reactivation list, or a caller, filled in no form; say where it
        # came from instead.
        if lead.campaign is not None:
            origin = f"From the reactivation list: {lead.campaign}"
        elif lead.intake == "phone_call":
            origin = "Came in as a phone call the office missed"
        else:
            origin = f"On the form: {problem_labels.get(lead.problem, lead.problem)}"
        lines += [
            f"Name: {lead.full_name}",
            f"Phone: {pretty_phone(lead.phone)}",
            f"Address: {lead.address}",
            origin,
            f"Inspection: {lead.booking or 'not booked'}",
        ]
        if lead.answers:
            answers = "; ".join(
                f"{ANSWER_LABELS.get(question, question)}: {answer}"
                for question, answer in lead.answers.items()
            )
            lines.append(f"Answers: {answers}")
    elif caller is not None:
        lines.append(f"From: {pretty_phone(caller) if caller.startswith('+1') else caller}")
    if alert.text_body is not None:
        lines += ["", "Their text:", f"  {alert.text_body}"]
    if alert.call_message is not None:
        lines += ["", "Their message:", f"  {alert.call_message}"]
    if alert.call_transcript:
        lines += ["", "The call:"]
        for turn in alert.call_transcript:
            # System notes (a hang-up, a draft that wasn't spoken) are marked, not quoted.
            speaker = {"caller": "Caller", "bot": "Bot"}.get(turn.speaker)
            lines.append(f"  {speaker}: {turn.text}" if speaker else f"  [{turn.text}]")
    if lead is not None and lead.transcript:
        lines += ["", "The whole conversation:"]
        for text in lead.transcript:
            speaker = "Homeowner" if text.direction == "in" else "Bot"
            # A text the homeowner never got is marked, so the owner doesn't assume they saw it.
            arrived = text.direction == "in" or text.status in ("sent", "delivered")
            lines.append(f"  {speaker}{'' if arrived else f' [{text.status}]'}: {text.body}")
    lines += ["", f"Alert {alert.alert_id}, sent by the lead bot."]

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = to_address
    if meaning.urgent:
        message["Importance"] = "high"  # most mail apps flag it
        message["X-Priority"] = "1"
    message.set_content("\n".join(lines))
    return message


# ---- the text to the owner ----


def build_alert_text(alert: AlertForText) -> str:
    """The short text version of an alert: what happened, who, and what to do. The full details
    (answers, the whole conversation) stay in the email."""
    meaning = alert_meaning(alert.reason)
    headline = f"Lead bot: {'URGENT: ' if meaning.urgent else ''}{meaning.headline}."
    details: list[str] = []
    if alert.full_name is not None and alert.phone is not None:
        details.append(f"{alert.full_name}, {pretty_phone(alert.phone)}, {alert.address}.")
    elif alert.call_phone is not None:
        caller = alert.call_phone
        details.append(f"Caller {pretty_phone(caller) if caller.startswith('+1') else caller}.")
    if alert.booking is not None:
        details.append(f"Inspection: {alert.booking}.")
    if alert.call_message is not None:
        details.append(f'Message: "{alert.call_message}"')
    # A homeowner's name or address typed on a phone may hold curly quotes; one of them would
    # switch the whole text to the costlier encoding (see text_script.py).
    text = plain_punctuation(" ".join([headline, *details, meaning.action]))
    if len(text) > MAX_ALERT_TEXT_CHARACTERS:
        text = plain_punctuation(" ".join([headline, *details, "Details are in your email."]))
    return text[:MAX_ALERT_TEXT_CHARACTERS]


# ---- sending ----


@dataclass(frozen=True)
class EmailSettings:
    to_address: str
    username: str
    password: str
    host: str = DEFAULT_SMTP_HOST
    port: int = DEFAULT_SMTP_PORT

    @classmethod
    def from_environment(cls) -> EmailSettings | None:
        """The email settings, or None when none are set. All three or none: a half-filled .env
        stops here, naming every missing value, rather than failing on the first alert."""
        values = {name: os.environ.get(name, "").strip() for name in EMAIL_VARIABLES}
        missing = [name for name, value in values.items() if not value]
        if len(missing) == len(EMAIL_VARIABLES):
            return None
        if missing:
            raise RuntimeError(
                f"Set all three owner alert email values in .env, or none. Missing: {', '.join(missing)}"
            )
        port_text = os.environ.get("SMTP_PORT", "").strip() or str(DEFAULT_SMTP_PORT)
        if not port_text.isdigit():
            raise RuntimeError(f"SMTP_PORT={port_text!r} must be a number, like 587")
        return cls(
            to_address=values["OWNER_ALERT_EMAIL"],
            username=values["SMTP_USERNAME"],
            # Google shows app passwords in groups of four with spaces; the spaces aren't part of it.
            password=values["SMTP_PASSWORD"].replace(" ", ""),
            host=os.environ.get("SMTP_HOST", "").strip() or DEFAULT_SMTP_HOST,
            port=int(port_text),
        )


class Mailer(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class SmtpConnection(Protocol):
    """The part of smtplib.SMTP the mailer uses, so tests can pass a stand-in."""

    def __enter__(self) -> SmtpConnection: ...

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    def starttls(self, *, context: ssl.SSLContext) -> object: ...

    def ehlo(self) -> object: ...

    def auth(self, mechanism: str, authobject: Callable[..., str]) -> object: ...

    def send_message(self, msg: EmailMessage) -> object: ...


class SmtpMailer:
    """Sends through an SMTP server with STARTTLS, logging in each time (alerts are rare)."""

    def __init__(
        self, settings: EmailSettings, smtp_factory: Callable[..., SmtpConnection] = smtplib.SMTP
    ) -> None:
        self._settings = settings
        self._smtp_factory = smtp_factory  # tests pass a fake

    def send(self, message: EmailMessage) -> None:
        username, password = self._settings.username, self._settings.password
        with self._smtp_factory(
            self._settings.host, self._settings.port, timeout=SMTP_TIMEOUT_SECONDS
        ) as smtp:
            # Certificate checked: the password must only ever go to the real server.
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()  # the server only lists its login methods again after encryption starts
            # One login method (PLAIN), not smtplib.login(): login() tries a second method after a
            # refusal, and Gmail hangs up on the retry, so a wrong password showed up as
            # "connection unexpectedly closed". With one method it's a clear 535 refusal.
            smtp.auth("PLAIN", lambda challenge=None: f"\0{username}\0{password}")
            smtp.send_message(message)


class PrintingMailer:
    """For the dry run without email settings: prints each alert email instead of sending it."""

    def send(self, message: EmailMessage) -> None:
        print(f"\n[owner alert email, not sent] {message['Subject']}\n{message.get_content()}")


@dataclass(frozen=True)
class AlertTexter:
    """Where alert texts go and what sends them. The sender is the notifier's own, not the
    worker's: the notifier runs on another thread, and one Twilio client shouldn't be shared
    between threads."""

    sender: Sender
    owner_phone: str  # E.164


class OwnerAlertNotifier:
    def __init__(
        self,
        store: Store,
        settings: BusinessSettings,
        mailer: Mailer,
        from_address: str,
        to_address: str,
        clock: Callable[[], datetime] = utc_now,
        record_sent: bool = True,
        texter: AlertTexter | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._mailer = mailer
        self._from = from_address
        self._to = to_address
        self._clock = clock
        # False when emails are only printed (no email settings). Then nothing is recorded as
        # emailed, so once real email is set up every alert still reaches the owner. Printed ones
        # are remembered for this run only, so each is printed once.
        self._record_sent = record_sent
        self._printed: set[int] = set()
        # None: no OWNER_ALERT_PHONE, so alerts are only emailed.
        self._texter = texter
        self._printed_texts: set[int] = set()

    def notify_pending(self) -> int:
        """Email every alert that's due, and text the owner the ones that need action today.
        Returns how many emails and texts went."""
        sent = self._email_pending()
        if self._texter is not None:
            sent += self._text_pending(self._texter)
        return sent

    def _email_pending(self) -> int:
        sent = 0
        for alert in self._store.alerts_to_email(self._clock(), limit=1000 if not self._record_sent else 10):
            if alert.alert_id in self._printed:
                continue
            message = build_alert_email(alert, self._settings, self._from, self._to)
            try:
                self._mailer.send(message)
            except smtplib.SMTPAuthenticationError as error:
                # Every email would fail the same way, so stop this pass and say what to fix.
                logger.critical(
                    "the mail server refused SMTP_USERNAME / SMTP_PASSWORD (%s). For Gmail, SMTP_PASSWORD "
                    "must be an app password. Alerts wait and are retried.",
                    error.smtp_code,
                )
                self._retry_later(alert, f"login refused: {error.smtp_code}")
                break
            except (smtplib.SMTPException, OSError) as error:
                logger.warning("alert %d email failed (try %d): %s", alert.alert_id, alert.tries + 1, error)
                self._retry_later(alert, f"{type(error).__name__}: {error}")
                continue
            if self._record_sent:
                self._store.mark_alert_emailed(alert.alert_id, self._clock())
            else:
                self._printed.add(alert.alert_id)
            logger.info("alert %d emailed: %s", alert.alert_id, alert.reason)
            sent += 1
        return sent

    def _retry_later(self, alert: AlertForEmail, error: str) -> None:
        gap = RETRY_GAPS[min(alert.tries, len(RETRY_GAPS) - 1)]
        self._store.retry_alert_email_later(alert.alert_id, error, self._clock() + gap)

    def _text_pending(self, texter: AlertTexter) -> int:
        """Text the owner each fresh alert that needs action today. Returns how many went."""
        sent = 0
        alerts = self._store.alerts_to_text(self._clock(), TEXTED_ALERT_REASONS, ALERT_TEXT_MAX_AGE)
        for alert in alerts:
            if alert.alert_id in self._printed_texts:
                continue
            try:
                provider_message_id = texter.sender.send(texter.owner_phone, build_alert_text(alert))
            except SenderCredentialsRejected as error:
                # Every text would fail the same way: stop this pass and try again later. Unlike a
                # refusal, this one is retried, since the text itself was never the problem.
                logger.critical("Twilio refused the bot's credentials for an alert text: %s", error)
                gap = RETRY_GAPS[min(alert.tries, len(RETRY_GAPS) - 1)]
                retry_at = self._clock() + gap
                self._store.retry_alert_text_later(alert.alert_id, f"credentials refused: {error}", retry_at)
                break
            except SendRefused as error:
                logger.warning("alert %d text refused: %s", alert.alert_id, error)
                self._store.give_up_alert_text(alert.alert_id, f"refused: {error}")
                continue
            except Exception as error:
                # It may have gone (a timeout after the request left), so it's never resent: the
                # owner getting the same alert twice would teach them to skim. The email has it.
                logger.exception("alert %d text: send failed; it may or may not have gone", alert.alert_id)
                error_note = f"may have gone: {type(error).__name__}: {error}"
                self._store.give_up_alert_text(alert.alert_id, error_note)
                continue
            if self._record_sent:
                self._store.mark_alert_texted(alert.alert_id, provider_message_id, self._clock())
            else:
                self._printed_texts.add(alert.alert_id)
            logger.info("alert %d texted: %s", alert.alert_id, alert.reason)
            sent += 1
        return sent

    def run_forever(self, stop: threading.Event) -> None:
        """The notifier thread's loop. It must never die quietly, so every pass is guarded."""
        while not stop.is_set():
            try:
                self.notify_pending()
            except Exception:
                logger.exception("owner alert pass failed; trying again shortly")
            stop.wait(CHECK_EVERY_SECONDS)


def build_notifier(
    store: Store, settings: BusinessSettings, email: EmailSettings | None, texter: AlertTexter | None = None
) -> OwnerAlertNotifier:
    """The notifier for these settings: real email when set, printed emails otherwise. `texter`
    adds texts to the owner; a printing sender there prints them instead."""
    if email is None:
        return OwnerAlertNotifier(
            store, settings, PrintingMailer(), "lead-bot", "the owner", record_sent=False, texter=texter
        )
    return OwnerAlertNotifier(
        store, settings, SmtpMailer(email), email.username, email.to_address, texter=texter
    )


def send_test_email(email: EmailSettings, settings: BusinessSettings, mailer: Mailer | None = None) -> None:
    message = EmailMessage()
    message["Subject"] = f"[{settings.business_name}] Test: owner alerts reach you"
    message["From"] = email.username
    message["To"] = email.to_address
    message.set_content(
        "This is a test from the lead bot. Owner alerts (an active leak, a homeowner asking for a "
        "person, a text that wasn't delivered) will arrive at this address."
    )
    (mailer or SmtpMailer(email)).send(message)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--test", action="store_true", help="send one test email and stop")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    settings = BusinessSettings.from_environment()
    email = EmailSettings.from_environment()
    if email is None:
        raise SystemExit(f"[owner_alerts] set {', '.join(EMAIL_VARIABLES)} in .env first")
    if arguments.test:
        try:
            send_test_email(email, settings)
        except smtplib.SMTPAuthenticationError as error:
            raise SystemExit(
                f"[owner_alerts] login refused ({error.smtp_code}). For Gmail, SMTP_PASSWORD must be an app "
                "password from myaccount.google.com/apppasswords, with 2-Step Verification on."
            ) from None
        print(f"[owner_alerts] test email sent to {email.to_address}")
        return
    # Without --test: email what's pending once and stop (the worker normally does this).
    count = build_notifier(Store(database_path_from_environment()), settings, email).notify_pending()
    print(f"[owner_alerts] {count} alert emails sent")


if __name__ == "__main__":
    main()
