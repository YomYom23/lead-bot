# lead-bot/tests/conftest.py
# Shared builders: a lead, consent evidence, and a store in a temp folder.
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from lead_bot.consent import CONSENT_VERSION, ConsentEvidence, consent_text
from lead_bot.leads import NewLead
from lead_bot.store import Store
from lead_bot.timing import TextingHours

PHOENIX = ZoneInfo("America/Phoenix")
HOURS = TextingHours(PHOENIX)
BUSINESS_NAME = "Desert Ridge Roofing LLC"
# The owner's mobile for alert texts (OWNER_ALERT_PHONE). 555-01xx numbers are reserved for
# fiction, so no real phone is ever in a test.
OWNER_PHONE = "+1" + "480" + "555" + "0177"
DANA_PHONE = "+16025550142"


def phoenix(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=PHOENIX)


MONDAY_10AM = phoenix(2026, 10, 5, 10, 0)


def make_lead(phone: str = DANA_PHONE, name: str = "Dana Ruiz") -> NewLead:
    return NewLead(full_name=name, phone=phone, address="12 Saguaro Ln, Mesa AZ", problem="storm_damage")


def make_consent(given: bool = True) -> ConsentEvidence:
    return ConsentEvidence(
        given=given,
        version=CONSENT_VERSION,
        text_shown=consent_text(BUSINESS_NAME),
        ip_address="203.0.113.7",
        forwarded_for=None,
        user_agent="pytest",
        source="http://testserver/leads",
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "leads.db")
