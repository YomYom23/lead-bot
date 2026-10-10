# lead-bot/src/lead_bot/reactivation.py
"""Reactivation campaigns: text a client's old leads, a few a day, asking whether they still
want a free roof inspection.

    uv run python -m lead_bot.reactivation import old-leads.csv --name spring-2026 --dry-run
    uv run python -m lead_bot.reactivation import old-leads.csv --name spring-2026 --daily-limit 50
    uv run python -m lead_bot.reactivation status

The list is a CSV file with a header row and these columns, in any order:
    full_name, phone, address, consent_date, consent_source, consent_wording
consent_date is the day the person agreed to texts, as YYYY-MM-DD. consent_source is where they
agreed (the web form's address, say). consent_wording is the exact sentence they agreed to.

Why every consent column is required: a text the business can't show consent for can cost it
$500 to $1,500 under the federal robocall law (the TCPA). A row missing any of them is refused
and listed by line number, so the client can fix the file. This module checks that the proof is
there; whether the wording actually covers automated texts from this business is a judgement for
a person, ideally the client's lawyer. The dry run lists every distinct wording for that reason.

How it fits together:
  1. read_contact_list checks every row of the file.
  2. Store.add_campaign turns each good row into an ordinary lead (with its consent evidence)
     waiting for its turn, leaving out numbers on the do-not-call list, already in a
     conversation, or in an earlier campaign.
  3. The worker starts up to the daily limit each day, spread across the texting window
     (Store.start_campaign_contacts). From the first text on, it's the same conversation as a
     form lead's, starting at the still_interested question, with one reminder two days later.
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from .leads import (
    MAX_ADDRESS_LENGTH,
    MAX_NAME_LENGTH,
    InvalidLeadForm,
    ReactivationContact,
    normalize_us_phone,
)
from .settings import BusinessSettings, database_path_from_environment
from .store import CampaignExists, CampaignImport, CampaignSummary, Store
from .timing import utc_now

REQUIRED_COLUMNS = ("full_name", "phone", "address", "consent_date", "consent_source", "consent_wording")
DEFAULT_DAILY_LIMIT = 50
# How much of each distinct consent wording the report shows. Enough to judge it; the full text
# is stored with every contact.
WORDING_PREVIEW_CHARACTERS = 300


class InvalidContactList(ValueError):
    """The file as a whole can't be used: empty, or missing a required column."""


@dataclass(frozen=True)
class RefusedRow:
    line_number: int  # as a spreadsheet shows it: the header is line 1
    reason: str


@dataclass(frozen=True)
class ContactList:
    contacts: list[ReactivationContact]
    refused: list[RefusedRow]


def read_contact_list(path: Path, today: date) -> ContactList:
    """Check every row of the CSV file. Good rows become contacts; the rest are refused with a
    reason. `today` is the business's local date, for refusing consent dates in the future."""
    # utf-8-sig: Excel's "CSV UTF-8" puts an invisible marker at the start of the file, which
    # would otherwise become part of the first column's name. newline="": the csv module's rule,
    # so a line break inside a quoted cell stays in the cell.
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            raise InvalidContactList(f"{path} is empty")
        # Headers are matched without regard to spaces and capitals: "Full_Name " is full_name.
        columns = {name.strip().lower(): name for name in reader.fieldnames}
        missing = [column for column in REQUIRED_COLUMNS if column not in columns]
        if missing:
            raise InvalidContactList(
                f"{path} is missing the column(s) {', '.join(missing)}. "
                f"It needs a header row with: {', '.join(REQUIRED_COLUMNS)}"
            )
        contacts: list[ReactivationContact] = []
        refused: list[RefusedRow] = []
        first_line_for_phone: dict[str, int] = {}
        for row in reader:
            # The file's own line number, not a row count: DictReader skips blank lines, and a
            # quoted cell can span lines, so counting rows would point the client at the wrong line.
            line_number = reader.line_num
            # A short row gives None for its missing cells; extra spaces inside a value fold to one.
            values = {
                column: " ".join((row.get(columns[column]) or "").split()) for column in REQUIRED_COLUMNS
            }
            if not any(values.values()):
                continue  # a blank line, often at the end of a spreadsheet export
            reason, contact = _check_row(values, today)
            if contact is not None and contact.phone in first_line_for_phone:
                reason, contact = f"same phone as line {first_line_for_phone[contact.phone]}", None
            if contact is None:
                refused.append(RefusedRow(line_number, reason))
                continue
            first_line_for_phone[contact.phone] = line_number
            contacts.append(contact)
    return ContactList(contacts, refused)


