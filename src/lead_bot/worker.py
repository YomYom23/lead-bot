# lead-bot/src/lead_bot/worker.py
"""The worker: everything that happens after the form, one small step at a time.

    uv run python -m lead_bot.worker              # dry run: texts are printed, not sent
    uv run python -m lead_bot.worker --offline    # dry run with keyword stand-ins, no keys needed
    uv run python -m lead_bot.worker --send       # real texts through Twilio (twilio_sms.py)

Each pass of the loop does, in order:
  1. answer the incoming texts that are waiting (Store.claim_incoming_text)
  2. queue the opening texts and reminders whose time has come
  3. hand quiet conversations to a person
  4. send what's queued

Owner alerts are emailed by a second thread (owner_alerts.py), so mail never delays a reply.

Every step reads its work from the database and writes the result back in one transaction, so a
crash or restart loses nothing (see store.py). The web server only ever adds rows; this is the
only process that sends.

Incoming texts come from Twilio's webhook (server.py), or from the simulator and the scenario
runner, which save them the same way. Texts go out through TwilioSender with --send, and are
printed otherwise.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from dotenv import load_dotenv

from .conversation import Conversation, ConversationServices, is_explicit_opt_out
from .jev import JevError
from .settings import BusinessSettings, database_path_from_environment
from .store import OutgoingText, Store
from .text_script import (
    opening_text,
    opt_out_confirmation,
    plain_punctuation,
    segment_count,
    twilio_keyword,
)
from .timing import TextingHours, is_allowed, next_allowed_time, utc_now
from .turn_data import TurnOutcome

if TYPE_CHECKING:
    # Imported for the type only: owner_alerts imports store and settings, which load fine, but
    # keeping it out of the import chain means the scenario runner never needs email settings.
    from .owner_alerts import OwnerAlertNotifier

logger = logging.getLogger(__name__)

# How many times one incoming text is tried when Jev fails, and how long between tries. Jev has
# its own quick retries (wiring.JEV_RETRIES); these cover a longer outage. On the last try the
# conversation goes to a person instead (Conversation.handle, last_try=True).
MAX_TRIES_PER_TEXT = 3
RETRY_DELAY = timedelta(minutes=1)
# A conversation the homeowner stopped replying to goes to a person after this long.
QUIET_CONVERSATION_LIMIT = timedelta(hours=24)
# The FCC presumes an opt-out confirmation sent within five minutes is covered by the original
# consent (47 CFR 64.1200(a)(12)). One that waited longer, because the worker was down, is
# dropped: no confirmation is safer than a late one.
OPT_OUT_CONFIRMATION_LIMIT = timedelta(minutes=5)
# How long the loop sleeps when a pass found nothing to do.
IDLE_SECONDS = 2.0


class SendRefused(Exception):
    """The provider refused the text outright (a bad number, the recipient opted out). It was
    certainly not sent."""

    def __init__(self, message: str, recipient_opted_out: bool = False) -> None:
        super().__init__(message)
        # True when the provider refused because the person opted out with it (Twilio's 21610:
        # they texted STOP and the webhook never reached us). The bot's own list must learn it.
        self.recipient_opted_out = recipient_opted_out


class SenderCredentialsRejected(Exception):
    """The provider refused the bot's own credentials. Every send would fail the same way, so
    the text goes back to the queue untouched and the worker stops (main exits with 78)."""


class Sender(Protocol):
    """Anything that can send one text. Returns the provider's id for it.

    Raises SendRefused when the text certainly wasn't sent. Any other exception means it isn't
    known whether it was (a timeout after the request went out), and the text is never retried.
    """

    def send(self, phone: str, body: str) -> str: ...


class PrintingSender:
    """Phase 1's sender: prints each text instead of sending it."""

    def __init__(self) -> None:
        self._count = 0

    def send(self, phone: str, body: str) -> str:
        self._count += 1
        segments = segment_count(body)
        print(f"\n[text to {phone}, {segments} segment{'s' if segments != 1 else ''}]\n{body}\n")
        return f"DRYRUN{self._count}"


@dataclass(frozen=True)
class PassSummary:
    """What one pass of the loop did, so the loop knows whether to sleep."""

    texts_answered: int
    openings_queued: int
    conversations_closed: int
    texts_sent: int

    @property
    def did_anything(self) -> bool:
        return bool(
            self.texts_answered or self.openings_queued or self.conversations_closed or self.texts_sent
        )


