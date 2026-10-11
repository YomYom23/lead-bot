# lead-bot/src/lead_bot/store.py
"""SQLite storage: leads, consent evidence, conversations, every text in and out, the do-not-call
list, bookings and owner alerts.

Two processes share the file. The web server (server.py) adds leads and, from phase 2, saves the
texts Twilio delivers. The worker (worker.py) does the rest: it answers incoming texts, queues the
opening texts that are due, and sends what's queued. WAL mode lets one write while the other reads.

Every piece of work is a row, not something held in memory, so a restart loses nothing:
  - An incoming text is saved the moment it arrives ('received') and answered later. One left
    'handling' by a worker that stopped goes back in the queue (requeue_interrupted_incoming).
  - Every text the bot sends is queued in the same transaction as the decision behind it
    (save_turn), then sent. One left 'sending' by a worker that stopped is marked 'unknown' and
    the owner is told, rather than sent again: two copies of a text are worse than a person
    checking whether one arrived.
  - The opening text and each reminder are a due time on the conversation (next_opening_due_at).

Times are stored as Unix seconds (UTC) and handed out as timezone-aware datetimes.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

from .consent import ConsentEvidence
from .leads import IntakeDecision, NewLead, ReactivationContact, decide_intake
from .slots import Slot
from .text_questions import STAGES, Stage
from .timing import (
    REMINDER_DELAYS_BY_KIND,
    ConversationKind,
    TextingHours,
    conversation_kind,
    is_allowed,
    max_text_attempts,
    next_allowed_time,
    reminder_time,
)
from .turn_data import ConversationState, LeadStatus, TurnOutcome
from .writer import Turn

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    lead_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name   TEXT NOT NULL,
    phone       TEXT NOT NULL,              -- E.164
    address     TEXT NOT NULL,
    problem     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    intake      TEXT NOT NULL,              -- the IntakeDecision made when the form arrived
    status      TEXT NOT NULL               -- a LeadStatus
);
CREATE INDEX IF NOT EXISTS leads_by_phone ON leads (phone, status);

-- One row per lead, written in the same transaction as the lead. Never updated: it is the
-- record of what was agreed at the time.
CREATE TABLE IF NOT EXISTS consents (
    lead_id       INTEGER PRIMARY KEY REFERENCES leads (lead_id),
    given         INTEGER NOT NULL,         -- 1 = box ticked on the current form version
    version       TEXT NOT NULL,
    text_shown    TEXT NOT NULL,
    ip_address    TEXT NOT NULL,
    forwarded_for TEXT,
    user_agent    TEXT NOT NULL,
    source        TEXT NOT NULL,
    recorded_at   REAL NOT NULL,
    -- When the person consented, if that was before it was recorded here: an old lead imported
    -- from a client's list (reactivation.py). NULL for a form lead, who consented at recorded_at.
    consented_at  REAL
);

-- One row per lead the bot texts, made with the lead. Where the conversation is, so each
-- incoming text picks up from there.
CREATE TABLE IF NOT EXISTS conversations (
    lead_id             INTEGER PRIMARY KEY REFERENCES leads (lead_id),
    stage               TEXT NOT NULL,              -- a Stage
    offered_slots       TEXT NOT NULL DEFAULT '[]', -- JSON: [[slot id, label], ...]
    unclear_in_a_row    INTEGER NOT NULL DEFAULT 0,
    slot_taken_count    INTEGER NOT NULL DEFAULT 0,
    opening_texts_sent  INTEGER NOT NULL DEFAULT 0, -- the first text plus reminders queued so far
    first_text_at       REAL,                       -- when the first text was queued
    next_opening_due_at REAL,                       -- the next opening text or reminder; NULL if none
    closed              INTEGER NOT NULL DEFAULT 0, -- 1: a person has taken over; the bot stays quiet
    result              TEXT,                       -- how the bot's part ended, e.g. 'booked'
    last_activity_at    REAL NOT NULL,              -- the last text either way
    kind                TEXT NOT NULL DEFAULT 'form' -- a ConversationKind: 'form' or 'reactivation'
);
CREATE INDEX IF NOT EXISTS conversations_due ON conversations (next_opening_due_at);

-- Every text in either direction, in order. The record of what the bot told the homeowner.
CREATE TABLE IF NOT EXISTS texts (
    text_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id             INTEGER REFERENCES leads (lead_id),  -- NULL: from a number with no conversation
    phone               TEXT NOT NULL,              -- the homeowner's number, E.164
    direction           TEXT NOT NULL,              -- 'in' or 'out'
    kind                TEXT NOT NULL,              -- a TextKind
    body                TEXT NOT NULL,
    status              TEXT NOT NULL,              -- an IncomingStatus or an OutgoingStatus
    created_at          REAL NOT NULL,
    not_before          REAL,                       -- incoming: not tried again before this
    tries               INTEGER NOT NULL DEFAULT 0,
    provider_message_id TEXT UNIQUE,                -- Twilio's MessageSid; UNIQUE drops a redelivery
    sent_at             REAL,
    note                TEXT                        -- what the bot decided but didn't text, or an error
);
CREATE INDEX IF NOT EXISTS texts_waiting ON texts (direction, status, text_id);
CREATE INDEX IF NOT EXISTS texts_by_lead ON texts (lead_id, text_id);

-- The qualifying answers, one row per lead and question. A later answer replaces an earlier one.
CREATE TABLE IF NOT EXISTS lead_answers (
    lead_id     INTEGER NOT NULL REFERENCES leads (lead_id),
    question    TEXT NOT NULL,              -- e.g. 'active_leak'
    answer      TEXT NOT NULL,              -- e.g. 'yes'
    recorded_at REAL NOT NULL,
    PRIMARY KEY (lead_id, question)
);

CREATE TABLE IF NOT EXISTS bookings (
    lead_id    INTEGER PRIMARY KEY REFERENCES leads (lead_id),  -- one inspection per lead
    starts_at  REAL NOT NULL,
    slot_label TEXT NOT NULL,
    booked_at  REAL NOT NULL
);

-- Numbers the bot never texts again. Named for the federal list; it covers texts too.
CREATE TABLE IF NOT EXISTS do_not_call (
    phone    TEXT PRIMARY KEY,
    added_at REAL NOT NULL,
    source   TEXT NOT NULL                  -- e.g. 'replied STOP', 'owner'
);

-- Twilio's delivery reports (the status callback), one row per text, keeping the latest.
-- Kept apart from texts because a report can arrive before the worker has recorded the send
-- (it only learns Twilio's MessageSid when the send returns). mark_text_sent applies any report
-- already waiting for that MessageSid.
CREATE TABLE IF NOT EXISTS delivery_reports (
    provider_message_id TEXT PRIMARY KEY,
    status              TEXT NOT NULL,          -- Twilio's MessageStatus, e.g. 'delivered'
    error_code          TEXT,                   -- Twilio's ErrorCode, e.g. '30032'
    received_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS owner_alerts (
    alert_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER REFERENCES leads (lead_id),  -- NULL: about a number with no lead
    reason     TEXT NOT NULL,
    created_at REAL NOT NULL,
    text_id    INTEGER REFERENCES texts (text_id),  -- the incoming text behind it, if one was
    call_id    INTEGER REFERENCES calls (call_id)   -- the phone call behind it, if one was
);

-- Which owner alerts have been emailed (owner_alerts.py). Kept apart from owner_alerts so a
-- database made before alert emails existed needs no change: an alert with no row here is
-- simply not emailed yet.
CREATE TABLE IF NOT EXISTS alert_emails (
    alert_id   INTEGER PRIMARY KEY REFERENCES owner_alerts (alert_id),
    sent_at    REAL,                        -- NULL until it has gone
    tries      INTEGER NOT NULL DEFAULT 0,
    not_before REAL,                        -- after a failure: not tried again before this
    last_error TEXT
);

-- Which owner alerts have been texted to the owner's phone (owner_alerts.py). Only alerts that
-- need action today are texted, and only while fresh; email still carries every alert. Kept
-- apart from alert_emails so each channel tracks its own sends and failures.
CREATE TABLE IF NOT EXISTS alert_texts (
    alert_id            INTEGER PRIMARY KEY REFERENCES owner_alerts (alert_id),
    sent_at             REAL,                -- NULL until it has gone
    provider_message_id TEXT,                -- Twilio's MessageSid for the text
    tries               INTEGER NOT NULL DEFAULT 0,
    not_before          REAL,                -- after a refused login: not tried again before this
    gave_up             INTEGER NOT NULL DEFAULT 0,  -- 1: refused, or may have gone; never retried
    last_error          TEXT
);

-- Reactivation campaigns (reactivation.py): a client's list of old leads, texted a few a day.
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    source_file     TEXT NOT NULL,          -- the list it was imported from
    daily_limit     INTEGER NOT NULL,       -- most contacts started per day
    created_at      REAL NOT NULL,
    last_started_at REAL                    -- when the last contact was started; spaces them out
);

-- One row per contact imported into a campaign. The contact itself is an ordinary lead (with
-- its consent row and a 'reactivation' conversation), so everything after the first text works
-- exactly as for a form lead.
CREATE TABLE IF NOT EXISTS campaign_contacts (
    lead_id     INTEGER PRIMARY KEY REFERENCES leads (lead_id),
    campaign_id INTEGER NOT NULL REFERENCES campaigns (campaign_id),
    started_at  REAL,                       -- NULL while waiting for its turn
    skipped     TEXT                        -- why it was never started, e.g. 'opted out since import'
);
CREATE INDEX IF NOT EXISTS campaign_contacts_waiting ON campaign_contacts (campaign_id, started_at);

-- Phone calls the bot answered (call_session.py): the business's line forwards calls nobody
-- picked up to the Twilio number, and the bot takes them. One row per call.
CREATE TABLE IF NOT EXISTS calls (
    call_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    call_sid    TEXT NOT NULL UNIQUE,       -- Twilio's CallSid; UNIQUE: a retried webhook is one call
    phone       TEXT NOT NULL,              -- the caller as Twilio gives it ('anonymous' if hidden)
    lead_id     INTEGER REFERENCES leads (lead_id),  -- the lead the call is about, once known
    relay_token TEXT NOT NULL,              -- one-time secret the live connection must present
    status      TEXT NOT NULL,              -- 'ringing' -> 'connected' -> 'ended'
    result      TEXT,                       -- how the bot's part ended, e.g. 'booked', 'message'
    message     TEXT,                       -- what a caller asked to be passed on, if anything
    started_at  REAL NOT NULL,
    ended_at    REAL
);

-- Everything said on a call, in order: the transcript the owner gets with the alert.
CREATE TABLE IF NOT EXISTS call_turns (
    turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_id INTEGER NOT NULL REFERENCES calls (call_id),
    speaker TEXT NOT NULL,                  -- 'bot', 'caller' or 'system'
    text    TEXT NOT NULL,
    said_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS call_turns_by_call ON call_turns (call_id, turn_id);

-- Which monthly results emails have gone (report.py), so each month's goes once.
CREATE TABLE IF NOT EXISTS monthly_reports (
    month   TEXT PRIMARY KEY,               -- 'YYYY-MM', the month the report covers
    sent_at REAL NOT NULL
);

-- When the worker last completed a loop pass (one row), so /health can tell a stopped worker
-- from a healthy one. The server reads the database either way.
CREATE TABLE IF NOT EXISTS worker_heartbeat (
    only_row INTEGER PRIMARY KEY CHECK (only_row = 1),
    beat_at  REAL NOT NULL
);
"""