def _check_row(values: dict[str, str], today: date) -> tuple[str, ReactivationContact | None]:
    """A contact, or the reason the row is refused."""
    empty = [column for column in REQUIRED_COLUMNS if not values[column]]
    if empty:
        return f"missing {', '.join(empty)}", None
    try:
        phone = normalize_us_phone(values["phone"])
    except InvalidLeadForm:
        return "phone isn't a 10-digit US number", None
    if len(values["full_name"]) > MAX_NAME_LENGTH:
        return f"full_name is longer than {MAX_NAME_LENGTH} characters", None
    if len(values["address"]) > MAX_ADDRESS_LENGTH:
        return f"address is longer than {MAX_ADDRESS_LENGTH} characters", None
    try:
        consented_on = date.fromisoformat(values["consent_date"])
    except ValueError:
        return "consent_date must be a date like 2025-03-14", None
    if consented_on > today:
        return "consent_date is in the future", None
    return "", ReactivationContact(
        full_name=values["full_name"],
        phone=phone,
        address=values["address"],
        consented_on=consented_on,
        consent_source=values["consent_source"],
        consent_wording=values["consent_wording"],
    )


# ---- reports ----


def import_report(contact_list: ContactList, result: CampaignImport, daily_limit: int, dry_run: bool) -> str:
    """What an import did, or would do, in plain words."""
    lines = [
        "DRY RUN: nothing was saved." if dry_run else f"Campaign saved (id {result.campaign_id}).",
        f"Rows accepted: {len(contact_list.contacts)}. Rows refused: {len(contact_list.refused)}.",
    ]
    for refused_row in contact_list.refused:
        lines.append(f"  line {refused_row.line_number}: {refused_row.reason}")
    lines.append(f"Contacts {'that would be ' if dry_run else ''}added: {result.added}.")
    for reason, phones in result.skipped.items():
        lines.append(f"  left out, {reason}: {len(phones)}")
    if result.added:
        days = math.ceil(result.added / daily_limit)
        lines.append(f"At {daily_limit} a day, the first texts take {days} day{'s' if days != 1 else ''}.")
    wordings = Counter(contact.consent_wording for contact in contact_list.contacts)
    if wordings:
        lines.append("")
        lines.append("Consent wording on the list. Check that each one covers automated texts from")
        lines.append("this business before importing for real:")
        for wording, count in wordings.most_common():
            preview = wording[:WORDING_PREVIEW_CHARACTERS]
            if len(wording) > WORDING_PREVIEW_CHARACTERS:
                preview += "..."
            lines.append(f'  {count} contact{"s" if count != 1 else ""}: "{preview}"')
    return "\n".join(lines)


def status_report(summaries: Sequence[CampaignSummary]) -> str:
    if not summaries:
        return "No campaigns yet."
    lines: list[str] = []
    for summary in summaries:
        statuses = summary.contacts_by_status
        total = sum(statuses.values()) + summary.skipped
        lines += [
            f"{summary.name} ({summary.daily_limit} a day, imported {summary.created_at:%Y-%m-%d}): "
            f"{total} contact{'s' if total != 1 else ''}",
            f"  waiting for their turn: {statuses.get('campaign_waiting', 0)}",
            f"  texted, no reply yet: {statuses.get('following_up', 0)}",
            f"  never replied: {statuses.get('unreached', 0)}",
            f"  talking with the bot: {statuses.get('in_conversation', 0)}",
            f"  booked: {statuses.get('booked', 0)}",
            f"  handed to a person: {statuses.get('handed_off', 0)}",
            f"  opted out or not interested: {statuses.get('opted_out', 0)}",
            f"  left out at their turn: {summary.skipped + statuses.get('duplicate', 0)}",
        ]
    return "\n".join(lines)


# ---- the command ----


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    importing = commands.add_parser("import", help="import a CSV list of old leads as a campaign")
    importing.add_argument("path", type=Path)
    importing.add_argument("--name", required=True, help="a name for the campaign, e.g. spring-2026")
    importing.add_argument(
        "--daily-limit", type=int, default=DEFAULT_DAILY_LIMIT, help="contacts started per day"
    )
    importing.add_argument("--dry-run", action="store_true", help="check everything, save nothing")
    commands.add_parser("status", help="how each campaign is going")
    parsed = parser.parse_args(arguments)

    load_dotenv()
    settings = BusinessSettings.from_environment()
    store = Store(database_path_from_environment())
    if parsed.command == "status":
        print(status_report(store.campaign_summaries()))
        return
    if parsed.daily_limit < 1:
        raise SystemExit("[reactivation] --daily-limit must be at least 1")
    now = utc_now()
    try:
        contact_list = read_contact_list(parsed.path, today=now.astimezone(settings.timezone).date())
    except (InvalidContactList, OSError) as error:
        raise SystemExit(f"[reactivation] {error}") from None
    if not contact_list.contacts:
        print(import_report(contact_list, CampaignImport(None, [], {}), parsed.daily_limit, dry_run=True))
        raise SystemExit("[reactivation] no usable rows, so no campaign was made")
    try:
        result = store.add_campaign(
            parsed.name,
            parsed.path.name,
            parsed.daily_limit,
            contact_list.contacts,
            now,
            settings.texting_hours,
            save=not parsed.dry_run,
        )
    except CampaignExists as error:
        raise SystemExit(f"[reactivation] {error}") from None
    print(import_report(contact_list, result, parsed.daily_limit, dry_run=parsed.dry_run))
    if not parsed.dry_run:
        print("The worker starts texting at the next opening of the texting window.")


if __name__ == "__main__":
    main()
