# lead-bot/src/lead_bot/settings.py
"""The business's settings, read from the environment (.env locally).

Phase 1 needed only the name and timezone. Phase 2 adds what a call needs: the callback number
spoken in the opening, the public address Twilio connects back to, and the inspection hours.
Twilio's own credentials come in phase 4.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .leads import InvalidLeadForm, normalize_us_phone, pretty_phone
from .slots import BookingRules
from .timing import CallingHours

DEFAULT_TIMEZONE = "America/Phoenix"

# Where the server can be reached. Locally that's uvicorn's default address. On the VPS it's the
# public https:// address, and Twilio connects to the wss:// form of it for the live call.
DEFAULT_PUBLIC_BASE_URL = "http://127.0.0.1:8000"


def placeholder_inspection_rules(timezone: ZoneInfo) -> BookingRules:
    """Placeholder inspection hours until a real roofer gives theirs.

    Mon-Fri 8-5, a 1-hour inspection, then 30 minutes kept free to drive to the next job. At
    least 2 hours' notice, so an inspector is never booked for 20 minutes from now.
    """
    return BookingRules(
        timezone=timezone,
        open_weekdays=frozenset({0, 1, 2, 3, 4}),
        opening_time=time(8, 0),
        closing_time=time(17, 0),
        appointment_length=timedelta(hours=1),
        gap_after_appointment=timedelta(minutes=30),
        minimum_notice=timedelta(hours=2),
        days_ahead=14,
    )


@dataclass(frozen=True)
class BusinessSettings:
    # The REGISTERED business name. 47 CFR 64.1200(b)(1) requires an artificial-voice call to
    # begin with "the name under which the entity is registered to conduct business" with the
    # state, so a nickname or brand here would make every call non-compliant.
    business_name: str
    timezone: ZoneInfo
    calling_hours: CallingHours
    # Said in every call's opening. 47 CFR 64.1200(b)(2) requires a number the homeowner can call
    # back, and it may not be the number the call comes from. E.164, like +14805550199.
    callback_phone: str = "+14805550199"
    public_base_url: str = DEFAULT_PUBLIC_BASE_URL

    @property
    def callback_phone_spoken(self) -> str:
        """'(480) 555-0199': how the opening says it."""
        return pretty_phone(self.callback_phone)

    @property
    def relay_url(self) -> str:
        """The WebSocket address for live calls: ws:// locally, wss:// behind https."""
        base = self.public_base_url.rstrip("/")
        if base.startswith("https://"):
            return "wss://" + base.removeprefix("https://") + "/calls/relay"
        return "ws://" + base.removeprefix("http://") + "/calls/relay"

    @property
    def inspection_rules(self) -> BookingRules:
        return placeholder_inspection_rules(self.timezone)

    @classmethod
    def from_environment(cls) -> BusinessSettings:
        business_name = " ".join(os.environ.get("BUSINESS_NAME", "").split())
        if not business_name:
            raise RuntimeError(
                "BUSINESS_NAME must be set in .env: the business's registered name. It appears in "
                "the consent text on the form and is said first on every call."
            )
        timezone_name = os.environ.get("BUSINESS_TIMEZONE", DEFAULT_TIMEZONE)
        try:
            timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise RuntimeError(
                f"BUSINESS_TIMEZONE={timezone_name!r} isn't a timezone name; use one like America/Denver"
            ) from None
        raw_callback_phone = os.environ.get("BUSINESS_CALLBACK_PHONE", "")
        try:
            callback_phone = normalize_us_phone(raw_callback_phone)
        except InvalidLeadForm:
            raise RuntimeError(
                "BUSINESS_CALLBACK_PHONE must be set in .env to a 10-digit US number people can call "
                "back on. The rules require it in every call's opening, and it can't be the number "
                "the calls come from."
            ) from None
        public_base_url = os.environ.get("PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).strip()
        if not public_base_url.startswith(("http://", "https://")):
            raise RuntimeError(f"PUBLIC_BASE_URL={public_base_url!r} must start with http:// or https://")
        return cls(
            business_name=business_name,
            timezone=timezone,
            calling_hours=CallingHours(timezone),
            callback_phone=callback_phone,
            public_base_url=public_base_url,
        )


def database_path_from_environment() -> str:
    """Where the SQLite file lives. The form server and the dialer both call this.

    They must open the same file: the server writes leads and the dialer reads them. If each
    worked the path out separately, a change to one would leave the dialer polling an empty
    database forever, with no error.
    """
    return os.environ.get("LEAD_BOT_DB_PATH", "lead_bot.db")
