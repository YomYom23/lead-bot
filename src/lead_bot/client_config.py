# lead-bot/src/lead_bot/client_config.py
"""One roofer's own settings, read from their client.toml.

Each client runs their own copy of the bot, and what differs between them lives in this file:
the facts the writer may text to homeowners, and the hours inspections can be booked.
settings.py combines it with the .env values (name, timezone, phones) into BusinessSettings.

TOML holds only plain values, so this module turns them into the Python types the rest of the
bot uses (frozenset, time, timedelta). Every value is checked here, at startup: a typo in one
client's file should stop the bot with a message naming the key, not quietly offer the wrong
hours to homeowners.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .slots import BookingRules

# Where the real client's file is looked for, relative to where the bot is started.
DEFAULT_CLIENT_CONFIG_PATH = "client.toml"

# The placeholder roofer, kept in git. Tests, scenarios and the simulator use it, so their
# expectations (Mon-Fri 8-5) don't change whenever a real client's file does.
EXAMPLE_CLIENT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "client.example.toml"


class InvalidClientConfig(RuntimeError):
    """client.toml is missing, isn't valid TOML, or has a missing or wrong value."""


@dataclass(frozen=True)
class InspectionHours:
    """When inspections can happen, without the timezone (that comes from .env)."""

    open_weekdays: frozenset[int]  # Monday is 0 and Sunday is 6, as in date.weekday()
    opening_time: time
    closing_time: time
    appointment_length: timedelta
    gap_after_appointment: timedelta
    minimum_notice: timedelta
    days_ahead: int

    def booking_rules(self, timezone: ZoneInfo) -> BookingRules:
        return BookingRules(
            timezone=timezone,
            open_weekdays=self.open_weekdays,
            opening_time=self.opening_time,
            closing_time=self.closing_time,
            appointment_length=self.appointment_length,
            gap_after_appointment=self.gap_after_appointment,
            minimum_notice=self.minimum_notice,
            days_ahead=self.days_ahead,
        )


@dataclass(frozen=True)
class ClientConfig:
    # Sentences the writer may text to a homeowner. Nothing goes in that the business hasn't
    # approved: the guard lets through any claim that matches one of these.
    business_facts: tuple[str, ...]
    inspection_hours: InspectionHours


def client_config_path_from_environment() -> Path:
    return Path(os.environ.get("CLIENT_CONFIG_PATH", DEFAULT_CLIENT_CONFIG_PATH))


def load_client_config(path: Path) -> ClientConfig:
    try:
        # tomllib only reads binary files: it decodes the UTF-8 itself.
        with path.open("rb") as config_file:
            document = tomllib.load(config_file)
    except FileNotFoundError:
        raise InvalidClientConfig(
            f"{path} not found. Copy client.example.toml to client.toml and fill in the client's "
            "facts and inspection hours, or set CLIENT_CONFIG_PATH."
        ) from None
    except tomllib.TOMLDecodeError as error:
        raise InvalidClientConfig(f"{path} isn't valid TOML: {error}") from None
    return ClientConfig(
        business_facts=_business_facts(_table(document, "business", path), path),
        inspection_hours=_inspection_hours(_table(document, "inspection", path), path),
    )


# ---- checking each value ----
# tomllib returns plain dicts typed as Any, so each value is narrowed with isinstance before
# use. That is also where a wrong value in the file gets caught.


def _table(document: dict[str, object], name: str, path: Path) -> dict[str, object]:
    table = document.get(name)
    if not isinstance(table, dict):
        raise InvalidClientConfig(f"{path} needs a [{name}] section")
    return table


def _value(table: dict[str, object], key: str, section: str, path: Path) -> object:
    if key not in table:
        raise InvalidClientConfig(f"{path}: [{section}] is missing {key}")
    return table[key]


def _business_facts(table: dict[str, object], path: Path) -> tuple[str, ...]:
    facts = _value(table, "facts", "business", path)
    # An empty list is allowed in principle, but every client so far has facts; an empty one is
    # far more likely a mistake than a choice, and it would make the writer hand off everything.
    if not isinstance(facts, list) or not facts:
        raise InvalidClientConfig(f"{path}: [business] facts must be a non-empty list of sentences")
    checked_facts: list[str] = []
    for fact in facts:
        if not isinstance(fact, str) or not fact.strip():
            raise InvalidClientConfig(f"{path}: every [business] fact must be a non-empty string")
        checked_facts.append(" ".join(fact.split()))
    return tuple(checked_facts)


def _inspection_hours(table: dict[str, object], path: Path) -> InspectionHours:
    weekdays = _value(table, "open_weekdays", "inspection", path)
    # bool is a subclass of int in Python, so `true` would pass a plain isinstance(int) check.
    if (
        not isinstance(weekdays, list)
        or not weekdays
        or not all(isinstance(day, int) and not isinstance(day, bool) and 0 <= day <= 6 for day in weekdays)
    ):
        raise InvalidClientConfig(
            f"{path}: [inspection] open_weekdays must be a non-empty list of numbers 0-6 (Monday is 0)"
        )
    opening_time = _clock_time(table, "opening_time", path)
    closing_time = _clock_time(table, "closing_time", path)
    if closing_time <= opening_time:
        raise InvalidClientConfig(f"{path}: [inspection] closing_time must be after opening_time")
    return InspectionHours(
        open_weekdays=frozenset(weekdays),
        opening_time=opening_time,
        closing_time=closing_time,
        appointment_length=timedelta(minutes=_whole_number(table, "appointment_minutes", path, minimum=1)),
        gap_after_appointment=timedelta(
            minutes=_whole_number(table, "gap_after_appointment_minutes", path, minimum=0)
        ),
        minimum_notice=timedelta(minutes=_whole_number(table, "minimum_notice_minutes", path, minimum=0)),
        days_ahead=_whole_number(table, "days_ahead", path, minimum=1),
    )


def _clock_time(table: dict[str, object], key: str, path: Path) -> time:
    value = _value(table, key, "inspection", path)
    # A TOML local time (08:00:00, no quotes) arrives as datetime.time. A quoted "08:00" is a
    # string, which is the likeliest typo, so the message shows the right form.
    if not isinstance(value, time):
        raise InvalidClientConfig(
            f"{path}: [inspection] {key} must be a time without quotes, like {key} = 08:00:00"
        )
    return value


def _whole_number(table: dict[str, object], key: str, path: Path, minimum: int) -> int:
    value = _value(table, key, "inspection", path)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise InvalidClientConfig(f"{path}: [inspection] {key} must be a whole number of at least {minimum}")
    return value
