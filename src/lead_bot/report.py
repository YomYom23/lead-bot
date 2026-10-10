# lead-bot/src/lead_bot/report.py
"""The owner's monthly results email: what the bot did last month, in numbers, with the most
recent bookings.

    uv run python -m lead_bot.report                          # preview last month's report
    uv run python -m lead_bot.report --month 2026-10          # preview any month
    uv run python -m lead_bot.report --month 2026-10 --send   # email it now

The worker sends last month's report by itself, once, on or after 8 AM on the 1st, from its own
thread (MonthlyReportSender), through the same email settings as the owner alerts. Each month
sent is recorded (store.monthly_reports), so a restart never sends it twice. A month that ended
before the database's first lead or call gets no report, so a new install doesn't email the
owner an empty one for the month before it existed.

Months follow the business's timezone: "October" is October in Phoenix, not in UTC.

Why it exists: the number a roofer renews for is inspections booked. The report puts it first,
then where those bookings came from, so the client sees what each part of the service is worth.
"""

from __future__ import annotations

import argparse
import logging
import threading
from collections.abc import Callable
from datetime import datetime, time
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .owner_alerts import EmailSettings, Mailer, PrintingMailer, SmtpMailer
from .settings import BusinessSettings, database_path_from_environment
from .store import MonthlyNumbers, Store
from .timing import utc_now

logger = logging.getLogger(__name__)

# Not at midnight: a report arriving at 12:01 AM on the 1st is read at the bottom of the inbox.
REPORT_SEND_TIME = time(8, 0)
# How often the worker's report thread checks whether a report is due.
CHECK_EVERY_SECONDS = 900.0
SOURCE_LABELS = {"form": "web form", "phone": "phone call", "reactivation": "old-lead campaign"}