# homeowner         -> a text the homeowner sent us
# opening           -> the first text or a reminder, started by the bot
# reply             -> the bot's answer to a homeowner text
# stop_confirmation -> the one text sent after an opt-out in the homeowner's own words
TextKind = Literal["homeowner", "opening", "reply", "stop_confirmation"]

# Incoming: received -> waiting to be answered;  handling -> a worker is answering it;
#           handled  -> answered (or deliberately left unanswered)
IncomingStatus = Literal["received", "handling", "handled"]

# Outgoing: queued -> waiting to be sent;  sending -> handed to the sender;  sent -> accepted by
#           the provider;  delivered -> the carrier confirmed delivery;  undelivered -> the
#           carrier reported it wasn't delivered;  failed -> refused, or the provider couldn't
#           send it;  cancelled -> never sent (the number opted out);  unknown -> a worker
#           stopped mid-send, so it may or may not have gone
OutgoingStatus = Literal[
    "queued", "sending", "sent", "delivered", "undelivered", "failed", "cancelled", "unknown"
]

# Twilio statuses that end a text's story. Only these change a text's row; the in-between ones
# ('queued', 'sending', 'sent') say nothing the row doesn't already.
FINAL_DELIVERY_STATUSES = ("delivered", "undelivered", "failed")

# What queue_opening_text did:
#   queued      -> the text is queued to send now
#   rescheduled -> outside texting hours; moved to the next opening of the window
#   stopped     -> no consent, or the number opted out; nothing more will be started
#   not_due     -> nothing to do (already queued by another pass, or the lead replied meanwhile)
OpeningResult = Literal["queued", "rescheduled", "stopped", "not_due"]

# A second form from a number in one of these gets no second conversation.
ACTIVE_LEAD_STATUSES = ("following_up", "in_conversation", "booked")

# Where a lead came from, as SQL over `leads` (the monthly report counts bookings by it): a
# reactivation list, a phone call the office missed, or the web form.
_LEAD_SOURCE = (
    "CASE WHEN EXISTS (SELECT 1 FROM campaign_contacts WHERE campaign_contacts.lead_id = leads.lead_id) "
    "THEN 'reactivation' WHEN leads.intake = 'phone_call' THEN 'phone' ELSE 'form' END"
)

LEAD_STATUS_FOR_INTAKE: dict[IntakeDecision, LeadStatus] = {
    "text": "following_up",
    "previously_opted_out": "owner_alerted",
    "no_text_consent": "owner_alerted",
    "already_in_follow_up": "duplicate",
}


@dataclass(frozen=True)
class AddedLead:
    lead_id: int
    decision: IntakeDecision
    first_text_due: datetime | None  # None when the bot won't text


# The consent version stored for an imported contact. The exact words they agreed to are in
# text_shown, as for a form lead; this marks that the business, not our form, collected them.
IMPORTED_CONSENT_VERSION = "imported-list"


class CampaignExists(ValueError):
    """A campaign with that name was already imported."""


@dataclass
class CampaignImport:
    """What importing a contact list did (or, for a dry run, would do)."""

    campaign_id: int | None  # None after a dry run
    lead_ids: list[int]  # the contacts added, one lead each
    skipped: dict[str, list[str]]  # why -> the phone numbers left out for that reason

    @property
    def added(self) -> int:
        return len(self.lead_ids)


@dataclass(frozen=True)
class CampaignSummary:
    name: str
    daily_limit: int
    created_at: datetime
    contacts_by_status: dict[str, int]  # LeadStatus -> how many; campaign_waiting = not texted yet
    skipped: int  # left out at their turn (opted out or in a conversation since the import)


class _RollBackDryRun(Exception):
    """Raised inside add_campaign's transaction on a dry run, so the transaction rolls back and
    the result comes out with it."""

    def __init__(self, result: CampaignImport) -> None:
        super().__init__("dry run")
        self.result = result


# The consent version stored when a caller agrees on the phone to a text with their booking.
VERBAL_CONSENT_VERSION = "verbal-on-call"


@dataclass(frozen=True)
class IncomingCall:
    """A call Twilio just told us about, before the live connection opens."""

    call_id: int
    relay_token: str  # the live connection must present this; see connect_call


@dataclass(frozen=True)
class KnownCaller:
    """The most recent lead with the caller's number, for a caller who has been in touch before."""

    lead_id: int
    full_name: str
    address: str
    booked_label: str | None
    texts_allowed: bool  # their consent covers texts and the number isn't on the do-not-call list


@dataclass(frozen=True)
class ConnectedCall:
    call_id: int
    call_sid: str
    phone: str  # as Twilio gave it: E.164, or 'anonymous' for a hidden number
    known: KnownCaller | None
    on_do_not_call: bool


@dataclass(frozen=True)
class CallTurnLine:
    speaker: str  # 'bot', 'caller' or 'system'
    text: str


@dataclass(frozen=True)
class BookedInspection:
    full_name: str
    slot_label: str
    source: str  # 'form', 'phone' or 'reactivation'


@dataclass(frozen=True)
class MonthlyNumbers:
    """What the bot did in one month, for the owner's monthly email (report.py)."""

    form_leads: int  # leads from the web form
    form_leads_texted: int  # of those, the ones with consent the bot texted
    form_leads_replied: int  # of those texted, the ones who replied
    form_leads_booked: int
    bookings: int  # every inspection booked this month, whatever the source
    bookings_by_source: dict[str, int]  # 'form', 'phone', 'reactivation' -> how many
    calls_answered: int
    calls_booked: int
    calls_messages: int  # callers who left a message for a call back
    calls_hung_up: int
    reactivation_started: int  # old leads texted for the first time this month
    reactivation_replied: int
    reactivation_booked: int
    urgent_leaks: int
    alerts_for_owner: int  # alerts that needed a person, not counting "inspection booked"
    opted_out: int  # numbers added to the do-not-call list
    recent_bookings: list[BookedInspection]  # up to three, most recent first


@dataclass(frozen=True)
class DueOpening:
    """An opening text whose time has come. The worker words it; queue_opening_text re-checks it."""

    lead_id: int
    attempt: int  # 1 = the first text, 2 and up = reminders
    full_name: str
    address: str
    kind: ConversationKind = "form"  # an old lead from a reactivation list has its own wording


@dataclass(frozen=True)
class IncomingText:
    """A claimed incoming text, with the conversation it belongs to, read in the same transaction."""

    text_id: int
    phone: str
    body: str
    tries: int  # how many times it has been claimed, this time included
    conversation: ConversationState | None  # None: no conversation was ever started with this number
    closed: bool  # a person has taken the conversation over
    on_do_not_call: bool
    history: list[Turn]  # the conversation before this text, oldest first


@dataclass(frozen=True)
class OutgoingText:
    """A claimed text to send, with the do-not-call check read in the same transaction."""

    text_id: int
    lead_id: int | None
    phone: str
    kind: str
    body: str
    created_at: datetime  # when it was queued
    on_do_not_call: bool


@dataclass(frozen=True)
class TranscriptLine:
    direction: str  # 'in' or 'out'
    kind: str
    body: str
    status: str


@dataclass(frozen=True)
class OwnerAlert:
    alert_id: int
    lead_id: int | None
    reason: str
    created_at: datetime


@dataclass(frozen=True)
class AlertForEmail:
    """An owner alert not emailed yet, with everything the email shows about it."""

    alert_id: int
    reason: str
    created_at: datetime
    tries: int  # failed attempts so far
    lead: LeadSummary | None  # None for a text from a number the bot never texted
    text_phone: str | None  # the incoming text behind the alert, if there was one
    text_body: str | None
    # The phone call behind the alert, if there was one: who called, what they asked to pass
    # on, and everything said.
    call_phone: str | None = None
    call_message: str | None = None
    call_transcript: tuple[CallTurnLine, ...] = ()


@dataclass(frozen=True)
class AlertForText:
    """An owner alert to text the owner, with the few details that fit in a text."""

    alert_id: int
    reason: str
    tries: int  # refused logins so far
    full_name: str | None  # None for an alert with no lead
    phone: str | None
    address: str | None
    booking: str | None  # the booked time's label, if there is one
    call_phone: str | None = None  # a caller with no lead (they left a message)
    call_message: str | None = None


@dataclass(frozen=True)
class LeadSummary:
    lead_id: int
    full_name: str
    phone: str
    address: str
    problem: str
    answers: dict[str, str]
    booking: str | None
    transcript: list[TranscriptLine]
    campaign: str | None = None  # the reactivation campaign it came from; None for a form lead
    intake: str = ""  # how it arrived: an IntakeDecision for a form, 'phone_call' for a caller


