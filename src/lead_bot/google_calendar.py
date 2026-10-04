# lead-bot/src/lead_bot/google_calendar.py
"""Talks to Google Calendar: reads busy times, looks up events, and creates booking events.

Signing in happens once, in a browser (google_login.py), which saves a refresh token to
google_token.json. This module trades that refresh token for a short-lived access token whenever
it needs one, then calls the Calendar API with it. The call server never needs a browser.

Plain httpx2 rather than Google's client library: tests swap in a fake HTTP transport and check
the exact requests sent.

Two kinds of failure, kept apart on purpose:
  - GoogleAuthExpired: the sign-in itself is dead. While the consent screen is in Testing, Google
    expires it after 7 days. Every later calendar call would fail the same way, so the call
    server refuses to start until someone signs in again.
  - CalendarUnavailable: anything else, such as an outage, a timeout, or a response that can't be
    read. The call hands that homeowner to a person and the server carries on.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx2

from .booking import CalendarUnavailable
from .slots import BusyPeriod

# The only two permissions the bot asks for: the narrowest pair that covers what it does,
# checked against Google's published Calendar API definition.
#   calendar.freebusy      - see when the calendar is busy, without reading what the events are
#   calendar.events.owned  - create events, and look up the ones it made, on calendars you own
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/calendar.events.owned",
)

CALENDAR_API_BASE_URL = "https://www.googleapis.com/calendar/v3"

# Google's token endpoint, from its OAuth docs for desktop apps. Used if the saved sign-in
# doesn't name one.
DEFAULT_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Token-endpoint errors that retrying can never fix, so they mean "sign in again".
#   invalid_grant  - the refresh token has expired or been revoked (Google's docs; this is the
#                    error the 7-day Testing limit produces)
#   invalid_client - the OAuth client was deleted or its secret changed (standard OAuth error)
SIGN_IN_DEAD_ERRORS = frozenset({"invalid_grant", "invalid_client"})

# Refresh the access token this many seconds before Google says it expires, so it can't run out
# between being checked and being used.
EXPIRY_MARGIN_SECONDS = 60


class GoogleAuthExpired(Exception):
    """The saved Google sign-in can't be used: missing, expired, or revoked.

    Deliberately NOT a CalendarUnavailable: a call catches that one and hands off, but this one
    means every booking will fail the same way until someone signs in again.
    """


def google_token_path_from_environment() -> Path:
    """Where google_login.py saves the sign-in and the call server reads it. Both call this."""
    return Path(os.environ.get("GOOGLE_TOKEN_PATH", "google_token.json"))


class GoogleSignIn:
    """What google_login.py saves: enough to get fresh access tokens without a browser.

    A plain class rather than a dataclass so the client secret and refresh token never show up
    in a repr, for example in a traceback or a log line.
    """

    def __init__(self, refresh_token: str, client_id: str, client_secret: str, token_url: str) -> None:
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url

    def __repr__(self) -> str:
        return f"GoogleSignIn(client_id={self.client_id!r}, token_url={self.token_url!r})"


def load_google_sign_in(token_path: Path) -> GoogleSignIn:
    """Read the saved sign-in. Raises GoogleAuthExpired if it's missing or unusable."""
    try:
        saved = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:  # missing file, unreadable file, or not JSON
        raise GoogleAuthExpired(f"can't read the Google sign-in at {token_path}: {error}") from None
    if not isinstance(saved, dict):
        raise GoogleAuthExpired(f"{token_path} doesn't contain a saved sign-in")

    def required_text(key: str) -> str:
        value = saved.get(key)
        if not isinstance(value, str) or not value:
            raise GoogleAuthExpired(f"{token_path} has no {key}")
        return value

    token_url = saved.get("token_url")
    return GoogleSignIn(
        refresh_token=required_text("refresh_token"),
        client_id=required_text("client_id"),
        client_secret=required_text("client_secret"),
        token_url=token_url if isinstance(token_url, str) and token_url else DEFAULT_TOKEN_URL,
    )