class Worker:
    def __init__(
        self,
        store: Store,
        settings: BusinessSettings,
        services: ConversationServices,
        sender: Sender,
        clock: Callable[[], datetime] = utc_now,
        on_turn: Callable[[TurnOutcome], None] | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._services = services
        self._sender = sender
        self._clock = clock  # tests and the scenario runner pass a fixed time
        # Called with each saved turn. The scenario runner uses it to see the writer's drafts,
        # which aren't saved in the database.
        self._on_turn = on_turn

    @property
    def hours(self) -> TextingHours:
        return self._settings.texting_hours

    def recover_after_restart(self) -> None:
        """Run once at startup, before the loop: settle what a stopped worker left half done."""
        requeued = self._store.requeue_interrupted_incoming()
        unknown = self._store.mark_interrupted_sends_unknown(self._clock())
        if requeued or unknown:
            logger.warning("after restart: %d incoming texts requeued, %d sends unknown", requeued, unknown)

    def run_once(self) -> PassSummary:
        answered = 0
        while self.answer_next_incoming():
            answered += 1
        queued = self.queue_due_openings()
        closed = len(self._store.close_quiet_conversations(self._clock(), QUIET_CONVERSATION_LIMIT))
        sent = 0
        while self.send_next_queued():
            sent += 1
        return PassSummary(answered, queued, closed, sent)

    def run_forever(self) -> None:
        self.recover_after_restart()
        while True:
            summary = self.run_once()
            if not summary.did_anything:
                time.sleep(IDLE_SECONDS)

    # ---- 1. incoming texts ----

    def answer_next_incoming(self) -> bool:
        """Answer the oldest waiting incoming text. False when there was none to answer."""
        now = self._clock()
        incoming = self._store.claim_incoming_text(now)
        if incoming is None:
            return False
        body = plain_punctuation(incoming.body).strip()
        keyword = twilio_keyword(body)
        explicit_opt_out = keyword == "stop" or is_explicit_opt_out(body)
        # A bare STOP got Twilio's confirmation already; our own goes only with an opt-out in
        # the homeowner's own words.
        confirmation = None if keyword == "stop" else opt_out_confirmation(self._settings)

        if incoming.conversation is None or incoming.on_do_not_call or incoming.closed:
            # Not a conversation the bot is running: never texted, opted out, or taken over by a
            # person. No scripted reply. An opt-out is still honored, and anything else is
            # passed to the owner, because only a person can tell what it's about.
            if incoming.conversation is None:
                reason = "text_from_a_number_without_a_conversation"
            elif incoming.on_do_not_call:
                reason = "text_from_an_opted_out_number"
            else:
                reason = "homeowner_texted_after_handoff"
            self._store.finish_incoming_outside_conversation(
                incoming.text_id,
                now,
                opt_out_source=f"texted {body[:80]!r}"
                if explicit_opt_out and not incoming.on_do_not_call
                else None,
                # Someone already opted out gets no second confirmation, and a stranger who was
                # never texted gets none at all.
                confirmation=confirmation
                if incoming.conversation is not None and not incoming.on_do_not_call
                else None,
                alert=None if keyword is not None else reason,
            )
            return True

        last_try = incoming.tries >= MAX_TRIES_PER_TEXT
        conversation = Conversation(incoming.conversation, incoming.history, self._settings, self._services)
        try:
            outcome = conversation.handle(body, last_try=last_try)
        except JevError as error:
            logger.warning("text %d: Jev failed (try %d): %s", incoming.text_id, incoming.tries, error)
            self._store.retry_incoming_later(incoming.text_id, now + RETRY_DELAY)
            return True
        except Exception as error:
            # A bug, or a failure nothing above expected. Retried like a Jev failure, then given up on
            # with an owner alert, so one bad text can't stop every text from this number behind it.
            logger.exception("text %d: unexpected error (try %d)", incoming.text_id, incoming.tries)
            if last_try:
                self._store.give_up_on_incoming(incoming.text_id, f"{type(error).__name__}: {error}", now)
            else:
                self._store.retry_incoming_later(incoming.text_id, now + RETRY_DELAY)
            return True
        self._store.save_turn(incoming.text_id, incoming.conversation.lead_id, outcome, now)
        if self._on_turn is not None:
            self._on_turn(outcome)
        return True

    # ---- 2. opening texts and reminders ----

    def queue_due_openings(self) -> int:
        now = self._clock()
        queued = 0
        for due in self._store.due_openings(now):
            body = opening_text(self._settings, due.full_name, due.address, due.attempt)
            result = self._store.queue_opening_text(due.lead_id, due.attempt, body, now, self.hours)
            if result == "queued":
                queued += 1
            elif result != "not_due":
                logger.info("lead %d: opening text %d %s", due.lead_id, due.attempt, result)
        return queued

    # ---- 4. sending ----

    def send_next_queued(self) -> bool:
        """Send the oldest queued text. False when there was none."""
        outgoing = self._store.claim_queued_text()
        if outgoing is None:
            return False
        now = self._clock()
        reason_not_to_send = self._reason_not_to_send(outgoing, now)
        if reason_not_to_send is not None:
            if reason_not_to_send == "outside texting hours" and outgoing.lead_id is not None:
                self._store.reschedule_opening(
                    outgoing.text_id, outgoing.lead_id, next_allowed_time(now, self.hours)
                )
            else:
                self._store.cancel_text(outgoing.text_id, reason_not_to_send)
            logger.info("text %d not sent: %s", outgoing.text_id, reason_not_to_send)
            return True
        try:
            provider_message_id = self._sender.send(outgoing.phone, outgoing.body)
        except SenderCredentialsRejected:
            # Not this text's fault: put it back as it was and stop, so the whole queue isn't
            # marked failed one text at a time.
            self._store.return_text_to_queue(outgoing.text_id)
            raise
        except SendRefused as error:
            self._store.mark_text_failed(outgoing.text_id, str(error), self._clock())
            if error.recipient_opted_out:
                # They texted STOP but the webhook never reached us; our own list must learn it.
                source = "the texting provider says they opted out"
                self._store.add_to_do_not_call(outgoing.phone, source, self._clock())
            return True
        except Exception as error:
            logger.exception("text %d: send failed; it may or may not have gone", outgoing.text_id)
            self._store.mark_text_unknown(outgoing.text_id, f"{type(error).__name__}: {error}", self._clock())
            return True
        self._store.mark_text_sent(outgoing.text_id, provider_message_id, self._clock())
        return True

    def _reason_not_to_send(self, outgoing: OutgoingText, now: datetime) -> str | None:
        """The last checks before a text goes out, made after it was claimed."""
        if outgoing.kind == "stop_confirmation":
            # The one text allowed after an opt-out, so the do-not-call entry doesn't stop it.
            if now - outgoing.created_at > OPT_OUT_CONFIRMATION_LIMIT:
                return "opt-out confirmation older than five minutes"
            return None
        if outgoing.on_do_not_call:
            return "number opted out"
        # A reply answers a text the homeowner just sent, so it isn't held for the window (see
        # timing.py). A text the bot starts is.
        if outgoing.kind == "opening" and not is_allowed(now, self.hours):
            return "outside texting hours"
        return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--offline", action="store_true", help="keyword stand-ins for Jev and the calendar")
    parser.add_argument("--send", action="store_true", help="send real texts through Twilio")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv()
    settings = BusinessSettings.from_environment()
    store = Store(database_path_from_environment())
    sender = _build_sender(arguments.send, settings)
    notifier = _build_alert_notifier(arguments.send, store, settings)
    if arguments.offline:
        from .stand_ins import offline_services

        services = offline_services(settings)
    else:
        from .wiring import build_conversation_services

        services = build_conversation_services(settings)
    # Alert emails run on their own thread: a slow mail server must never hold up a reply.
    stop_notifier = threading.Event()
    threading.Thread(
        target=notifier.run_forever, args=(stop_notifier,), name="owner-alerts", daemon=True
    ).start()
    try:
        Worker(store, settings, services, sender).run_forever()
    except SenderCredentialsRejected as error:
        logger.critical(
            "Twilio rejected TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN (%s). Fix them in .env and start "
            "the worker again. No texts were lost: the one being sent is back in the queue.",
            error,
        )
        # 78 (EX_CONFIG): the systemd unit doesn't restart on it, since a restart would fail the same way.
        raise SystemExit(78) from None
    finally:
        stop_notifier.set()


def _build_alert_notifier(
    send_for_real: bool, store: Store, settings: BusinessSettings
) -> OwnerAlertNotifier:
    from .owner_alerts import EMAIL_VARIABLES, EmailSettings, build_notifier

    email = EmailSettings.from_environment()
    if email is None and send_for_real:
        # Real homeowners are told "I'll flag this as urgent for our team". Without alert email
        # nobody would be told, so real texting doesn't start.
        raise SystemExit(f"[worker] --send needs owner alert email: set {', '.join(EMAIL_VARIABLES)} in .env")
    if email is None:
        print("[worker] owner alerts are printed, not emailed (no OWNER_ALERT_EMAIL in .env).")
    else:
        print(f"[worker] owner alerts are emailed to {email.to_address}.")
    return build_notifier(store, settings, email)


def _build_sender(send_for_real: bool, settings: BusinessSettings) -> Sender:
    if not send_for_real:
        print("[worker] dry run: texts are printed, not sent. Add --send to text for real. Ctrl+C to stop.")
        return PrintingSender()
    from .twilio_sms import TwilioSender, TwilioSettings, status_callback_url

    twilio_settings = TwilioSettings.from_environment()
    if twilio_settings is None:
        raise SystemExit(
            "[worker] --send needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER in .env"
        )
    callback_url = status_callback_url(settings.public_base_url)
    if callback_url is None:
        print("[worker] PUBLIC_BASE_URL isn't https://, so Twilio can't report delivery back to us.")
    print(f"[worker] sending real texts from {twilio_settings.phone_number}. Ctrl+C to stop.")
    return TwilioSender(twilio_settings, callback_url)


if __name__ == "__main__":
    main()