class Store:
    def __init__(self, database_path: Path | str) -> None:
        self._path = str(database_path)
        connection = self._connect()
        try:
            # WAL lets the web server write while the worker reads, instead of one blocking.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            # CREATE TABLE IF NOT EXISTS leaves an existing table as it was, so a column added
            # later has to be added to databases made before it.
            alert_columns = {row["name"] for row in connection.execute("PRAGMA table_info(owner_alerts)")}
            if "text_id" not in alert_columns:
                connection.execute(
                    "ALTER TABLE owner_alerts ADD COLUMN text_id INTEGER REFERENCES texts (text_id)"
                )
            conversation_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(conversations)")
            }
            if "kind" not in conversation_columns:
                # Every conversation before reactivation existed followed up a form.
                connection.execute("ALTER TABLE conversations ADD COLUMN kind TEXT NOT NULL DEFAULT 'form'")
            consent_columns = {row["name"] for row in connection.execute("PRAGMA table_info(consents)")}
            if "consented_at" not in consent_columns:
                connection.execute("ALTER TABLE consents ADD COLUMN consented_at REAL")
            if "call_id" not in alert_columns:
                # Which call an alert came from, so its email carries the call's transcript.
                connection.execute(
                    "ALTER TABLE owner_alerts ADD COLUMN call_id INTEGER REFERENCES calls (call_id)"
                )
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: Python's sqlite3 module then never opens transactions behind our
        # back. Every write below starts its own with BEGIN IMMEDIATE (see _write_transaction).
        connection = sqlite3.connect(self._path, timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")  # SQLite leaves REFERENCES unchecked otherwise
        return connection

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        """One transaction that holds the write lock from its first statement.

        IMMEDIATE matters for anything that reads and then writes, like add_lead. With a plain
        BEGIN, two overlapping submissions both start as readers. When the second then tries to
        write, SQLite can't upgrade its now out-of-date read, and fails at once with "database
        is locked": that homeowner's lead is lost. IMMEDIATE makes the second wait for the
        first to commit, then read the first's lead.
        """
        # Unlike a file, `with sqlite3.connect(...)` would NOT close the connection; it only
        # commits. So this closes it explicitly.
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    # ---- leads ----

    def add_lead(
        self, lead: NewLead, consent: ConsentEvidence, now: datetime, hours: TextingHours
    ) -> AddedLead:
        """Record a form submission and act on it, all in one transaction.

        The lead, its consent evidence, and either its conversation (with the first text due) or
        an owner alert are written together. A crash can't leave a scheduled text with no
        consent record behind it.
        """
        with self._write_transaction() as connection:
            on_do_not_call = _is_on_do_not_call(connection, lead.phone)
            placeholders = ", ".join("?" for _ in ACTIVE_LEAD_STATUSES)
            already_in_follow_up = (
                connection.execute(
                    f"SELECT 1 FROM leads WHERE phone = ? AND status IN ({placeholders})",
                    (lead.phone, *ACTIVE_LEAD_STATUSES),
                ).fetchone()
                is not None
            )
            decision = decide_intake(consent.given, on_do_not_call, already_in_follow_up)
            cursor = connection.execute(
                "INSERT INTO leads (full_name, phone, address, problem, created_at, intake, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    lead.full_name,
                    lead.phone,
                    lead.address,
                    lead.problem,
                    now.timestamp(),
                    decision,
                    LEAD_STATUS_FOR_INTAKE[decision],
                ),
            )
            lead_id = cursor.lastrowid
            if lead_id is None:  # can't happen after an INSERT, but the type allows it
                raise RuntimeError("SQLite returned no id for the new lead")
            connection.execute(
                "INSERT INTO consents (lead_id, given, version, text_shown, ip_address, forwarded_for, "
                "user_agent, source, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    lead_id,
                    1 if consent.given else 0,
                    consent.version,
                    consent.text_shown,
                    consent.ip_address,
                    consent.forwarded_for,
                    consent.user_agent,
                    consent.source,
                    now.timestamp(),
                ),
            )
            first_text_due: datetime | None = None
            if decision == "text":
                first_text_due = next_allowed_time(now, hours)
                # The first text asks the address question, so the conversation starts there.
                connection.execute(
                    "INSERT INTO conversations (lead_id, stage, next_opening_due_at, last_activity_at) "
                    "VALUES (?, 'address', ?, ?)",
                    (lead_id, first_text_due.timestamp(), now.timestamp()),
                )
            elif decision in ("previously_opted_out", "no_text_consent"):
                _add_owner_alert(connection, lead_id, decision, now)
            return AddedLead(lead_id=lead_id, decision=decision, first_text_due=first_text_due)

    def lead_status(self, lead_id: int) -> str | None:
        with self._read() as connection:
            row = connection.execute("SELECT status FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        return None if row is None else str(row["status"])

    def add_owner_alert(self, lead_id: int | None, reason: str, now: datetime) -> None:
        with self._write_transaction() as connection:
            _add_owner_alert(connection, lead_id, reason, now)

    # ---- reactivation campaigns (reactivation.py) ----

    def add_campaign(
        self,
        name: str,
        source_file: str,
        daily_limit: int,
        contacts: Sequence[ReactivationContact],
        now: datetime,
        hours: TextingHours,
        save: bool = True,
    ) -> CampaignImport:
        """Import a checked contact list as a campaign, all in one transaction.

        Each contact becomes an ordinary lead with its consent evidence and a 'reactivation'
        conversation, waiting for its turn (start_campaign_contacts). A contact is left out when
        its number is on the do-not-call list, already in a conversation, or in an earlier
        campaign: texting someone twice from two lists is how complaints start.

        save=False is the dry run: everything is checked inside the transaction exactly as for
        real, then rolled back, so the counts it reports are the ones a real import would give.
        """
        if daily_limit < 1:
            raise ValueError("a campaign's daily limit must be at least 1")
        try:
            with self._write_transaction() as connection:
                if connection.execute("SELECT 1 FROM campaigns WHERE name = ?", (name,)).fetchone():
                    raise CampaignExists(f"there is already a campaign named {name!r}; pick another name")
                cursor = connection.execute(
                    "INSERT INTO campaigns (name, source_file, daily_limit, created_at) VALUES (?, ?, ?, ?)",
                    (name, source_file, daily_limit, now.timestamp()),
                )
                campaign_id = cursor.lastrowid
                if campaign_id is None:  # can't happen after an INSERT, but the type allows it
                    raise RuntimeError("SQLite returned no id for the new campaign")
                result = CampaignImport(campaign_id=campaign_id if save else None, lead_ids=[], skipped={})
                for contact in contacts:
                    reason = _reason_to_leave_out(connection, contact.phone)
                    if reason is not None:
                        result.skipped.setdefault(reason, []).append(contact.phone)
                        continue
                    result.lead_ids.append(
                        _add_campaign_contact(connection, campaign_id, source_file, contact, now, hours)
                    )
                if not save:
                    raise _RollBackDryRun(result)
                return result
        except _RollBackDryRun as dry_run:
            return dry_run.result

    def start_campaign_contacts(self, now: datetime, hours: TextingHours) -> int:
        """Start the next waiting contact of each campaign whose turn has come. Returns how many.

        A campaign starts at most `daily_limit` contacts per local day, spread evenly across the
        texting window rather than all at the opening. 1,000 texts at 8 AM would bring a burst
        of replies all wanting the same few inspection times, and a wave of owner alerts.

        Each contact is checked again right before it starts, since days may have passed since
        the import: on the do-not-call list now, or in a conversation now (they filled in the
        form meanwhile), and it's skipped instead. queue_opening_text then checks consent and
        the do-not-call list once more, as it does for every opening text.
        """
        if not is_allowed(now, hours):
            return 0
        local_now = now.astimezone(hours.timezone)
        start_of_today = datetime.combine(local_now.date(), time(0), tzinfo=hours.timezone)
        started = 0
        with self._write_transaction() as connection:
            campaigns = connection.execute(
                "SELECT campaign_id, daily_limit, last_started_at FROM campaigns WHERE EXISTS ("
                "SELECT 1 FROM campaign_contacts WHERE campaign_contacts.campaign_id = campaigns.campaign_id "
                "AND started_at IS NULL AND skipped IS NULL)"
            ).fetchall()
            for campaign in campaigns:
                started_today = connection.execute(
                    "SELECT COUNT(*) FROM campaign_contacts WHERE campaign_id = ? AND started_at >= ?",
                    (campaign["campaign_id"], start_of_today.timestamp()),
                ).fetchone()[0]
                if started_today >= campaign["daily_limit"]:
                    continue
                last_started_at = campaign["last_started_at"]
                spacing = _start_spacing(hours, campaign["daily_limit"]).total_seconds()
                if last_started_at is not None and now.timestamp() - last_started_at < spacing:
                    continue
                if _start_next_contact(connection, campaign["campaign_id"], now):
                    started += 1
        return started

    def campaign_summaries(self) -> list[CampaignSummary]:
        """Every campaign with its contacts counted by where they are, for `reactivation status`."""
        with self._read() as connection:
            campaigns = connection.execute(
                "SELECT campaign_id, name, daily_limit, created_at FROM campaigns ORDER BY campaign_id"
            ).fetchall()
            summaries: list[CampaignSummary] = []
            for campaign in campaigns:
                rows = connection.execute(
                    "SELECT leads.status, COUNT(*) AS total FROM campaign_contacts "
                    "JOIN leads ON leads.lead_id = campaign_contacts.lead_id "
                    "WHERE campaign_contacts.campaign_id = ? AND campaign_contacts.skipped IS NULL "
                    "GROUP BY leads.status",
                    (campaign["campaign_id"],),
                ).fetchall()
                skipped = connection.execute(
                    "SELECT COUNT(*) FROM campaign_contacts WHERE campaign_id = ? AND skipped IS NOT NULL",
                    (campaign["campaign_id"],),
                ).fetchone()[0]
                summaries.append(
                    CampaignSummary(
                        name=campaign["name"],
                        daily_limit=campaign["daily_limit"],
                        created_at=datetime.fromtimestamp(campaign["created_at"], UTC),
                        contacts_by_status={row["status"]: row["total"] for row in rows},
                        skipped=skipped,
                    )
                )
        return summaries

    # ---- phone calls (call_session.py, relay.py, server.py) ----

    def start_incoming_call(self, call_sid: str, phone: str, now: datetime) -> IncomingCall:
        """Record a call Twilio is about to connect, with a fresh one-time token for the live
        connection. Twilio may retry the webhook; the same CallSid gets the same call back."""
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO calls (call_sid, phone, relay_token, status, started_at) "
                "VALUES (?, ?, ?, 'ringing', ?)",
                (call_sid, phone, secrets.token_urlsafe(24), now.timestamp()),
            )
            row = connection.execute(
                "SELECT call_id, relay_token FROM calls WHERE call_sid = ?", (call_sid,)
            ).fetchone()
        return IncomingCall(call_id=row["call_id"], relay_token=row["relay_token"])

    def connect_call(
        self, call_id: int, relay_token: str, now: datetime, call_sid: str | None = None
    ) -> ConnectedCall | None:
        """Let the live connection start, once, and only with the token this call was given.

        None for a wrong token, a call that isn't ringing (already connected or ended), no such
        call, or (when `call_sid` is given) a different Twilio call than the one recorded. The
        WebSocket address is public, so without this anyone who found it could open a "call"
        and talk to the bot, booking real inspections.
        """
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT call_sid, phone, relay_token, status FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            # compare_digest takes the same time however much of the token matches, so the
            # token can't be guessed one character at a time from how fast refusals come back.
            if (
                row is None
                or row["status"] != "ringing"
                or not hmac.compare_digest(str(row["relay_token"]), relay_token)
                or (call_sid is not None and call_sid != row["call_sid"])
            ):
                return None
            connection.execute("UPDATE calls SET status = 'connected' WHERE call_id = ?", (call_id,))
            on_do_not_call = _is_on_do_not_call(connection, row["phone"])
            return ConnectedCall(
                call_id=call_id,
                call_sid=row["call_sid"],
                phone=row["phone"],
                known=_known_caller(connection, row["phone"], on_do_not_call),
                on_do_not_call=on_do_not_call,
            )

    def add_call_turn(self, call_id: int, speaker: str, text: str, now: datetime) -> None:
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT INTO call_turns (call_id, speaker, text, said_at) VALUES (?, ?, ?, ?)",
                (call_id, speaker, text, now.timestamp()),
            )

    def call_transcript(self, call_id: int) -> list[CallTurnLine]:
        with self._read() as connection:
            return _call_transcript(connection, call_id)

    def call_result(self, call_id: int) -> str | None:
        with self._read() as connection:
            row = connection.execute("SELECT result FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        return None if row is None else row["result"]

    def call_lead_id(self, call_id: int) -> int | None:
        with self._read() as connection:
            row = connection.execute("SELECT lead_id FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        return None if row is None else row["lead_id"]

    def link_call_to_lead(self, call_id: int, lead_id: int) -> None:
        """A known caller, confirmed by name: the call's answers and booking go to their lead."""
        with self._write_transaction() as connection:
            connection.execute("UPDATE calls SET lead_id = ? WHERE call_id = ?", (lead_id, call_id))

    def add_lead_from_call(
        self, call_id: int, full_name: str, phone: str, address: str, now: datetime
    ) -> int:
        """A new caller with a roof to inspect becomes a lead, linked to the call. No consent row
        yet: they've agreed to nothing about texts until they're asked (record_text_consent)."""
        with self._write_transaction() as connection:
            cursor = connection.execute(
                "INSERT INTO leads (full_name, phone, address, problem, created_at, intake, status) "
                "VALUES (?, ?, ?, 'not_sure', ?, 'phone_call', 'in_conversation')",
                (full_name, phone, address, now.timestamp()),
            )
            lead_id = cursor.lastrowid
            if lead_id is None:  # can't happen after an INSERT, but the type allows it
                raise RuntimeError("SQLite returned no id for the new lead")
            connection.execute("UPDATE calls SET lead_id = ? WHERE call_id = ?", (lead_id, call_id))
            return lead_id

    def save_answer(self, lead_id: int, question: str, answer: str, now: datetime) -> None:
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT INTO lead_answers (lead_id, question, answer, recorded_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (lead_id, question) DO UPDATE SET answer = excluded.answer, "
                "recorded_at = excluded.recorded_at",
                (lead_id, question, answer, now.timestamp()),
            )

    def add_call_alert(self, call_id: int, reason: str, now: datetime) -> None:
        """An owner alert raised during a call, about the call's lead if it has one yet."""
        with self._write_transaction() as connection:
            row = connection.execute("SELECT lead_id FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            lead_id = None if row is None else row["lead_id"]
            _add_owner_alert(connection, lead_id, reason, now, call_id=call_id)

    def record_call_booking(
        self, call_id: int, lead_id: int, starts_at: datetime, slot_label: str, now: datetime
    ) -> None:
        """The booking, the lead now booked, and the owner told, in one transaction. A text
        conversation the lead already had stops its reminders and knows about the booking."""
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO bookings (lead_id, starts_at, slot_label, booked_at) "
                "VALUES (?, ?, ?, ?)",
                (lead_id, starts_at.timestamp(), slot_label, now.timestamp()),
            )
            connection.execute("UPDATE leads SET status = 'booked' WHERE lead_id = ?", (lead_id,))
            connection.execute(
                "UPDATE conversations SET stage = 'booked', offered_slots = '[]', "
                "next_opening_due_at = NULL, result = 'booked', last_activity_at = ? WHERE lead_id = ?",
                (now.timestamp(), lead_id),
            )
            _add_owner_alert(connection, lead_id, "inspection_booked", now, call_id=call_id)

    def record_text_consent(
        self, lead_id: int, call_sid: str, given: bool, question: str, now: datetime
    ) -> None:
        """What a new caller answered when asked on the phone whether they may be texted. Kept
        like a form's consent: the exact question, the answer, when, and which call."""
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO consents (lead_id, given, version, text_shown, ip_address, "
                "forwarded_for, user_agent, source, recorded_at) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (
                    lead_id,
                    1 if given else 0,
                    VERBAL_CONSENT_VERSION,
                    question,
                    "phone call",
                    f"Twilio call {call_sid}",
                    "spoken answer on a phone call",
                    now.timestamp(),
                ),
            )

    def queue_call_confirmation(self, lead_id: int, body: str, now: datetime) -> None:
        """Text the booking confirmation after a call, and give the lead a text conversation
        (at 'booked') so a reply to it is answered like any other. The caller must have agreed
        to texts; the do-not-call list is checked again here and when it's sent."""
        with self._write_transaction() as connection:
            lead = connection.execute("SELECT phone FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
            consent = connection.execute(
                "SELECT given FROM consents WHERE lead_id = ?", (lead_id,)
            ).fetchone()
            if lead is None or consent is None or consent["given"] != 1 or _is_on_do_not_call(
                connection, lead["phone"]
            ):
                return
            connection.execute(
                "INSERT OR IGNORE INTO conversations (lead_id, stage, last_activity_at, kind, result) "
                "VALUES (?, 'booked', ?, 'call', 'booked')",
                (lead_id, now.timestamp()),
            )
            _queue_text(connection, lead_id, lead["phone"], "reply", body, now)

    def finish_call(
        self,
        call_id: int,
        result: str,
        now: datetime,
        lead_status: LeadStatus | None = None,
        alert: str | None = None,
        message: str | None = None,
    ) -> bool:
        """Record how the call ended, once. False if it had already ended (a hang-up arriving
        after the bot said goodbye, say), and then nothing changes."""
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT status, lead_id FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            if row is None or row["status"] == "ended":
                return False
            connection.execute(
                "UPDATE calls SET status = 'ended', result = ?, ended_at = ?, message = COALESCE(?, message) "
                "WHERE call_id = ?",
                (result, now.timestamp(), message, call_id),
            )
            lead_id = row["lead_id"]
            if lead_id is not None and lead_status is not None:
                # A booked lead stays booked, and an opted-out one stays opted out.
                connection.execute(
                    "UPDATE leads SET status = ? WHERE lead_id = ? AND status IN ('in_conversation', "
                    "'following_up', 'unreached', 'handed_off', 'campaign_waiting')",
                    (lead_status, lead_id),
                )
            if alert is not None:
                _add_owner_alert(connection, lead_id, alert, now, call_id=call_id)
            return True

    # ---- opening texts and reminders ----

    def due_openings(self, now: datetime) -> list[DueOpening]:
        """Opening texts and reminders whose time has come, most overdue first."""
        with self._read() as connection:
            rows = connection.execute(
                "SELECT conversations.lead_id, conversations.opening_texts_sent, conversations.kind, "
                "leads.full_name, leads.address FROM conversations "
                "JOIN leads ON leads.lead_id = conversations.lead_id "
                "WHERE conversations.next_opening_due_at <= ? AND conversations.closed = 0 "
                "AND leads.status = 'following_up' ORDER BY conversations.next_opening_due_at",
                (now.timestamp(),),
            ).fetchall()
        return [
            DueOpening(
                lead_id=row["lead_id"],
                attempt=row["opening_texts_sent"] + 1,
                full_name=row["full_name"],
                address=row["address"],
                kind=conversation_kind(row["kind"]),
            )
            for row in rows
        ]

    def queue_opening_text(
        self, lead_id: int, attempt: int, body: str, now: datetime, hours: TextingHours
    ) -> OpeningResult:
        """Queue one opening text, if everything that allows it still holds right now.

        Checked here, in the transaction that queues the text, rather than when it was scheduled:
        the consent, the do-not-call list, the lead still waiting for a first reply, and the
        texting window. `attempt` must be the next one due, so a second pass (or a second worker)
        can't queue the same text twice.

        Queuing also schedules the next reminder, measured from the first text.
        """
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT conversations.opening_texts_sent, conversations.first_text_at, "
                "conversations.next_opening_due_at, conversations.closed, conversations.kind, "
                "leads.status, leads.phone, "
                "consents.given AS consent_given, "
                "EXISTS (SELECT 1 FROM do_not_call WHERE do_not_call.phone = leads.phone) AS on_do_not_call "
                "FROM conversations JOIN leads ON leads.lead_id = conversations.lead_id "
                # LEFT JOIN: a lead with no consent row still comes back, with consent_given NULL,
                # which counts as "not given" below. An inner join would hide it, and its text
                # would stay due forever.
                "LEFT JOIN consents ON consents.lead_id = conversations.lead_id "
                "WHERE conversations.lead_id = ?",
                (lead_id,),
            ).fetchone()
            if (
                row is None
                or row["closed"]
                or row["status"] != "following_up"
                or row["opening_texts_sent"] != attempt - 1
                or row["next_opening_due_at"] is None
                or row["next_opening_due_at"] > now.timestamp()
            ):
                return "not_due"
            if row["on_do_not_call"]:
                _stop_opening_texts(connection, lead_id, result="opted_out", lead_status="opted_out")
                return "stopped"
            if row["consent_given"] != 1:  # NULL (no consent row) counts as not given
                _stop_opening_texts(connection, lead_id, result="no_consent", lead_status="owner_alerted")
                _add_owner_alert(connection, lead_id, "no_text_consent", now)
                return "stopped"
            if not is_allowed(now, hours):
                connection.execute(
                    "UPDATE conversations SET next_opening_due_at = ? WHERE lead_id = ?",
                    (next_allowed_time(now, hours).timestamp(), lead_id),
                )
                return "rescheduled"

            first_text_at = now if attempt == 1 else datetime.fromtimestamp(row["first_text_at"], UTC)
            kind = conversation_kind(row["kind"])
            next_due = reminder_time(first_text_at, attempt, now, hours, REMINDER_DELAYS_BY_KIND[kind])
            connection.execute(
                "UPDATE conversations SET opening_texts_sent = ?, first_text_at = ?, "
                "next_opening_due_at = ?, last_activity_at = ? WHERE lead_id = ?",
                (
                    attempt,
                    first_text_at.timestamp(),
                    None if next_due is None else next_due.timestamp(),
                    now.timestamp(),
                    lead_id,
                ),
            )
            _queue_text(connection, lead_id, row["phone"], "opening", body, now)
            return "queued"

    def close_quiet_conversations(self, now: datetime, quiet_for: timedelta) -> list[int]:
        """Hand quiet conversations to a person. Returns the lead ids it changed.

        Two kinds:
          - Every opening text went out and none got a reply: 'unreached'. The conversation stays
            open, so a reply that comes later still gets answered. The owner is alerted for a
            form lead, who asked recently and may pick up a call. Not for an old lead from a
            reactivation list: a campaign of 1,000 would email the owner hundreds of times about
            people who simply didn't answer, and the campaign report counts them instead.
          - The homeowner stopped replying partway: 'handed_off', and the bot stops.
        """
        cutoff = (now - quiet_for).timestamp()
        changed: list[int] = []
        with self._write_transaction() as connection:
            unreached = connection.execute(
                "SELECT conversations.lead_id, conversations.kind FROM conversations "
                "JOIN leads ON leads.lead_id = conversations.lead_id "
                "WHERE leads.status = 'following_up' AND conversations.closed = 0 "
                "AND conversations.next_opening_due_at IS NULL "
                "AND conversations.opening_texts_sent >= "
                "CASE conversations.kind WHEN 'reactivation' THEN ? ELSE ? END "
                "AND conversations.last_activity_at <= ?",
                (max_text_attempts("reactivation"), max_text_attempts("form"), cutoff),
            ).fetchall()
            for row in unreached:
                connection.execute(
                    "UPDATE leads SET status = 'unreached' WHERE lead_id = ?", (row["lead_id"],)
                )
                connection.execute(
                    "UPDATE conversations SET result = 'unreached' WHERE lead_id = ?", (row["lead_id"],)
                )
                if row["kind"] != "reactivation":
                    _add_owner_alert(connection, row["lead_id"], "no_reply_to_any_text", now)
                changed.append(row["lead_id"])
            stalled = connection.execute(
                "SELECT conversations.lead_id FROM conversations "
                "JOIN leads ON leads.lead_id = conversations.lead_id "
                "WHERE leads.status = 'in_conversation' AND conversations.closed = 0 "
                "AND conversations.last_activity_at <= ?",
                (cutoff,),
            ).fetchall()
            for row in stalled:
                connection.execute(
                    "UPDATE leads SET status = 'handed_off' WHERE lead_id = ?", (row["lead_id"],)
                )
                connection.execute(
                    "UPDATE conversations SET closed = 1, result = 'stopped_replying' WHERE lead_id = ?",
                    (row["lead_id"],),
                )
                _add_owner_alert(connection, row["lead_id"], "stopped_replying", now)
                changed.append(row["lead_id"])
        return changed

    # ---- incoming texts ----

    def record_incoming_text(
        self, phone: str, body: str, provider_message_id: str, now: datetime
    ) -> int | None:
        """Save a homeowner's text the moment it arrives. None if this exact message was saved before.

        Twilio sends a webhook again when it didn't get a quick answer, so one message can arrive
        twice. The UNIQUE provider_message_id makes the second arrival change nothing, instead of
        getting a second answer.
        """
        with self._write_transaction() as connection:
            lead = _latest_lead_with_a_conversation(connection, phone)
            cursor = connection.execute(
                "INSERT INTO texts (lead_id, phone, direction, kind, body, status, created_at, "
                "provider_message_id) VALUES (?, ?, 'in', 'homeowner', ?, 'received', ?, ?) "
                "ON CONFLICT (provider_message_id) DO NOTHING",
                (lead, phone, body, now.timestamp(), provider_message_id),
            )
            return cursor.lastrowid if cursor.rowcount == 1 else None

    def claim_incoming_text(self, now: datetime) -> IncomingText | None:
        """Take the oldest waiting incoming text, mark it 'handling', and load its conversation.

        One text per number at a time, in the order they arrived. A number with a text still
        being handled is skipped, and so is any text with an older one from the same number
        still waiting. Two quick texts are then answered in order, the second seeing what the
        first changed.
        """
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT text_id, phone, body, tries, lead_id FROM texts AS waiting "
                "WHERE direction = 'in' AND status = 'received' "
                "AND (not_before IS NULL OR not_before <= ?) "
                "AND NOT EXISTS (SELECT 1 FROM texts AS other WHERE other.direction = 'in' "
                "  AND other.phone = waiting.phone AND (other.status = 'handling' "
                "  OR (other.status = 'received' AND other.text_id < waiting.text_id))) "
                "ORDER BY text_id LIMIT 1",
                (now.timestamp(),),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE texts SET status = 'handling', tries = tries + 1 WHERE text_id = ?", (row["text_id"],)
            )
            loaded = None if row["lead_id"] is None else _load_conversation(connection, row["lead_id"])
            return IncomingText(
                text_id=row["text_id"],
                phone=row["phone"],
                body=row["body"],
                tries=row["tries"] + 1,
                conversation=None if loaded is None else loaded[0],
                closed=False if loaded is None else loaded[1],
                on_do_not_call=_is_on_do_not_call(connection, row["phone"]),
                history=[]
                if row["lead_id"] is None
                else _history(connection, row["lead_id"], row["text_id"]),
            )

    def retry_incoming_later(self, text_id: int, not_before: datetime) -> None:
        """Put a claimed text back in the queue, not to be tried again before `not_before`."""
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE texts SET status = 'received', not_before = ? "
                "WHERE text_id = ? AND status = 'handling'",
                (not_before.timestamp(), text_id),
            )

    def give_up_on_incoming(self, text_id: int, error: str, now: datetime) -> None:
        """A text that failed every try. It's marked handled so it can't block the number's later
        texts, and the owner is told, because the homeowner is waiting on an answer."""
        with self._write_transaction() as connection:
            row = connection.execute("SELECT lead_id FROM texts WHERE text_id = ?", (text_id,)).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE texts SET status = 'handled', note = ? WHERE text_id = ?",
                (f"gave up: {error}"[:500], text_id),
            )
            _add_owner_alert(connection, row["lead_id"], "bot_could_not_answer_a_text", now)

    def requeue_interrupted_incoming(self) -> int:
        """At worker start: texts left 'handling' by a worker that stopped go back in the queue.

        Answering one again is safe. Nothing about it was saved, because save_turn is one
        transaction, and a booking made again for the same lead finds the calendar event already
        there (booking.py) instead of making a second.
        """
        with self._write_transaction() as connection:
            cursor = connection.execute(
                "UPDATE texts SET status = 'received' WHERE direction = 'in' AND status = 'handling'"
            )
            return cursor.rowcount

    def save_turn(self, text_id: int, lead_id: int, outcome: TurnOutcome, now: datetime) -> None:
        """Apply everything one homeowner text changed, in one transaction.

        The incoming text is marked handled, the conversation moves on, answers and any booking
        are saved, and the replies are queued, all together. A crash can't leave a booking
        without its confirmation text, or a reply queued from a stage that was never saved.
        """
        with self._write_transaction() as connection:
            lead = connection.execute(
                "SELECT phone, status FROM leads WHERE lead_id = ?", (lead_id,)
            ).fetchone()
            if lead is None:
                raise ValueError(f"no lead {lead_id}")
            note = "; ".join(outcome.notes) or None
            connection.execute(
                "UPDATE texts SET status = 'handled', note = ? WHERE text_id = ?", (note, text_id)
            )
            # Any reply from the homeowner stops the reminders.
            connection.execute(
                "UPDATE conversations SET stage = ?, offered_slots = ?, unclear_in_a_row = ?, "
                "slot_taken_count = ?, next_opening_due_at = NULL, last_activity_at = ?, "
                "closed = MAX(closed, ?), result = COALESCE(?, result) WHERE lead_id = ?",
                (
                    outcome.stage,
                    json.dumps([[slot.id, slot.label] for slot in outcome.offered_slots]),
                    outcome.unclear_in_a_row,
                    outcome.slot_taken_count,
                    now.timestamp(),
                    1 if outcome.close_conversation else 0,
                    outcome.result,
                    lead_id,
                ),
            )
            for question, answer in outcome.answers.items():
                connection.execute(
                    "INSERT INTO lead_answers (lead_id, question, answer, recorded_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (lead_id, question) DO UPDATE SET answer = excluded.answer, "
                    "recorded_at = excluded.recorded_at",
                    (lead_id, question, answer, now.timestamp()),
                )
            if outcome.booking is not None:
                # OR REPLACE: booking.py makes at most one calendar event per lead, and a retry
                # that finds it confirms the same time, so a second row would only repeat it.
                connection.execute(
                    "INSERT OR REPLACE INTO bookings (lead_id, starts_at, slot_label, booked_at) "
                    "VALUES (?, ?, ?, ?)",
                    (lead_id, outcome.booking.starts_at.timestamp(), outcome.booking.label, now.timestamp()),
                )
            # Only a lead the bot is still working on changes status. One that opted out stays
            # opted out, and one that was booked stays booked whatever comes after.
            if outcome.lead_status is not None:
                connection.execute(
                    "UPDATE leads SET status = ? WHERE lead_id = ? "
                    "AND status IN ('following_up', 'in_conversation', 'unreached')",
                    (outcome.lead_status, lead_id),
                )
            else:
                connection.execute(
                    "UPDATE leads SET status = 'in_conversation' WHERE lead_id = ? "
                    "AND status IN ('following_up', 'unreached')",
                    (lead_id,),
                )
            if outcome.do_not_contact_source is not None:
                if lead["status"] == "booked":
                    # The inspection stands, but whoever confirms it mustn't text them.
                    _add_owner_alert(connection, lead_id, "opted_out_after_booking", now)
                _add_to_do_not_call(connection, lead["phone"], outcome.do_not_contact_source, now)
            else:
                for reply in outcome.replies:
                    _queue_text(connection, lead_id, lead["phone"], "reply", reply, now)
            # Queued after the do-not-call entry, which cancels every other queued text to the number.
            if outcome.stop_confirmation is not None:
                _queue_text(
                    connection, lead_id, lead["phone"], "stop_confirmation", outcome.stop_confirmation, now
                )
            for reason in outcome.owner_alerts:
                _add_owner_alert(connection, lead_id, reason, now, text_id=text_id)

    def finish_incoming_outside_conversation(
        self,
        text_id: int,
        now: datetime,
        opt_out_source: str | None = None,
        confirmation: str | None = None,
        alert: str | None = None,
    ) -> None:
        """A text the bot doesn't answer with the script: from a number with no conversation, from
        a number that opted out, or to a conversation a person has taken over.

        It's marked handled. An opt-out in it is still honored, with `confirmation` as the one text
        sent back (None when Twilio already confirmed it). `alert` tells the owner someone texted.
        """
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT lead_id, phone FROM texts WHERE text_id = ?", (text_id,)
            ).fetchone()
            if row is None:
                return
            connection.execute("UPDATE texts SET status = 'handled' WHERE text_id = ?", (text_id,))
            if opt_out_source is not None:
                _add_to_do_not_call(connection, row["phone"], opt_out_source, now)
                if confirmation is not None and row["lead_id"] is not None:
                    _queue_text(
                        connection, row["lead_id"], row["phone"], "stop_confirmation", confirmation, now
                    )
            if alert is not None:
                _add_owner_alert(connection, row["lead_id"], alert, now, text_id=text_id)

    # ---- outgoing texts ----

    def claim_queued_text(self) -> OutgoingText | None:
        """Take the oldest queued text and mark it 'sending', so it's sent once."""
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT text_id, lead_id, phone, kind, body, created_at, "
                "EXISTS (SELECT 1 FROM do_not_call WHERE do_not_call.phone = texts.phone) AS on_do_not_call "
                "FROM texts WHERE direction = 'out' AND status = 'queued' ORDER BY text_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE texts SET status = 'sending', tries = tries + 1 WHERE text_id = ?", (row["text_id"],)
            )
            return OutgoingText(
                text_id=row["text_id"],
                lead_id=row["lead_id"],
                phone=row["phone"],
                kind=row["kind"],
                body=row["body"],
                created_at=datetime.fromtimestamp(row["created_at"], UTC),
                on_do_not_call=bool(row["on_do_not_call"]),
            )

    def mark_text_sent(self, text_id: int, provider_message_id: str, now: datetime) -> None:
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE texts SET status = 'sent', provider_message_id = ?, sent_at = ? "
                "WHERE text_id = ? AND status = 'sending'",
                (provider_message_id, now.timestamp(), text_id),
            )
            # Twilio may have reported on it already, between taking it and our recording it.
            _apply_delivery_report(connection, provider_message_id, now)

    def return_text_to_queue(self, text_id: int) -> None:
        """Undo a claim: the send never reached the provider (our credentials were refused).
        The try isn't counted, since the text itself was never the problem."""
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE texts SET status = 'queued', tries = MAX(tries - 1, 0) "
                "WHERE text_id = ? AND status = 'sending'",
                (text_id,),
            )

    def record_delivery_report(
        self, provider_message_id: str, status: str, error_code: str | None, now: datetime
    ) -> None:
        """Save one of Twilio's delivery reports and apply it to the text it's about.

        Reports can arrive out of order and more than once, so a final status ('delivered',
        'undelivered', 'failed') is never replaced by a later in-between one.
        """
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT INTO delivery_reports (provider_message_id, status, error_code, received_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT (provider_message_id) DO UPDATE SET "
                "status = excluded.status, error_code = excluded.error_code, "
                "received_at = excluded.received_at "
                "WHERE delivery_reports.status NOT IN ('delivered', 'undelivered', 'failed')",
                (provider_message_id, status, error_code, now.timestamp()),
            )
            _apply_delivery_report(connection, provider_message_id, now)

    def mark_text_failed(self, text_id: int, error: str, now: datetime) -> None:
        """The provider refused the text. The owner is alerted, because the homeowner is now
        waiting on an answer that never came."""
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT lead_id FROM texts WHERE text_id = ? AND status = 'sending'", (text_id,)
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE texts SET status = 'failed', note = ? WHERE text_id = ?", (error[:500], text_id)
            )
            _add_owner_alert(connection, row["lead_id"], "text_failed_to_send", now)

    def cancel_text(self, text_id: int, reason: str) -> None:
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE texts SET status = 'cancelled', note = ? WHERE text_id = ? AND status = 'sending'",
                (reason, text_id),
            )

    def mark_text_unknown(self, text_id: int, error: str, now: datetime) -> None:
        """The send failed in a way that leaves it unclear whether the provider took the text
        (a timeout, a dropped connection). Not retried, since it may have gone; the owner checks."""
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT lead_id FROM texts WHERE text_id = ? AND status = 'sending'", (text_id,)
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE texts SET status = 'unknown', note = ? WHERE text_id = ?", (error[:500], text_id)
            )
            _add_owner_alert(connection, row["lead_id"], "text_may_not_have_sent", now)

    def reschedule_opening(self, text_id: int, lead_id: int, due_at: datetime) -> None:
        """An opening text found outside texting hours at send time (it sat queued while the
        worker was down). It's cancelled and its attempt is due again at `due_at`, so the same
        text is queued afresh when the window opens."""
        with self._write_transaction() as connection:
            cursor = connection.execute(
                "UPDATE texts SET status = 'cancelled', note = 'outside texting hours at send time' "
                "WHERE text_id = ? AND status = 'sending'",
                (text_id,),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    "UPDATE conversations SET opening_texts_sent = opening_texts_sent - 1, "
                    "next_opening_due_at = ? WHERE lead_id = ? AND closed = 0 AND opening_texts_sent > 0",
                    (due_at.timestamp(), lead_id),
                )

    def mark_interrupted_sends_unknown(self, now: datetime) -> int:
        """At worker start: texts left 'sending' by a worker that stopped.

        Twilio may or may not have taken each one, and sending it again could deliver it twice.
        So none is resent: each becomes 'unknown' and the owner is alerted to check.
        """
        with self._write_transaction() as connection:
            rows = connection.execute(
                "SELECT text_id, lead_id FROM texts WHERE direction = 'out' AND status = 'sending'"
            ).fetchall()
            for row in rows:
                connection.execute("UPDATE texts SET status = 'unknown' WHERE text_id = ?", (row["text_id"],))
                _add_owner_alert(connection, row["lead_id"], "text_may_not_have_sent", now)
            return len(rows)

    # ---- do-not-call ----

    def add_to_do_not_call(self, phone: str, source: str, now: datetime) -> None:
        with self._write_transaction() as connection:
            _add_to_do_not_call(connection, phone, source, now)

    def is_on_do_not_call(self, phone: str) -> bool:
        with self._read() as connection:
            return _is_on_do_not_call(connection, phone)

    # ---- reading, for the simulator, reports and /health ----

    def conversation(self, lead_id: int) -> ConversationState | None:
        with self._read() as connection:
            loaded = _load_conversation(connection, lead_id)
        return None if loaded is None else loaded[0]

    def conversation_result(self, lead_id: int) -> str | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT result FROM conversations WHERE lead_id = ?", (lead_id,)
            ).fetchone()
        return None if row is None or row["result"] is None else str(row["result"])

    def transcript(self, lead_id: int) -> list[TranscriptLine]:
        """Every text to and from this lead, in order."""
        with self._read() as connection:
            rows = connection.execute(
                "SELECT direction, kind, body, status FROM texts WHERE lead_id = ? ORDER BY text_id",
                (lead_id,),
            ).fetchall()
        return [TranscriptLine(row["direction"], row["kind"], row["body"], row["status"]) for row in rows]

    def lead_answers(self, lead_id: int) -> dict[str, str]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT question, answer FROM lead_answers WHERE lead_id = ?", (lead_id,)
            ).fetchall()
        return {row["question"]: row["answer"] for row in rows}

    def booking_label(self, lead_id: int) -> str | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT slot_label FROM bookings WHERE lead_id = ?", (lead_id,)
            ).fetchone()
        return None if row is None else str(row["slot_label"])

    def owner_alerts(self) -> list[OwnerAlert]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT alert_id, lead_id, reason, created_at FROM owner_alerts ORDER BY alert_id"
            ).fetchall()
        return [
            OwnerAlert(
                alert_id=row["alert_id"],
                lead_id=row["lead_id"],
                reason=row["reason"],
                created_at=datetime.fromtimestamp(row["created_at"], UTC),
            )
            for row in rows
        ]

    # ---- owner alert emails (owner_alerts.py) ----

    def alerts_to_email(self, now: datetime, limit: int = 10) -> list[AlertForEmail]:
        """Owner alerts not emailed yet whose retry time (after a failure) has come, oldest first."""
        with self._read() as connection:
            rows = connection.execute(
                "SELECT owner_alerts.alert_id, owner_alerts.lead_id, owner_alerts.reason, "
                "owner_alerts.created_at, owner_alerts.call_id, COALESCE(alert_emails.tries, 0) AS tries, "
                "texts.phone AS text_phone, texts.body AS text_body, "
                "calls.phone AS call_phone, calls.message AS call_message "
                "FROM owner_alerts "
                "LEFT JOIN alert_emails ON alert_emails.alert_id = owner_alerts.alert_id "
                "LEFT JOIN texts ON texts.text_id = owner_alerts.text_id "
                "LEFT JOIN calls ON calls.call_id = owner_alerts.call_id "
                "WHERE alert_emails.sent_at IS NULL "
                "AND (alert_emails.not_before IS NULL OR alert_emails.not_before <= ?) "
                "ORDER BY owner_alerts.alert_id LIMIT ?",
                (now.timestamp(), limit),
            ).fetchall()
            return [
                AlertForEmail(
                    alert_id=row["alert_id"],
                    reason=row["reason"],
                    created_at=datetime.fromtimestamp(row["created_at"], UTC),
                    tries=row["tries"],
                    lead=None if row["lead_id"] is None else _lead_summary(connection, row["lead_id"]),
                    text_phone=row["text_phone"],
                    text_body=row["text_body"],
                    call_phone=row["call_phone"],
                    call_message=row["call_message"],
                    call_transcript=()
                    if row["call_id"] is None
                    else tuple(_call_transcript(connection, row["call_id"])),
                )
                for row in rows
            ]

    def mark_alert_emailed(self, alert_id: int, now: datetime) -> None:
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT INTO alert_emails (alert_id, sent_at, tries) VALUES (?, ?, 0) "
                "ON CONFLICT (alert_id) DO UPDATE SET sent_at = excluded.sent_at, last_error = NULL",
                (alert_id, now.timestamp()),
            )

    def retry_alert_email_later(self, alert_id: int, error: str, not_before: datetime) -> None:
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT INTO alert_emails (alert_id, tries, not_before, last_error) VALUES (?, 1, ?, ?) "
                "ON CONFLICT (alert_id) DO UPDATE SET tries = alert_emails.tries + 1, "
                "not_before = excluded.not_before, last_error = excluded.last_error",
                (alert_id, not_before.timestamp(), error[:500]),
            )

    # ---- owner alert texts (owner_alerts.py) ----

    def alerts_to_text(
        self, now: datetime, reasons: tuple[str, ...], max_age: timedelta, limit: int = 10
    ) -> list[AlertForText]:
        """Alerts with one of `reasons`, raised within `max_age`, not texted or given up on yet,
        and past any retry time. Oldest first.

        The age limit matters on the first start with texting turned on: without it, every old
        urgent alert in the database would be texted to the owner at once.
        """
        if not reasons:
            return []
        placeholders = ", ".join("?" for _ in reasons)
        with self._read() as connection:
            rows = connection.execute(
                "SELECT owner_alerts.alert_id, owner_alerts.reason, COALESCE(alert_texts.tries, 0) AS tries, "
                "leads.full_name, leads.phone, leads.address, bookings.slot_label, "
                "calls.phone AS call_phone, calls.message AS call_message "
                "FROM owner_alerts "
                "LEFT JOIN alert_texts ON alert_texts.alert_id = owner_alerts.alert_id "
                "LEFT JOIN leads ON leads.lead_id = owner_alerts.lead_id "
                "LEFT JOIN bookings ON bookings.lead_id = owner_alerts.lead_id "
                "LEFT JOIN calls ON calls.call_id = owner_alerts.call_id "
                f"WHERE owner_alerts.reason IN ({placeholders}) "
                "AND owner_alerts.created_at >= ? "
                "AND alert_texts.sent_at IS NULL "
                "AND COALESCE(alert_texts.gave_up, 0) = 0 "
                "AND (alert_texts.not_before IS NULL OR alert_texts.not_before <= ?) "
                "ORDER BY owner_alerts.alert_id LIMIT ?",
                (*reasons, (now - max_age).timestamp(), now.timestamp(), limit),
            ).fetchall()
        return [
            AlertForText(
                alert_id=row["alert_id"],
                reason=row["reason"],
                tries=row["tries"],
                full_name=row["full_name"],
                phone=row["phone"],
                address=row["address"],
                booking=row["slot_label"],
                call_phone=row["call_phone"],
                call_message=row["call_message"],
            )
            for row in rows
        ]

    def mark_alert_texted(self, alert_id: int, provider_message_id: str, now: datetime) -> None:
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT INTO alert_texts (alert_id, sent_at, provider_message_id) VALUES (?, ?, ?) "
                "ON CONFLICT (alert_id) DO UPDATE SET sent_at = excluded.sent_at, "
                "provider_message_id = excluded.provider_message_id, last_error = NULL",
                (alert_id, now.timestamp(), provider_message_id),
            )

    def give_up_alert_text(self, alert_id: int, error: str) -> None:
        """Never text this alert: the provider refused it, or it may have gone already (a resend
        could reach the owner twice). The email still carries it."""
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT INTO alert_texts (alert_id, tries, gave_up, last_error) VALUES (?, 1, 1, ?) "
                "ON CONFLICT (alert_id) DO UPDATE SET tries = alert_texts.tries + 1, gave_up = 1, "
                "last_error = excluded.last_error",
                (alert_id, error[:500]),
            )

    def retry_alert_text_later(self, alert_id: int, error: str, not_before: datetime) -> None:
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT INTO alert_texts (alert_id, tries, not_before, last_error) VALUES (?, 1, ?, ?) "
                "ON CONFLICT (alert_id) DO UPDATE SET tries = alert_texts.tries + 1, "
                "not_before = excluded.not_before, last_error = excluded.last_error",
                (alert_id, not_before.timestamp(), error[:500]),
            )

    # ---- the monthly report (report.py) ----

    def monthly_numbers(self, start: datetime, end: datetime) -> MonthlyNumbers:
        """Everything counted for [start, end): leads by when they arrived, bookings by when
        they were made, calls by when they came in, campaign contacts by when they were first
        texted, alerts and opt-outs by when they happened."""
        window = (start.timestamp(), end.timestamp())
        with self._read() as connection:

            def count(sql: str, parameters: tuple[object, ...] = window) -> int:
                return int(connection.execute(sql, parameters).fetchone()[0])

            form_lead = (
                "leads.intake != 'phone_call' AND NOT EXISTS (SELECT 1 FROM campaign_contacts "
                "WHERE campaign_contacts.lead_id = leads.lead_id)"
            )
            replied = (
                "EXISTS (SELECT 1 FROM texts WHERE texts.lead_id = leads.lead_id AND texts.direction = 'in')"
            )
            booked = "EXISTS (SELECT 1 FROM bookings WHERE bookings.lead_id = leads.lead_id)"
            form_leads_in_window = (
                "SELECT COUNT(*) FROM leads WHERE leads.created_at >= ? AND leads.created_at < ? "
                f"AND {form_lead}"
            )
            calls_in_window = "SELECT COUNT(*) FROM calls WHERE started_at >= ? AND started_at < ?"
            alerts_in_window = "SELECT COUNT(*) FROM owner_alerts WHERE created_at >= ? AND created_at < ?"
            started = (
                "FROM campaign_contacts JOIN leads ON leads.lead_id = campaign_contacts.lead_id "
                "WHERE campaign_contacts.started_at >= ? AND campaign_contacts.started_at < ?"
            )
            source_rows = connection.execute(
                f"SELECT {_LEAD_SOURCE} AS source, COUNT(*) AS total FROM bookings "
                "JOIN leads ON leads.lead_id = bookings.lead_id "
                "WHERE bookings.booked_at >= ? AND bookings.booked_at < ? GROUP BY source",
                window,
            ).fetchall()
            recent_rows = connection.execute(
                f"SELECT leads.full_name, bookings.slot_label, {_LEAD_SOURCE} AS source FROM bookings "
                "JOIN leads ON leads.lead_id = bookings.lead_id "
                "WHERE bookings.booked_at >= ? AND bookings.booked_at < ? "
                "ORDER BY bookings.booked_at DESC LIMIT 3",
                window,
            ).fetchall()
            return MonthlyNumbers(
                form_leads=count(form_leads_in_window),
                form_leads_texted=count(f"{form_leads_in_window} AND leads.intake = 'text'"),
                form_leads_replied=count(f"{form_leads_in_window} AND leads.intake = 'text' AND {replied}"),
                form_leads_booked=count(f"{form_leads_in_window} AND {booked}"),
                bookings=sum(row["total"] for row in source_rows),
                bookings_by_source={row["source"]: row["total"] for row in source_rows},
                calls_answered=count(calls_in_window),
                calls_booked=count(f"{calls_in_window} AND result = 'booked'"),
                calls_messages=count(f"{calls_in_window} AND result = 'message'"),
                calls_hung_up=count(f"{calls_in_window} AND result = 'hung_up'"),
                reactivation_started=count(f"SELECT COUNT(*) {started}"),
                reactivation_replied=count(f"SELECT COUNT(*) {started} AND {replied}"),
                reactivation_booked=count(f"SELECT COUNT(*) {started} AND {booked}"),
                urgent_leaks=count(f"{alerts_in_window} AND reason = 'URGENT_active_leak'"),
                alerts_for_owner=count(f"{alerts_in_window} AND reason != 'inspection_booked'"),
                opted_out=count("SELECT COUNT(*) FROM do_not_call WHERE added_at >= ? AND added_at < ?"),
                recent_bookings=[
                    BookedInspection(row["full_name"], row["slot_label"], row["source"])
                    for row in recent_rows
                ],
            )

    def earliest_activity(self) -> datetime | None:
        """When the first lead or call arrived, or None in an empty database. A month that ended
        before this gets no report: a new install mustn't email the owner an empty one."""
        with self._read() as connection:
            row = connection.execute(
                "SELECT MIN(first) FROM (SELECT MIN(created_at) AS first FROM leads "
                "UNION ALL SELECT MIN(started_at) FROM calls)"
            ).fetchone()
        return None if row[0] is None else datetime.fromtimestamp(row[0], UTC)

    def monthly_report_sent(self, month: str) -> bool:
        with self._read() as connection:
            row = connection.execute("SELECT 1 FROM monthly_reports WHERE month = ?", (month,)).fetchone()
        return row is not None

    def mark_monthly_report_sent(self, month: str, now: datetime) -> None:
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO monthly_reports (month, sent_at) VALUES (?, ?)",
                (month, now.timestamp()),
            )

    def record_worker_heartbeat(self, now: datetime) -> None:
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT INTO worker_heartbeat (only_row, beat_at) VALUES (1, ?) "
                "ON CONFLICT (only_row) DO UPDATE SET beat_at = excluded.beat_at",
                (now.timestamp(),),
            )

    def last_worker_heartbeat(self) -> datetime | None:
        """When the worker last reported in, or None if it never has."""
        with self._read() as connection:
            row = connection.execute("SELECT beat_at FROM worker_heartbeat").fetchone()
        return None if row is None else datetime.fromtimestamp(row["beat_at"], tz=UTC)

    def counts(self) -> dict[str, dict[str, int]]:
        """Leads by status, and texts by status in each direction, for /health."""
        with self._read() as connection:
            lead_rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM leads GROUP BY status"
            ).fetchall()
            text_rows = connection.execute(
                "SELECT direction, status, COUNT(*) AS total FROM texts GROUP BY direction, status"
            ).fetchall()
        return {
            "leads": {row["status"]: row["total"] for row in lead_rows},
            "texts_in": {row["status"]: row["total"] for row in text_rows if row["direction"] == "in"},
            "texts_out": {row["status"]: row["total"] for row in text_rows if row["direction"] == "out"},
        }


