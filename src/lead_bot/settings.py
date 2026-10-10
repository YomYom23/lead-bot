# lead-bot/src/lead_bot/settings.py
"""The business's settings, read from the environment (.env locally).

The business name and timezone, the phone number people can call, the public address of the
server, plus the client's facts and inspection hours from client.toml (client_config.py).
Twilio's own credentials come with the texting phase that needs them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .client_config import ClientConfig, client_config_path_from_environment, load_client_config
from .leads import InvalidLeadForm, normalize_us_phone
from .slots import BookingRules
from .timing import TextingHours

DEFAULT_TIMEZONE = "America/Phoenix"

# Where the server can be reached. Locally that's uvicorn's default address. On the VPS it's the
# public https:// address Twilio delivers incoming texts to.
DEFAULT_PUBLIC_BASE_URL = "http://127.0.0.1:8000"


@dataclass(frozen=True)
class BusinessSettings:
    # The REGISTERED business name. The consent wording authorizes this seller by name, the first
    # text introduces the bot under it, and toll-free verification checks it against the business
    # that registered the number. A nickname here would match none of them.
    business_name: str
    timezone: ZoneInfo
    texting_hours: TextingHours
    # The client's own facts and inspection hours, from client.toml (see client_config.py).
    client: ClientConfig
    # A number people can phone: in the last reminder, the HELP reply, and the facts the writer
    # may give. Not the texting number: a toll-free texting number may not take voice calls.
    # E.164, like +14805550199.
    callback_phone: str = "+14805550199"
    public_base_url: str = DEFAULT_PUBLIC_BASE_URL

    @property
    def inspection_rules(self) -> BookingRules:
        # Built here rather than stored: the hours come from client.toml, the timezone from .env.
        return self.client.inspection_hours.booking_rules(self.timezone)

    @classmethod
    def from_environment(cls) -> BusinessSettings:
        business_name = " ".join(os.environ.get("BUSINESS_NAME", "").split())
        if not business_name:
            raise RuntimeError(
                "BUSINESS_NAME must be set in .env: the business's registered name. It appears in "
                "the consent text on the form and in the first text to every lead."
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
                "BUSINESS_CALLBACK_PHONE must be set in .env to a 10-digit US number people can "
                "phone. The last reminder and the HELP reply give it, and the writer may too."
            ) from None
        public_base_url = os.environ.get("PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).strip()
        if not public_base_url.startswith(("http://", "https://")):
            raise RuntimeError(f"PUBLIC_BASE_URL={public_base_url!r} must start with http:// or https://")
        # InvalidClientConfig is a RuntimeError, so a bad client.toml stops startup the same way
        # a bad .env value does, with a message naming the key.
        client = load_client_config(client_config_path_from_environment())
        return cls(
            business_name=business_name,
            timezone=timezone,
            texting_hours=TextingHours(timezone),
            client=client,
            callback_phone=callback_phone,
            public_base_url=public_base_url,
        )


def database_path_from_environment() -> str:
    """Where the SQLite file lives. The form server and the worker both call this.

    They must open the same file: the server writes leads and incoming texts, and the worker
    reads them. If each worked the path out separately, a change to one would leave the worker
    polling an empty database forever, with no error.
    """
    return os.environ.get("LEAD_BOT_DB_PATH", "lead_bot.db")