def month_bounds(month: str, timezone: ZoneInfo) -> tuple[datetime, datetime]:
    """'2026-10' -> midnight on October 1 and on November 1, in the business's timezone."""
    year, month_number = (int(part) for part in month.split("-"))
    start = datetime(year, month_number, 1, tzinfo=timezone)
    end = datetime(year + month_number // 12, month_number % 12 + 1, 1, tzinfo=timezone)
    return start, end


def previous_month(now: datetime, timezone: ZoneInfo) -> str:
    local = now.astimezone(timezone)
    year, month_number = (local.year, local.month - 1) if local.month > 1 else (local.year - 1, 12)
    return f"{year:04d}-{month_number:02d}"


def month_label(month: str) -> str:
    """'2026-10' -> 'October 2026'."""
    year, month_number = (int(part) for part in month.split("-"))
    return f"{datetime(year, month_number, 1):%B} {year}"


def percent(part: int, whole: int) -> str:
    return "n/a" if whole == 0 else f"{round(100 * part / whole)}%"


def report_text(numbers: MonthlyNumbers, business_name: str, month: str) -> str:
    by_source = numbers.bookings_by_source
    lines = [
        f"Here's what the lead bot did for {business_name} in {month_label(month)}.",
        "",
        f"Inspections booked: {numbers.bookings} (web form {by_source.get('form', 0)}, "
        f"phone calls {by_source.get('phone', 0)}, old-lead campaigns {by_source.get('reactivation', 0)})",
        "",
        f"Web form leads: {numbers.form_leads}",
        f"  Texted (agreed to texts): {numbers.form_leads_texted}",
        f"  Replied: {numbers.form_leads_replied} "
        f"({percent(numbers.form_leads_replied, numbers.form_leads_texted)} of those texted)",
        f"  Booked: {numbers.form_leads_booked} ({percent(numbers.form_leads_booked, numbers.form_leads)})",
        "",
        f"Missed calls the bot answered: {numbers.calls_answered}",
        f"  Booked: {numbers.calls_booked}. Left a message: {numbers.calls_messages}. "
        f"Hung up: {numbers.calls_hung_up}.",
        "",
        f"Old leads texted by a campaign: {numbers.reactivation_started}",
        f"  Replied: {numbers.reactivation_replied}. Booked: {numbers.reactivation_booked}.",
        "",
        f"Urgent leaks flagged to you: {numbers.urgent_leaks}",
        f"Times the bot passed someone to you: {numbers.alerts_for_owner}",
        f"Numbers that asked not to be texted: {numbers.opted_out}",
        "",
    ]
    if numbers.recent_bookings:
        lines.append("Most recent bookings:")
        lines += [
            f"  {booking.full_name}: {booking.slot_label} "
            f"(from a {SOURCE_LABELS.get(booking.source, booking.source)})"
            for booking in numbers.recent_bookings
        ]
    else:
        lines.append("No inspections were booked this month.")
    lines += ["", "Sent by the lead bot."]
    return "\n".join(lines)


def build_report_email(
    numbers: MonthlyNumbers, settings: BusinessSettings, month: str, from_address: str, to_address: str
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = f"[{settings.business_name}] Lead bot results for {month_label(month)}"
    message["From"] = from_address
    message["To"] = to_address
    message.set_content(report_text(numbers, settings.business_name, month))
    return message


class MonthlyReportSender:
    """Emails last month's report once it's due. Runs on its own worker thread."""

    def __init__(
        self,
        store: Store,
        settings: BusinessSettings,
        mailer: Mailer,
        from_address: str,
        to_address: str,
        clock: Callable[[], datetime] = utc_now,
        record_sent: bool = True,
    ) -> None:
        self._store = store
        self._settings = settings
        self._mailer = mailer
        self._from = from_address
        self._to = to_address
        self._clock = clock
        # False when emails are only printed (no email settings): nothing is recorded, so the
        # report still goes once email is set up. Printed months are remembered for this run.
        self._record_sent = record_sent
        self._printed: set[str] = set()

    def send_if_due(self) -> str | None:
        """Send last month's report if it's due and not sent. Returns the month sent, or None."""
        timezone = self._settings.timezone
        now = self._clock()
        local = now.astimezone(timezone)
        first_of_month_send_time = datetime.combine(
            local.date().replace(day=1), REPORT_SEND_TIME, tzinfo=timezone
        )
        if local < first_of_month_send_time:
            return None
        month = previous_month(now, timezone)
        if month in self._printed or self._store.monthly_report_sent(month):
            return None
        start, end = month_bounds(month, timezone)
        earliest = self._store.earliest_activity()
        if earliest is None or earliest >= end:
            return None
        numbers = self._store.monthly_numbers(start, end)
        # A failure raises and leaves the month unrecorded, so the next check tries again.
        self._mailer.send(build_report_email(numbers, self._settings, month, self._from, self._to))
        if self._record_sent:
            self._store.mark_monthly_report_sent(month, now)
        else:
            self._printed.add(month)
        logger.info("monthly report for %s sent", month)
        return month

    def run_forever(self, stop: threading.Event) -> None:
        """The report thread's loop. It must never die quietly, so every check is guarded."""
        while not stop.is_set():
            try:
                self.send_if_due()
            except Exception:
                logger.exception("monthly report check failed; trying again later")
            stop.wait(CHECK_EVERY_SECONDS)


def build_report_sender(
    store: Store, settings: BusinessSettings, email: EmailSettings | None
) -> MonthlyReportSender:
    """Real email when set, a printed report otherwise (the dry-run worker)."""
    if email is None:
        return MonthlyReportSender(
            store, settings, PrintingMailer(), "lead-bot", "the owner", record_sent=False
        )
    return MonthlyReportSender(store, settings, SmtpMailer(email), email.username, email.to_address)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--month", help="the month to report, as YYYY-MM (default: last month)")
    parser.add_argument("--send", action="store_true", help="email it to OWNER_ALERT_EMAIL now")
    arguments = parser.parse_args()
    load_dotenv()
    settings = BusinessSettings.from_environment()
    store = Store(database_path_from_environment())
    month = arguments.month or previous_month(utc_now(), settings.timezone)
    try:
        start, end = month_bounds(month, settings.timezone)
    except ValueError:
        raise SystemExit(f"[report] --month must look like 2026-10, not {month!r}") from None
    numbers = store.monthly_numbers(start, end)
    if not arguments.send:
        print(report_text(numbers, settings.business_name, month))
        return
    email = EmailSettings.from_environment()
    if email is None:
        raise SystemExit("[report] --send needs the owner alert email settings in .env")
    SmtpMailer(email).send(build_report_email(numbers, settings, month, email.username, email.to_address))
    # Recorded, so the worker doesn't send the same month again on the 1st.
    store.mark_monthly_report_sent(month, utc_now())
    print(f"[report] {month_label(month)} report sent to {email.to_address}")


if __name__ == "__main__":
    main()