# ---- helpers that run inside a caller's transaction ----


def _lead_summary(connection: sqlite3.Connection, lead_id: int) -> LeadSummary | None:
    """What an alert email says about the lead: who, where, the answers and every text."""
    lead = connection.execute(
        "SELECT full_name, phone, address, problem, intake FROM leads WHERE lead_id = ?", (lead_id,)
    ).fetchone()
    if lead is None:
        return None
    answers = connection.execute(
        "SELECT question, answer FROM lead_answers WHERE lead_id = ?", (lead_id,)
    ).fetchall()
    booking = connection.execute("SELECT slot_label FROM bookings WHERE lead_id = ?", (lead_id,)).fetchone()
    texts = connection.execute(
        "SELECT direction, kind, body, status FROM texts WHERE lead_id = ? ORDER BY text_id", (lead_id,)
    ).fetchall()
    campaign = connection.execute(
        "SELECT campaigns.name FROM campaign_contacts "
        "JOIN campaigns ON campaigns.campaign_id = campaign_contacts.campaign_id "
        "WHERE campaign_contacts.lead_id = ?",
        (lead_id,),
    ).fetchone()
    return LeadSummary(
        lead_id=lead_id,
        full_name=lead["full_name"],
        phone=lead["phone"],
        address=lead["address"],
        problem=lead["problem"],
        answers={row["question"]: row["answer"] for row in answers},
        booking=None if booking is None else str(booking["slot_label"]),
        transcript=[
            TranscriptLine(row["direction"], row["kind"], row["body"], row["status"]) for row in texts
        ],
        campaign=None if campaign is None else str(campaign["name"]),
        intake=lead["intake"],
    )


