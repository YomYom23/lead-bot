# lead-bot/src/lead_bot/settings.py
"""The business's settings, read from the environment (.env locally).

Only what phase 1 uses is required. The callback number spoken at the start of each call, and
the Twilio credentials, are added in the phases that use them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .timing import CallingHours

DEFAULT_TIMEZONE = "America/Phoenix"


@dataclass(frozen=True)
class BusinessSettings:
    # The REGISTERED business name. 47 CFR 64.1200(b)(1) requires an artificial-voice call to
    # begin with "the name under which the entity is registered to conduct business" with the
    # state, so a nickname or brand here would make every call non-compliant.
    business_name: str
    timezone: ZoneInfo
    calling_hours: CallingHours

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
        return cls(business_name=business_name, timezone=timezone, calling_hours=CallingHours(timezone))


def database_path_from_environment() -> str:
    """Where the SQLite file lives. The form server and the dialer both call this.

    They must open the same file: the server writes leads and the dialer reads them. If each
    worked the path out separately, a change to one would leave the dialer polling an empty
    database forever, with no error.
    """
    return os.environ.get("LEAD_BOT_DB_PATH", "lead_bot.db")