def json_object(response: httpx2.Response) -> dict[str, Any]:
    """The response body as a dict, or {} if it isn't a JSON object (an HTML error page, say)."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def parse_google_time(raw_time: object) -> datetime:
    """Read one of Google's RFC 3339 timestamps, e.g. "2026-10-05T16:00:00Z".

    Raises ValueError for anything without a timezone: a time with no offset can't be compared
    with the bot's timezone-aware slot times.
    """
    parsed = datetime.fromisoformat(str(raw_time))
    if parsed.tzinfo is None:
        raise ValueError(f"time has no timezone: {raw_time!r}")
    return parsed


class GoogleCalendarClient:
    """One calendar, reached with one saved sign-in."""

    def __init__(
        self,
        sign_in: GoogleSignIn,
        calendar_id: str = "primary",
        http_client: httpx2.Client | None = None,
        timeout_seconds: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sign_in = sign_in
        # "primary" is Google's name for the signed-in account's main calendar.
        self._calendar_id = calendar_id
        self._http = http_client if http_client is not None else httpx2.Client(timeout=timeout_seconds)
        # A monotonic clock only moves forward, so changing the computer's time can't make an
        # expired access token look valid.
        self._clock = clock
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0

    # ---- the three calendar operations ----

    def busy_periods(self, time_min: datetime, time_max: datetime) -> list[BusyPeriod]:
        """Every stretch between time_min and time_max when the calendar is taken."""
        response = self._send(
            "POST",
            "/freeBusy",
            {
                "timeMin": time_min.isoformat(),
                "timeMax": time_max.isoformat(),
                "items": [{"id": self._calendar_id}],
            },
        )
        body = self._json_or_raise(response, "free/busy query")
        calendars = body.get("calendars")
        if not isinstance(calendars, dict) or not calendars:
            raise CalendarUnavailable("free/busy response has no calendars")
        # The answer is keyed by calendar id. If the id asked for isn't a key but only one calendar
        # came back, that one is ours (for example, if "primary" were answered under the account's
        # email address).
        calendar = calendars.get(self._calendar_id)
        if calendar is None and len(calendars) == 1:
            calendar = next(iter(calendars.values()))
        if not isinstance(calendar, dict):
            raise CalendarUnavailable(f"free/busy response has nothing for {self._calendar_id}")
        if calendar.get("errors"):
            # For example notFound: the calendar id is wrong, or this account can't see it.
            raise CalendarUnavailable(f"free/busy error for {self._calendar_id}: {calendar['errors']}")

        periods: list[BusyPeriod] = []
        for period in calendar.get("busy") or []:
            # An unreadable period stops the lookup instead of being skipped: skipping it would
            # treat a busy time as free and could double-book it.
            try:
                periods.append(
                    BusyPeriod(start=parse_google_time(period["start"]), end=parse_google_time(period["end"]))
                )
            except (KeyError, TypeError, ValueError) as error:
                raise CalendarUnavailable(f"unreadable busy period: {period!r}") from error
        return periods

    def booked_event_start(self, event_id: str) -> datetime | None:
        """When the event with this id starts, or None if there's no such event.

        Google keeps deleted events with status "cancelled", and looking one up still returns it,
        so a deleted event counts as no event.
        """
        response = self._send("GET", f"{self._events_path()}/{quote(event_id, safe='')}")
        if response.status_code == 404:
            return None
        body = self._json_or_raise(response, "event lookup")
        if body.get("status") == "cancelled":
            return None
        start = body.get("start")
        try:
            return parse_google_time(start.get("dateTime") if isinstance(start, dict) else None)
        except ValueError as error:
            raise CalendarUnavailable(f"event {event_id} has an unreadable start: {start!r}") from error

    def create_event(
        self,
        *,
        event_id: str,
        start: datetime,
        end: datetime,
        timezone_name: str,
        summary: str,
        description: str,
        location: str,
    ) -> bool:
        """Create the event. Returns False, creating nothing, if this event id already exists.

        start and end must be timezone-aware. timezone_name (e.g. "America/Phoenix") is how
        Google displays the event; the offset inside start and end fixes the actual instant.
        location is the property address, free text; Google shows it as a map link, so the
        inspector can tap it to navigate.
        """
        response = self._send(
            "POST",
            self._events_path(),
            {
                "id": event_id,
                "summary": summary,
                "description": description,
                "location": location,
                "start": {"dateTime": start.isoformat(), "timeZone": timezone_name},
                "end": {"dateTime": end.isoformat(), "timeZone": timezone_name},
            },
        )
        # 409 "duplicate": "The requested identifier already exists" (Google's Calendar error docs).
        if response.status_code == 409:
            return False
        self._json_or_raise(response, "event insert")
        return True

    # ---- plumbing ----

    def _events_path(self) -> str:
        # safe="" also encodes "@" and "/": calendar ids other than "primary" look like email addresses.
        return f"/calendars/{quote(self._calendar_id, safe='')}/events"

    def _send(self, method: str, path: str, json_body: dict[str, Any] | None = None) -> httpx2.Response:
        """One Calendar API request, with a valid access token.

        A 401 means Google rejected the access token before its expiry time (it can revoke one
        early). Get a fresh token and try exactly once more. If the fresh token is rejected too,
        the sign-in itself is dead.
        """
        response = self._request_once(method, path, json_body, self._current_access_token())
        if response.status_code != 401:
            return response
        self._access_token = None  # makes _current_access_token fetch a new one
        response = self._request_once(method, path, json_body, self._current_access_token())
        if response.status_code == 401:
            raise GoogleAuthExpired(
                f"Calendar rejected a freshly refreshed access token: {response.text[:300]}"
            )
        return response

    def _request_once(
        self, method: str, path: str, json_body: dict[str, Any] | None, access_token: str
    ) -> httpx2.Response:
        try:
            return self._http.request(
                method,
                CALENDAR_API_BASE_URL + path,
                json=json_body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx2.HTTPError as error:  # timeouts, refused connections, DNS failures...
            raise CalendarUnavailable(f"{method} {path}: {type(error).__name__}: {error}") from error

    def _current_access_token(self) -> str:
        if self._access_token is not None and self._clock() < self._access_token_expires_at:
            return self._access_token
        return self._refresh_access_token()

    def _refresh_access_token(self) -> str:
        """Trade the saved refresh token for a new access token (they last about an hour)."""
        try:
            response = self._http.post(
                self._sign_in.token_url,
                # Form-encoded, as Google's token endpoint expects.
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._sign_in.refresh_token,
                    "client_id": self._sign_in.client_id,
                    "client_secret": self._sign_in.client_secret,
                },
            )
        except httpx2.HTTPError as error:
            raise CalendarUnavailable(f"token refresh: {type(error).__name__}: {error}") from error

        body = json_object(response)
        if response.status_code >= 400:
            error_code = body.get("error")
            if error_code in SIGN_IN_DEAD_ERRORS:
                raise GoogleAuthExpired(
                    f"token refresh failed {response.status_code}: {error_code}: "
                    f"{body.get('error_description', '')}"
                )
            raise CalendarUnavailable(f"token refresh failed {response.status_code}: {response.text[:300]}")

        access_token = body.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise CalendarUnavailable("token refresh returned no access token")
        expires_in = body.get("expires_in")
        lifetime_seconds = expires_in if isinstance(expires_in, int) else 0  # 0: refresh before every call
        self._access_token = access_token
        self._access_token_expires_at = self._clock() + lifetime_seconds - EXPIRY_MARGIN_SECONDS
        return access_token

    def _json_or_raise(self, response: httpx2.Response, what: str) -> dict[str, Any]:
        if response.status_code >= 400:
            raise CalendarUnavailable(f"{what} failed {response.status_code}: {response.text[:300]}")
        return json_object(response)