def _is_on_do_not_call(connection: sqlite3.Connection, phone: str) -> bool:
    return connection.execute("SELECT 1 FROM do_not_call WHERE phone = ?", (phone,)).fetchone() is not None


def _apply_delivery_report(connection: sqlite3.Connection, provider_message_id: str, now: datetime) -> None:
    """Move a sent text to its final delivery status, if a final report for it has arrived.

    Only a text still 'sent' changes, so a repeated report does nothing the second time. A text
    that didn't arrive alerts the owner: the homeowner is waiting on an answer they never got.
    """
    report = connection.execute(
        "SELECT status, error_code FROM delivery_reports WHERE provider_message_id = ?",
        (provider_message_id,),
    ).fetchone()
    if report is None or report["status"] not in FINAL_DELIVERY_STATUSES:
        return
    error_note = None if report["error_code"] is None else f"Twilio error {report['error_code']}"
    cursor = connection.execute(
        "UPDATE texts SET status = ?, note = COALESCE(?, note) "
        "WHERE provider_message_id = ? AND direction = 'out' AND status = 'sent'",
        (report["status"], error_note, provider_message_id),
    )
    if cursor.rowcount == 1 and report["status"] != "delivered":
        text = connection.execute(
            "SELECT lead_id FROM texts WHERE provider_message_id = ?", (provider_message_id,)
        ).fetchone()
        reason = f"text_not_delivered:{report['error_code'] or 'no_error_code'}"
        _add_owner_alert(connection, text["lead_id"], reason, now)


