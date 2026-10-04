# lead-bot/src/lead_bot/wiring.py
"""Builds the real services a live call uses: Jev, and the Google Calendar booking.

Kept apart from main.py so the dialer's simulator mode and the server build them the same way.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .booking import CalendarBooking
from .call_session import CallServices
from .google_calendar import (
    GoogleAuthExpired,
    GoogleCalendarClient,
    google_token_path_from_environment,
    load_google_sign_in,
)
from .jev import Jev
from .settings import BusinessSettings
from .slots import BookingRules
from .writer import OpenAIWriter

# On a call, every second of waiting is silence the homeowner hears. One retry, then the call
# hands off to a person ("someone will call you back") rather than leaving them on hold.
JEV_TIMEOUT_SECONDS = 2.5
JEV_RETRIES = 1
CALENDAR_TIMEOUT_SECONDS = 5.0
# The writer runs after "Let me check.", so it gets a little longer than Jev, but no retry: a
# failed draft falls back to the fixed "our team will answer that" line straight away.
WRITER_TIMEOUT_SECONDS = 4.0
# WRITER_MODEL in .env overrides this, so trying another model needs no code change.
DEFAULT_WRITER_MODEL = "gpt-6-luna"


class GoogleBookingLoader:
    """Hands out the calendar booking, reading the Google sign-in the first time a call needs it.

    Reading it lazily means a missing or expired sign-in fails only the booking step of a call
    (which hands off to a person), not the whole server at startup. And because the file is
    re-read whenever it changes, running google_login again fixes an expired sign-in without
    restarting anything.
    """

    def __init__(self, rules: BookingRules, token_path: Path, calendar_id: str) -> None:
        self._rules = rules
        self._token_path = token_path
        self._calendar_id = calendar_id
        self._lock = threading.Lock()  # two calls reaching the booking step at once load it once
        self._booking: CalendarBooking | None = None
        self._loaded_file_version: int | None = None

    def __call__(self) -> CalendarBooking:
        with self._lock:
            try:
                file_version = self._token_path.stat().st_mtime_ns
            except OSError:
                raise GoogleAuthExpired(
                    f"no Google sign-in at {self._token_path}; run: uv run python -m lead_bot.google_login"
                ) from None
            if self._booking is None or file_version != self._loaded_file_version:
                sign_in = load_google_sign_in(self._token_path)
                calendar = GoogleCalendarClient(
                    sign_in, calendar_id=self._calendar_id, timeout_seconds=CALENDAR_TIMEOUT_SECONDS
                )
                self._booking = CalendarBooking(self._rules, calendar)
                self._loaded_file_version = file_version
            return self._booking


def build_call_services(settings: BusinessSettings) -> CallServices:
    missing = [
        name for name in ("TYPESAFE_API_KEY", "OPENAI_API_KEY") if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise RuntimeError(
            f"{' and '.join(missing)} must be set in .env. TYPESAFE_API_KEY: Jev reads what the "
            "homeowner says on every call. OPENAI_API_KEY: the writer answers off-script questions."
        )
    jev = Jev(timeout_seconds=JEV_TIMEOUT_SECONDS, max_retries=JEV_RETRIES)
    booking = GoogleBookingLoader(
        settings.inspection_rules,
        google_token_path_from_environment(),
        calendar_id=os.environ.get("GOOGLE_CALENDAR_ID", "").strip() or "primary",
    )
    writer = OpenAIWriter(
        model=os.environ.get("WRITER_MODEL", "").strip() or DEFAULT_WRITER_MODEL,
        timeout_seconds=WRITER_TIMEOUT_SECONDS,
    )
    return CallServices(ask_jev=jev.ask, booking=booking, writer=writer)
