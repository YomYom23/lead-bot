# lead-bot/src/lead_bot/wiring.py
"""Builds the real services a conversation uses: Jev, the Google Calendar booking, and the writer.

Kept apart from worker.py so the worker, the simulator and the scenario runner build them the
same way.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .booking import CalendarBooking
from .conversation import ConversationServices
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

# A text reply can take a few seconds without anyone noticing, unlike a pause on a phone call, so
# these are far looser than the voice version's. If Jev still fails, the worker tries the same
# text again a little later (worker.RETRY_DELAY) before handing it to a person.
JEV_TIMEOUT_SECONDS = 10.0
JEV_RETRIES = 2
CALENDAR_TIMEOUT_SECONDS = 10.0
# No retry for the writer: a failed draft falls back to the fixed "I'll pass that question to our
# team" text, so a retry would only delay an answer that's already safe.
WRITER_TIMEOUT_SECONDS = 15.0
# WRITER_MODEL in .env overrides this, so trying another model needs no code change.
DEFAULT_WRITER_MODEL = "gpt-6-luna"


class GoogleBookingLoader:
    """Hands out the calendar booking, reading the Google sign-in the first time a text needs it.

    Reading it lazily means a missing or expired sign-in fails only the booking step of one
    conversation (which hands off to a person), not the whole worker at startup. And because the
    file is re-read whenever it changes, running google_login again fixes an expired sign-in
    without restarting anything.
    """

    def __init__(self, rules: BookingRules, token_path: Path, calendar_id: str) -> None:
        self._rules = rules
        self._token_path = token_path
        self._calendar_id = calendar_id
        self._lock = threading.Lock()  # two conversations reaching the booking step at once load it once
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


def build_jev() -> Jev:
    return Jev(timeout_seconds=JEV_TIMEOUT_SECONDS, max_retries=JEV_RETRIES)


def build_writer() -> OpenAIWriter:
    return OpenAIWriter(
        model=os.environ.get("WRITER_MODEL", "").strip() or DEFAULT_WRITER_MODEL,
        timeout_seconds=WRITER_TIMEOUT_SECONDS,
    )


def require_model_keys() -> None:
    """Stop with a clear message, naming what's missing, before anything is sent."""
    missing = [
        name for name in ("TYPESAFE_API_KEY", "OPENAI_API_KEY") if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise RuntimeError(
            f"{' and '.join(missing)} must be set in .env. TYPESAFE_API_KEY: Jev reads every text "
            "the homeowner sends. OPENAI_API_KEY: the writer answers off-script questions."
        )


def build_conversation_services(settings: BusinessSettings) -> ConversationServices:
    require_model_keys()
    booking = GoogleBookingLoader(
        settings.inspection_rules,
        google_token_path_from_environment(),
        calendar_id=os.environ.get("GOOGLE_CALENDAR_ID", "").strip() or "primary",
    )
    return ConversationServices(ask_jev=build_jev().ask, booking=booking, writer=build_writer())