def _add_owner_alert(
    connection: sqlite3.Connection,
    lead_id: int | None,
    reason: str,
    now: datetime,
    text_id: int | None = None,
    call_id: int | None = None,
) -> None:
    connection.execute(
        "INSERT INTO owner_alerts (lead_id, reason, created_at, text_id, call_id) VALUES (?, ?, ?, ?, ?)",
        (lead_id, reason, now.timestamp(), text_id, call_id),
    )


def _queue_text(
    connection: sqlite3.Connection, lead_id: int, phone: str, kind: TextKind, body: str, now: datetime
) -> None:
    connection.execute(
        "INSERT INTO texts (lead_id, phone, direction, kind, body, status, created_at) "
        "VALUES (?, ?, 'out', ?, ?, 'queued', ?)",
        (lead_id, phone, kind, body, now.timestamp()),
    )


def _add_to_do_not_call(connection: sqlite3.Connection, phone: str, source: str, now: datetime) -> None:
    """Opt a number out: every text still queued to it is cancelled and its conversations stop.

    A lead that was already booked keeps that status: the inspection stands, there are just no
    more texts. The one text queued after this is the opt-out confirmation (save_turn).
    """
    connection.execute(
        "INSERT OR IGNORE INTO do_not_call (phone, added_at, source) VALUES (?, ?, ?)",
        (phone, now.timestamp(), source),
    )
    connection.execute(
        "UPDATE texts SET status = 'cancelled', note = 'number opted out' "
        "WHERE phone = ? AND direction = 'out' AND status = 'queued'",
        (phone,),
    )
    connection.execute(
        "UPDATE conversations SET closed = 1, next_opening_due_at = NULL, "
        "result = COALESCE(result, 'opted_out') "
        "WHERE lead_id IN (SELECT lead_id FROM leads WHERE phone = ?)",
        (phone,),
    )
    connection.execute(
        "UPDATE leads SET status = 'opted_out' WHERE phone = ? "
        # campaign_waiting: an opt-out from any conversation also stops a reactivation
        # campaign that hasn't texted this number yet.
        "AND status IN ('following_up', 'in_conversation', 'unreached', 'handed_off', 'campaign_waiting')",
        (phone,),
    )


