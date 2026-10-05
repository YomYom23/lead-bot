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

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from .consent import ConsentEvidence
from .leads import IntakeDecision, NewLead, decide_intake
from .slots import Slot
from .text_questions import STAGES, Stage
from .timing import MAX_TEXT_ATTEMPTS, TextingHours, is_allowed, next_allowed_time, reminder_time
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
    recorded_at   REAL NOT NULL
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
    last_activity_at    REAL NOT NULL               -- the last text either way
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

CREATE TABLE IF NOT EXISTS owner_alerts (
    alert_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER REFERENCES leads (lead_id),  -- NULL: about a number with no lead
    reason     TEXT NOT NULL,
    created_at REAL NOT NULL
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
#           the provider;  failed -> refused;  cancelled -> never sent (the number opted out);
#           unknown -> a worker stopped mid-send, so it may or may not have gone
OutgoingStatus = Literal["queued", "sending", "sent", "failed", "cancelled", "unknown"]

# What queue_opening_text did:
#   queued      -> the text is queued to send now
#   rescheduled -> outside texting hours; moved to the next opening of the window
#   stopped     -> no consent, or the number opted out; nothing more will be started
#   not_due     -> nothing to do (already queued by another pass, or the lead replied meanwhile)
OpeningResult = Literal["queued", "rescheduled", "stopped", "not_due"]

# A second form from a number in one of these gets no second conversation.
ACTIVE_LEAD_STATUSES = ("following_up", "in_conversation", "booked")

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


@dataclass(frozen=True)
class DueOpening:
    """An opening text whose time has come. The worker words it; queue_opening_text re-checks it."""

    lead_id: int
    attempt: int  # 1 = the first text, 2 and up = reminders
    full_name: str
    address: str


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


class Store:
    def __init__(self, database_path: Path | str) -> None:
        self._path = str(database_path)
        connection = self._connect()
        try:
            # WAL lets the web server write while the worker reads, instead of one blocking.
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
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

    # ---- opening texts and reminders ----

    def due_openings(self, now: datetime) -> list[DueOpening]:
        """Opening texts and reminders whose time has come, most overdue first."""
        with self._read() as connection:
            rows = connection.execute(
                "SELECT conversations.lead_id, conversations.opening_texts_sent, leads.full_name, "
                "leads.address FROM conversations JOIN leads ON leads.lead_id = conversations.lead_id "
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
                "conversations.next_opening_due_at, conversations.closed, leads.status, leads.phone, "
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
            next_due = reminder_time(first_text_at, attempt, now, hours)
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
            open, so a reply that comes later still gets answered.
          - The homeowner stopped replying partway: 'handed_off', and the bot stops.
        """
        cutoff = (now - quiet_for).timestamp()
        changed: list[int] = []
        with self._write_transaction() as connection:
            unreached = connection.execute(
                "SELECT conversations.lead_id FROM conversations "
                "JOIN leads ON leads.lead_id = conversations.lead_id "
                "WHERE leads.status = 'following_up' AND conversations.closed = 0 "
                "AND conversations.next_opening_due_at IS NULL AND conversations.opening_texts_sent >= ? "
                "AND conversations.last_activity_at <= ?",
                (MAX_TEXT_ATTEMPTS, cutoff),
            ).fetchall()
            for row in unreached:
                connection.execute(
                    "UPDATE leads SET status = 'unreached' WHERE lead_id = ?", (row["lead_id"],)
                )
                connection.execute(
                    "UPDATE conversations SET result = 'unreached' WHERE lead_id = ?", (row["lead_id"],)
                )
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
                _add_owner_alert(connection, lead_id, reason, now)

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
                _add_owner_alert(connection, row["lead_id"], alert, now)

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


def _is_on_do_not_call(connection: sqlite3.Connection, phone: str) -> bool:
    return connection.execute("SELECT 1 FROM do_not_call WHERE phone = ?", (phone,)).fetchone() is not None


def _add_owner_alert(connection: sqlite3.Connection, lead_id: int | None, reason: str, now: datetime) -> None:
    connection.execute(
        "INSERT INTO owner_alerts (lead_id, reason, created_at) VALUES (?, ?, ?)",
        (lead_id, reason, now.timestamp()),
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
        "AND status IN ('following_up', 'in_conversation', 'unreached', 'handed_off')",
        (phone,),
    )


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
    """The conversation before one text, as the writer sees it. Texts never sent are left out."""
    rows = connection.execute(
        "SELECT direction, body FROM texts WHERE lead_id = ? AND text_id < ? "
        "AND status NOT IN ('cancelled', 'failed') ORDER BY text_id",
        (lead_id, before_text_id),
    ).fetchall()
    return [Turn("user" if row["direction"] == "in" else "assistant", row["body"]) for row in rows]
