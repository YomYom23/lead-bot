# lead-bot/src/lead_bot/store.py
"""SQLite storage for leads, consent evidence, scheduled calls, do-not-call list, owner alerts.

Two processes share the file. The form server (server.py) adds leads, and the dialer
(dialer.py) claims calls that are due. WAL mode lets one write while the other reads.

Every scheduled call is a row with a due time, not a timer in memory. So a restart never loses
a follow-up, and a follow-up can never be placed twice: (lead_id, attempt) is UNIQUE.

Times are stored as Unix seconds (UTC) and handed out as timezone-aware datetimes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from .consent import ConsentEvidence
from .leads import IntakeDecision, NewLead, decide_intake
from .timing import CallingHours, follow_up_call_time, next_allowed_time

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

CREATE TABLE IF NOT EXISTS calls (
    call_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id          INTEGER NOT NULL REFERENCES leads (lead_id),
    attempt          INTEGER NOT NULL,      -- 1 = the first call
    due_at           REAL NOT NULL,
    status           TEXT NOT NULL DEFAULT 'scheduled',  -- a CallStatus
    provider_call_id TEXT,                  -- Twilio's CallSid, once placed
    placed_at        REAL,
    outcome          TEXT,                  -- a CallOutcome, once known
    note             TEXT,                  -- why it was cancelled or failed
    UNIQUE (lead_id, attempt)               -- the same follow-up can't be scheduled twice
);
CREATE INDEX IF NOT EXISTS calls_due ON calls (status, due_at);

CREATE TABLE IF NOT EXISTS do_not_call (
    phone    TEXT PRIMARY KEY,
    added_at REAL NOT NULL,
    source   TEXT NOT NULL                  -- e.g. 'pressed 9 on call 12', 'owner'
);

CREATE TABLE IF NOT EXISTS owner_alerts (
    alert_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER REFERENCES leads (lead_id),
    reason     TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

# following_up  -> calls are scheduled or in progress
# answered      -> someone picked up; no more automatic calls (phase 2 refines this)
# unreached     -> every attempt went unanswered; the owner was alerted
# owner_alerted -> no AI calls at all (no consent, or opted out before); the owner was alerted
# duplicate     -> the number already had a follow-up running when this form arrived
# opted_out     -> the number went on the do-not-call list
LeadStatus = Literal["following_up", "answered", "unreached", "owner_alerted", "duplicate", "opted_out"]

# scheduled -> waiting for due_at;  dialing -> claimed by the dialer, checks running;
# placed -> handed to the phone provider;  completed -> outcome known;  cancelled -> never dialed
CallStatus = Literal["scheduled", "dialing", "placed", "completed", "cancelled"]

CallOutcome = Literal["answered", "no_answer", "busy", "voicemail", "failed"]

LEAD_STATUS_FOR_INTAKE: dict[IntakeDecision, LeadStatus] = {
    "call": "following_up",
    "previously_opted_out": "owner_alerted",
    "no_call_consent": "owner_alerted",
    "already_in_follow_up": "duplicate",
}


@dataclass(frozen=True)
class AddedLead:
    lead_id: int
    decision: IntakeDecision
    first_call_due: datetime | None  # None when the AI won't call


@dataclass(frozen=True)
class DueCall:
    """A claimed call, with everything the dialer re-checks before dialing, read fresh."""

    call_id: int
    lead_id: int
    attempt: int
    phone: str
    full_name: str
    lead_status: str
    consent_given: bool
    on_do_not_call: bool


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
            # WAL lets the form server write while the dialer reads, instead of one blocking.
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
        self, lead: NewLead, consent: ConsentEvidence, now: datetime, hours: CallingHours
    ) -> AddedLead:
        """Record a form submission and act on it, all in one transaction.

        The lead, its consent evidence, and either its first call or an owner alert are written
        together. A crash can't leave a scheduled call with no consent record behind it.
        """
        with self._write_transaction() as connection:
            on_do_not_call = _is_on_do_not_call(connection, lead.phone)
            already_in_follow_up = (
                connection.execute(
                    "SELECT 1 FROM leads WHERE phone = ? AND status = 'following_up'", (lead.phone,)
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
            first_call_due: datetime | None = None
            if decision == "call":
                first_call_due = next_allowed_time(now, hours)
                connection.execute(
                    "INSERT INTO calls (lead_id, attempt, due_at) VALUES (?, 1, ?)",
                    (lead_id, first_call_due.timestamp()),
                )
            elif decision in ("previously_opted_out", "no_call_consent"):
                _add_owner_alert(connection, lead_id, decision, now)
            return AddedLead(lead_id=lead_id, decision=decision, first_call_due=first_call_due)

    def lead_status(self, lead_id: int) -> str | None:
        with self._read() as connection:
            row = connection.execute("SELECT status FROM leads WHERE lead_id = ?", (lead_id,)).fetchone()
        return None if row is None else str(row["status"])

    # ---- calls ----

    def claim_due_call(self, now: datetime) -> DueCall | None:
        """Take the most overdue scheduled call and mark it 'dialing', so it's dialed once.

        The consent and do-not-call facts are read in the same transaction as the claim, so
        the dialer checks the state at this moment, not when the call was scheduled.
        """
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT calls.call_id, calls.lead_id, calls.attempt, leads.phone, leads.full_name, "
                "leads.status AS lead_status, consents.given AS consent_given, "
                "EXISTS (SELECT 1 FROM do_not_call WHERE do_not_call.phone = leads.phone) "
                "  AS on_do_not_call "
                "FROM calls JOIN leads ON leads.lead_id = calls.lead_id "
                # LEFT JOIN: a lead with no consent row still comes back, with consent_given NULL,
                # which reads as "not given" below. An inner join would hide it, and that call
                # would sit in the queue forever.
                "LEFT JOIN consents ON consents.lead_id = calls.lead_id "
                "WHERE calls.status = 'scheduled' AND calls.due_at <= ? "
                "ORDER BY calls.due_at LIMIT 1",
                (now.timestamp(),),
            ).fetchone()
            if row is None:
                return None
            connection.execute("UPDATE calls SET status = 'dialing' WHERE call_id = ?", (row["call_id"],))
            return DueCall(
                call_id=row["call_id"],
                lead_id=row["lead_id"],
                attempt=row["attempt"],
                phone=row["phone"],
                full_name=row["full_name"],
                lead_status=row["lead_status"],
                consent_given=row["consent_given"] == 1,  # NULL (no consent row) -> False
                on_do_not_call=row["on_do_not_call"] == 1,
            )

    def mark_call_placed(self, call_id: int, provider_call_id: str, placed_at: datetime) -> None:
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE calls SET status = 'placed', provider_call_id = ?, placed_at = ? "
                "WHERE call_id = ? AND status = 'dialing'",
                (provider_call_id, placed_at.timestamp(), call_id),
            )

    def reschedule_call(self, call_id: int, due_at: datetime) -> None:
        """Put a claimed call back in the queue for later, without dialing it."""
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE calls SET status = 'scheduled', due_at = ? WHERE call_id = ? AND status = 'dialing'",
                (due_at.timestamp(), call_id),
            )

    def cancel_call(
        self,
        call_id: int,
        reason: str,
        now: datetime,
        stop_lead_as: LeadStatus | None = None,
        alert_owner: bool = False,
    ) -> None:
        """Drop a claimed call without dialing it, optionally ending the lead's follow-up too."""
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT lead_id FROM calls WHERE call_id = ? AND status = 'dialing'", (call_id,)
            ).fetchone()
            if row is None:
                return
            connection.execute(
                "UPDATE calls SET status = 'cancelled', note = ? WHERE call_id = ?", (reason, call_id)
            )
            if stop_lead_as is not None:
                connection.execute(
                    "UPDATE leads SET status = ? WHERE lead_id = ? AND status = 'following_up'",
                    (stop_lead_as, row["lead_id"]),
                )
            if alert_owner:
                _add_owner_alert(connection, row["lead_id"], reason, now)

    def record_call_outcome(
        self,
        call_id: int,
        outcome: CallOutcome,
        now: datetime,
        hours: CallingHours,
        note: str | None = None,
    ) -> datetime | None:
        """Record how a call ended and schedule the next one if needed.

        Returns when the next call is due, or None if no further call was scheduled.

        Safe to call twice for the same call. Phone providers retry their status updates, and
        only the first update for a call does anything. A repeat can't schedule a second
        follow-up.
        """
        with self._write_transaction() as connection:
            updated = connection.execute(
                "UPDATE calls SET status = 'completed', outcome = ?, note = COALESCE(?, note) "
                "WHERE call_id = ? AND status IN ('dialing', 'placed')",
                (outcome, note, call_id),
            )
            if updated.rowcount != 1:
                return None  # already recorded, or not a call we know
            call = connection.execute(
                "SELECT calls.lead_id, calls.attempt, leads.status AS lead_status FROM calls "
                "JOIN leads ON leads.lead_id = calls.lead_id WHERE calls.call_id = ?",
                (call_id,),
            ).fetchone()
            lead_id: int = call["lead_id"]
            if call["lead_status"] != "following_up":
                return None  # stopped in the meantime, e.g. they opted out during the call
            if outcome == "answered":
                connection.execute("UPDATE leads SET status = 'answered' WHERE lead_id = ?", (lead_id,))
                return None

            # The schedule counts from the first call. If that one never reached the provider
            # (placed_at empty), its due time stands in.
            first_call = connection.execute(
                "SELECT COALESCE(placed_at, due_at) AS started FROM calls WHERE lead_id = ? AND attempt = 1",
                (lead_id,),
            ).fetchone()
            first_call_started = datetime.fromtimestamp(first_call["started"], UTC)
            next_call_due = follow_up_call_time(first_call_started, call["attempt"], now, hours)
            if next_call_due is None:
                connection.execute("UPDATE leads SET status = 'unreached' WHERE lead_id = ?", (lead_id,))
                _add_owner_alert(connection, lead_id, "not_reached_after_all_attempts", now)
                return None
            connection.execute(
                # OR IGNORE plus UNIQUE (lead_id, attempt): the follow-up exists at most once.
                "INSERT OR IGNORE INTO calls (lead_id, attempt, due_at) VALUES (?, ?, ?)",
                (lead_id, call["attempt"] + 1, next_call_due.timestamp()),
            )
            return next_call_due

    def interrupted_call_ids(self) -> list[int]:
        """Calls left 'dialing' by a dialer that stopped. See dialer.close_interrupted_calls."""
        with self._read() as connection:
            rows = connection.execute("SELECT call_id FROM calls WHERE status = 'dialing'").fetchall()
        return [row["call_id"] for row in rows]

    # ---- do-not-call ----

    def add_to_do_not_call(self, phone: str, source: str, now: datetime) -> None:
        """Opt a number out. Its scheduled calls are cancelled and its leads stop following up."""
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO do_not_call (phone, added_at, source) VALUES (?, ?, ?)",
                (phone, now.timestamp(), source),
            )
            connection.execute(
                "UPDATE calls SET status = 'cancelled', note = 'number opted out' "
                "WHERE status = 'scheduled' AND lead_id IN (SELECT lead_id FROM leads WHERE phone = ?)",
                (phone,),
            )
            connection.execute(
                "UPDATE leads SET status = 'opted_out' WHERE phone = ? AND status = 'following_up'", (phone,)
            )

    def is_on_do_not_call(self, phone: str) -> bool:
        with self._read() as connection:
            return _is_on_do_not_call(connection, phone)

    # ---- owner alerts and reporting ----

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
        """Leads and calls by status, for /health."""
        with self._read() as connection:
            lead_rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM leads GROUP BY status"
            ).fetchall()
            call_rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM calls GROUP BY status"
            ).fetchall()
        return {
            "leads": {row["status"]: row["total"] for row in lead_rows},
            "calls": {row["status"]: row["total"] for row in call_rows},
        }


def _is_on_do_not_call(connection: sqlite3.Connection, phone: str) -> bool:
    return connection.execute("SELECT 1 FROM do_not_call WHERE phone = ?", (phone,)).fetchone() is not None


def _add_owner_alert(connection: sqlite3.Connection, lead_id: int, reason: str, now: datetime) -> None:
    connection.execute(
        "INSERT INTO owner_alerts (lead_id, reason, created_at) VALUES (?, ?, ?)",
        (lead_id, reason, now.timestamp()),
    )