def _known_caller(connection: sqlite3.Connection, phone: str, on_do_not_call: bool) -> KnownCaller | None:
    """The most recent lead with this number, or None. 'duplicate' leads are copies of another."""
    row = connection.execute(
        "SELECT leads.lead_id, leads.full_name, leads.address, bookings.slot_label, consents.given "
        "FROM leads LEFT JOIN bookings ON bookings.lead_id = leads.lead_id "
        "LEFT JOIN consents ON consents.lead_id = leads.lead_id "
        "WHERE leads.phone = ? AND leads.status != 'duplicate' ORDER BY leads.lead_id DESC LIMIT 1",
        (phone,),
    ).fetchone()
    if row is None:
        return None
    return KnownCaller(
        lead_id=row["lead_id"],
        full_name=row["full_name"],
        address=row["address"],
        booked_label=row["slot_label"],
        texts_allowed=row["given"] == 1 and not on_do_not_call,
    )


def _call_transcript(connection: sqlite3.Connection, call_id: int) -> list[CallTurnLine]:
    rows = connection.execute(
        "SELECT speaker, text FROM call_turns WHERE call_id = ? ORDER BY turn_id", (call_id,)
    ).fetchall()
    return [CallTurnLine(row["speaker"], row["text"]) for row in rows]


def _has_active_lead(connection: sqlite3.Connection, phone: str, except_lead_id: int | None = None) -> bool:
    """Whether this number has a conversation going (other than `except_lead_id`'s own)."""
    placeholders = ", ".join("?" for _ in ACTIVE_LEAD_STATUSES)
    row = connection.execute(
        f"SELECT 1 FROM leads WHERE phone = ? AND status IN ({placeholders}) AND lead_id IS NOT ?",
        (phone, *ACTIVE_LEAD_STATUSES, except_lead_id),
    ).fetchone()
    return row is not None


def _reason_to_leave_out(connection: sqlite3.Connection, phone: str) -> str | None:
    """Why a contact on an imported list can't join the campaign, or None if it can."""
    if _is_on_do_not_call(connection, phone):
        return "on the do-not-call list"
    if _has_active_lead(connection, phone):
        return "already in a conversation"
    in_a_campaign = connection.execute(
        "SELECT 1 FROM campaign_contacts JOIN leads ON leads.lead_id = campaign_contacts.lead_id "
        "WHERE leads.phone = ?",
        (phone,),
    ).fetchone()
    if in_a_campaign is not None:
        return "in an earlier campaign"
    return None


def _add_campaign_contact(
    connection: sqlite3.Connection,
    campaign_id: int,
    source_file: str,
    contact: ReactivationContact,
    now: datetime,
    hours: TextingHours,
) -> int:
    """One imported contact as a lead, its consent evidence, a waiting conversation and its
    campaign row. Returns the lead id."""
    cursor = connection.execute(
        "INSERT INTO leads (full_name, phone, address, problem, created_at, intake, status) "
        # 'not_sure': the list has no problem type, and the conversation asks about leaks anyway.
        "VALUES (?, ?, ?, 'not_sure', ?, 'text', 'campaign_waiting')",
        (contact.full_name, contact.phone, contact.address, now.timestamp()),
    )
    lead_id = cursor.lastrowid
    if lead_id is None:  # can't happen after an INSERT, but the type allows it
        raise RuntimeError("SQLite returned no id for the new lead")
    # The list gives a date, not a time. Midday keeps it on that date in any US timezone.
    consented_at = datetime.combine(contact.consented_on, time(12), tzinfo=hours.timezone)
    connection.execute(
        "INSERT INTO consents (lead_id, given, version, text_shown, ip_address, forwarded_for, "
        "user_agent, source, recorded_at, consented_at) VALUES (?, 1, ?, ?, ?, NULL, ?, ?, ?, ?)",
        (
            lead_id,
            IMPORTED_CONSENT_VERSION,
            contact.consent_wording,
            "not recorded (imported list)",
            f"imported from {source_file}",
            contact.consent_source,
            now.timestamp(),
            consented_at.timestamp(),
        ),
    )
    # No first text due yet: start_campaign_contacts sets it when the contact's turn comes.
    connection.execute(
        "INSERT INTO conversations (lead_id, stage, next_opening_due_at, last_activity_at, kind) "
        "VALUES (?, 'still_interested', NULL, ?, 'reactivation')",
        (lead_id, now.timestamp()),
    )
    connection.execute(
        "INSERT INTO campaign_contacts (lead_id, campaign_id) VALUES (?, ?)", (lead_id, campaign_id)
    )
    return lead_id


def _start_spacing(hours: TextingHours, daily_limit: int) -> timedelta:
    """The gap between two contacts of one campaign: the texting window shared evenly. With the
    8:00-20:30 window and 50 a day, one every 15 minutes."""
    window = datetime.combine(date.min, hours.latest) - datetime.combine(date.min, hours.earliest)
    return window / daily_limit


def _start_next_contact(connection: sqlite3.Connection, campaign_id: int, now: datetime) -> bool:
    """Start the campaign's oldest waiting contact that may still be texted, skipping (for good)
    any that may not. False when none could be started."""
    waiting = connection.execute(
        "SELECT campaign_contacts.lead_id, leads.phone, leads.status FROM campaign_contacts "
        "JOIN leads ON leads.lead_id = campaign_contacts.lead_id "
        "WHERE campaign_contacts.campaign_id = ? AND campaign_contacts.started_at IS NULL "
        "AND campaign_contacts.skipped IS NULL ORDER BY campaign_contacts.lead_id",
        (campaign_id,),
    ).fetchall()
    for contact in waiting:
        lead_id = contact["lead_id"]
        if contact["status"] != "campaign_waiting":
            # Usually an opt-out from another conversation since the import (_add_to_do_not_call).
            _skip_contact(connection, lead_id, f"was {contact['status']} before its turn")
            continue
        if _is_on_do_not_call(connection, contact["phone"]):
            # A do-not-call row added by hand doesn't change lead statuses, so check it here too.
            _skip_contact(connection, lead_id, "on the do-not-call list")
            connection.execute("UPDATE leads SET status = 'opted_out' WHERE lead_id = ?", (lead_id,))
            continue
        if _has_active_lead(connection, contact["phone"], except_lead_id=lead_id):
            # They filled in the form since the import: one conversation per number.
            _skip_contact(connection, lead_id, "already in a conversation")
            connection.execute("UPDATE leads SET status = 'duplicate' WHERE lead_id = ?", (lead_id,))
            continue
        connection.execute("UPDATE leads SET status = 'following_up' WHERE lead_id = ?", (lead_id,))
        connection.execute(
            "UPDATE conversations SET next_opening_due_at = ?, last_activity_at = ? WHERE lead_id = ?",
            (now.timestamp(), now.timestamp(), lead_id),
        )
        connection.execute(
            "UPDATE campaign_contacts SET started_at = ? WHERE lead_id = ?", (now.timestamp(), lead_id)
        )
        connection.execute(
            "UPDATE campaigns SET last_started_at = ? WHERE campaign_id = ?", (now.timestamp(), campaign_id)
        )
        return True
    return False


def _skip_contact(connection: sqlite3.Connection, lead_id: int, reason: str) -> None:
    connection.execute("UPDATE campaign_contacts SET skipped = ? WHERE lead_id = ?", (reason, lead_id))


def _stop_opening_texts(
    connection: sqlite3.Connection, lead_id: int, result: str, lead_status: LeadStatus
) -> None:
    connection.execute(
        "UPDATE conversations SET next_opening_due_at = NULL, closed = 1, result = ? WHERE lead_id = ?",
        (result, lead_id),
    )
    connection.execute("UPDATE leads SET status = ? WHERE lead_id = ?", (lead_status, lead_id))


def _latest_lead_with_a_conversation(connection: sqlite3.Connection, phone: str) -> int | None:
    """The lead a text from this number belongs to: the newest one the bot started texting."""
    row = connection.execute(
        "SELECT leads.lead_id FROM leads JOIN conversations ON conversations.lead_id = leads.lead_id "
        "WHERE leads.phone = ? ORDER BY leads.lead_id DESC LIMIT 1",
        (phone,),
    ).fetchone()
    return None if row is None else int(row["lead_id"])


def _stage(value: str) -> Stage:
    # A loop rather than `value in STAGES` plus a cast: this way the type checker can see that
    # only a known stage is ever returned.
    for stage in STAGES:
        if stage == value:
            return stage
    raise ValueError(f"unknown conversation stage {value!r} in the database")


def _load_conversation(connection: sqlite3.Connection, lead_id: int) -> tuple[ConversationState, bool] | None:
    """The conversation as saved, and whether it's closed. None if the lead has no conversation."""
    row = connection.execute(
        "SELECT conversations.stage, conversations.offered_slots, conversations.unclear_in_a_row, "
        "conversations.slot_taken_count, conversations.closed, leads.phone, leads.full_name, "
        "leads.address, leads.problem, bookings.slot_label "
        "FROM conversations JOIN leads ON leads.lead_id = conversations.lead_id "
        "LEFT JOIN bookings ON bookings.lead_id = conversations.lead_id "
        "WHERE conversations.lead_id = ?",
        (lead_id,),
    ).fetchone()
    if row is None:
        return None
    answers = connection.execute(
        "SELECT question, answer FROM lead_answers WHERE lead_id = ?", (lead_id,)
    ).fetchall()
    state = ConversationState(
        lead_id=lead_id,
        phone=row["phone"],
        full_name=row["full_name"],
        address=row["address"],
        problem=row["problem"],
        stage=_stage(row["stage"]),
        offered_slots=tuple(
            Slot(id=slot_id, label=label) for slot_id, label in json.loads(row["offered_slots"])
        ),
        unclear_in_a_row=row["unclear_in_a_row"],
        slot_taken_count=row["slot_taken_count"],
        booked_label=row["slot_label"],
        answers={answer["question"]: answer["answer"] for answer in answers},
    )
    return state, bool(row["closed"])


def _history(connection: sqlite3.Connection, lead_id: int, before_text_id: int) -> list[Turn]:
    """The conversation before one text, as the writer sees it. Texts the homeowner never got
    (cancelled, refused, or reported undelivered) are left out."""
    rows = connection.execute(
        "SELECT direction, body FROM texts WHERE lead_id = ? AND text_id < ? "
        "AND status NOT IN ('cancelled', 'failed', 'undelivered') ORDER BY text_id",
        (lead_id, before_text_id),
    ).fetchall()
    return [Turn("user" if row["direction"] == "in" else "assistant", row["body"]) for row in rows]
