# lead-bot/tests/test_google_calendar.py
# The real GoogleCalendarClient against a fake HTTP transport. The fake answers in the shapes
# Google's Calendar API definition and error docs describe, and each test checks the exact
# request the client sent.
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx2
import pytest

from lead_bot.booking import CalendarUnavailable
from lead_bot.google_calendar import (
    CALENDAR_API_BASE_URL,
    DEFAULT_TOKEN_URL,
    GoogleAuthExpired,
    GoogleCalendarClient,
    GoogleSignIn,
    load_google_sign_in,
)
from lead_bot.google_login import save_google_sign_in
from lead_bot.slots import BusyPeriod

SIGN_IN = GoogleSignIn(
    refresh_token="test-refresh-token",
    client_id="test-client-id.apps.googleusercontent.com",
    client_secret="test-client-secret",
    token_url=DEFAULT_TOKEN_URL,
)
TOKEN_OK = {"access_token": "access-1", "expires_in": 3599, "token_type": "Bearer"}
OCT_5_UTC = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)


class FakeGoogle:
    """Answers the token endpoint and the Calendar API, and records every request."""

    def __init__(self, calendar_handler: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.calendar_handler = calendar_handler
        self.token_responses: list[httpx2.Response] = []  # used in order; the last one repeats
        self.token_requests: list[dict[str, list[str]]] = []
        self.calendar_requests: list[httpx2.Request] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        if str(request.url) == DEFAULT_TOKEN_URL:
            self.token_requests.append(parse_qs(request.content.decode()))
            if len(self.token_responses) > 1:
                return self.token_responses.pop(0)
            return self.token_responses[0] if self.token_responses else httpx2.Response(200, json=TOKEN_OK)
        self.calendar_requests.append(request)
        return self.calendar_handler(request)

    def client(
        self, calendar_id: str = "primary", clock: Callable[[], float] = lambda: 0.0
    ) -> GoogleCalendarClient:
        return GoogleCalendarClient(
            SIGN_IN,
            calendar_id=calendar_id,
            http_client=httpx2.Client(transport=httpx2.MockTransport(self.handle)),
            clock=clock,
        )


def freebusy_body(busy: list[dict[str, str]], calendar_key: str = "primary") -> dict[str, Any]:
    return {
        "kind": "calendar#freeBusy",
        "timeMin": "2026-10-05T00:00:00.000Z",
        "timeMax": "2026-10-20T00:00:00.000Z",
        "calendars": {calendar_key: {"busy": busy}},
    }


# ---- free/busy ----


def test_busy_periods_sends_the_documented_request_and_parses_the_answer() -> None:
    google = FakeGoogle(
        lambda _request: httpx2.Response(
            200, json=freebusy_body([{"start": "2026-10-05T16:00:00Z", "end": "2026-10-05T17:00:00Z"}])
        )
    )
    periods = google.client().busy_periods(OCT_5_UTC, OCT_5_UTC + timedelta(days=15))

    [request] = google.calendar_requests
    assert request.method == "POST"
    assert str(request.url) == f"{CALENDAR_API_BASE_URL}/freeBusy"
    assert request.headers["Authorization"] == "Bearer access-1"
    assert json.loads(request.content) == {
        "timeMin": "2026-10-05T00:00:00+00:00",
        "timeMax": "2026-10-20T00:00:00+00:00",
        "items": [{"id": "primary"}],
    }
    assert periods == [
        BusyPeriod(datetime(2026, 10, 5, 16, tzinfo=UTC), datetime(2026, 10, 5, 17, tzinfo=UTC))
    ]


def test_the_token_request_uses_the_saved_refresh_token() -> None:
    google = FakeGoogle(lambda _request: httpx2.Response(200, json=freebusy_body([])))
    google.client().busy_periods(OCT_5_UTC, OCT_5_UTC + timedelta(days=1))
    assert google.token_requests == [
        {
            "grant_type": ["refresh_token"],
            "refresh_token": ["test-refresh-token"],
            "client_id": ["test-client-id.apps.googleusercontent.com"],
            "client_secret": ["test-client-secret"],
        }
    ]


def test_an_access_token_is_reused_until_it_nearly_expires() -> None:
    google = FakeGoogle(lambda _request: httpx2.Response(200, json=freebusy_body([])))
    current_time = [0.0]
    client = google.client(clock=lambda: current_time[0])

    client.busy_periods(OCT_5_UTC, OCT_5_UTC + timedelta(days=1))
    current_time[0] = 3000.0  # 50 minutes later: the 3599-second token is still good
    client.busy_periods(OCT_5_UTC, OCT_5_UTC + timedelta(days=1))
    assert len(google.token_requests) == 1

    current_time[0] = 3550.0  # inside the last minute: refresh before it can run out mid-request
    client.busy_periods(OCT_5_UTC, OCT_5_UTC + timedelta(days=1))
    assert len(google.token_requests) == 2


def test_a_calendar_answered_under_another_key_is_still_read() -> None:
    google = FakeGoogle(
        lambda _request: httpx2.Response(
            200,
            json=freebusy_body(
                [{"start": "2026-10-05T16:00:00Z", "end": "2026-10-05T17:00:00Z"}],
                calendar_key="owner@example.com",
            ),
        )
    )
    assert len(google.client().busy_periods(OCT_5_UTC, OCT_5_UTC + timedelta(days=1))) == 1


def test_a_calendar_error_inside_free_busy_is_not_read_as_free() -> None:
    # A wrong GOOGLE_CALENDAR_ID comes back as 200 with an error, not as an empty busy list.
    body = {"calendars": {"primary": {"errors": [{"domain": "global", "reason": "notFound"}], "busy": []}}}
    google = FakeGoogle(lambda _request: httpx2.Response(200, json=body))
    with pytest.raises(CalendarUnavailable, match="notFound"):
        google.client().busy_periods(OCT_5_UTC, OCT_5_UTC + timedelta(days=1))


def test_an_unreadable_busy_period_is_not_skipped() -> None:
    google = FakeGoogle(
        lambda _request: httpx2.Response(200, json=freebusy_body([{"start": "2026-10-05T16:00:00"}]))
    )
    with pytest.raises(CalendarUnavailable, match="unreadable busy period"):
        google.client().busy_periods(OCT_5_UTC, OCT_5_UTC + timedelta(days=1))


def test_a_server_error_is_calendar_unavailable() -> None:
    body = {"error": {"errors": [{"domain": "global", "reason": "backendError"}], "code": 500}}
    google = FakeGoogle(lambda _request: httpx2.Response(500, json=body))
    with pytest.raises(CalendarUnavailable, match="500"):
        google.client().busy_periods(OCT_5_UTC, OCT_5_UTC + timedelta(days=1))


def test_a_timeout_is_calendar_unavailable() -> None:
    def time_out(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    google = FakeGoogle(time_out)
    with pytest.raises(CalendarUnavailable, match="ReadTimeout"):
        google.client().busy_periods(OCT_5_UTC, OCT_5_UTC + timedelta(days=1))


# ---- the sign-in dying ----


def test_an_expired_sign_in_raises_google_auth_expired() -> None:
    # What Google's token endpoint answers once the 7-day Testing limit has passed.
    google = FakeGoogle(lambda _request: httpx2.Response(200, json=freebusy_body([])))
    google.token_responses = [
        httpx2.Response(
            400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
        )
    ]
    with pytest.raises(GoogleAuthExpired, match="invalid_grant"):
        google.client().busy_periods(OCT_5_UTC, OCT_5_UTC + timedelta(days=1))
    assert google.calendar_requests == []  # never reached the calendar


def test_a_token_endpoint_outage_is_not_mistaken_for_an_expired_sign_in() -> None:
    google = FakeGoogle(lambda _request: httpx2.Response(200, json=freebusy_body([])))
    google.token_responses = [httpx2.Response(503, text="<html>Service Unavailable</html>")]
    with pytest.raises(CalendarUnavailable, match="503"):
        google.client().busy_periods(OCT_5_UTC, OCT_5_UTC + timedelta(days=1))


def test_a_rejected_access_token_is_refreshed_once_then_retried() -> None:
    answers = [
        httpx2.Response(401, json={"error": {"errors": [{"reason": "authError"}], "code": 401}}),
        httpx2.Response(200, json=freebusy_body([])),
    ]
    google = FakeGoogle(lambda _request: answers.pop(0))
    google.token_responses = [
        httpx2.Response(200, json=TOKEN_OK),
        httpx2.Response(200, json={**TOKEN_OK, "access_token": "access-2"}),
    ]
    assert google.client().busy_periods(OCT_5_UTC, OCT_5_UTC + timedelta(days=1)) == []
    assert [request.headers["Authorization"] for request in google.calendar_requests] == [
        "Bearer access-1",
        "Bearer access-2",
    ]


def test_a_fresh_access_token_rejected_too_means_the_sign_in_is_dead() -> None:
    google = FakeGoogle(lambda _request: httpx2.Response(401, json={"error": {"code": 401}}))
    with pytest.raises(GoogleAuthExpired):
        google.client().busy_periods(OCT_5_UTC, OCT_5_UTC + timedelta(days=1))
    assert len(google.calendar_requests) == 2  # one retry, not a loop


# ---- events ----


def test_create_event_sends_id_times_timezone_and_address() -> None:
    def echo_the_event(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={**json.loads(request.content), "status": "confirmed"})

    google = FakeGoogle(echo_the_event)
    created = google.client(calendar_id="owner@example.com").create_event(
        event_id="abc123def",
        start=datetime(2026, 10, 5, 16, 0, tzinfo=UTC),
        end=datetime(2026, 10, 5, 17, 0, tzinfo=UTC),
        timezone_name="America/Phoenix",
        summary="Roof inspection",
        description="details",
        location="4410 E Camelback Rd, Phoenix AZ 85018",
    )
    assert created is True
    [request] = google.calendar_requests
    # The calendar id is URL-encoded: "@" must not reach the path as-is.
    assert str(request.url) == f"{CALENDAR_API_BASE_URL}/calendars/owner%40example.com/events"
    assert json.loads(request.content) == {
        "id": "abc123def",
        "summary": "Roof inspection",
        "description": "details",
        "location": "4410 E Camelback Rd, Phoenix AZ 85018",
        "start": {"dateTime": "2026-10-05T16:00:00+00:00", "timeZone": "America/Phoenix"},
        "end": {"dateTime": "2026-10-05T17:00:00+00:00", "timeZone": "America/Phoenix"},
    }


def test_create_event_reports_a_duplicate_id_without_raising() -> None:
    body = {"error": {"errors": [{"domain": "global", "reason": "duplicate"}], "code": 409}}
    google = FakeGoogle(lambda _request: httpx2.Response(409, json=body))
    created = google.client().create_event(
        event_id="abc123def",
        start=datetime(2026, 10, 5, 16, tzinfo=UTC),
        end=datetime(2026, 10, 5, 17, tzinfo=UTC),
        timezone_name="America/Phoenix",
        summary="s",
        description="d",
        location="l",
    )
    assert created is False


def test_booked_event_start_reads_the_events_start() -> None:
    google = FakeGoogle(
        lambda _request: httpx2.Response(
            200, json={"id": "abc", "status": "confirmed", "start": {"dateTime": "2026-10-05T09:00:00-07:00"}}
        )
    )
    start = google.client().booked_event_start("abc")
    assert start == datetime(2026, 10, 5, 16, 0, tzinfo=UTC)
    assert str(google.calendar_requests[0].url) == f"{CALENDAR_API_BASE_URL}/calendars/primary/events/abc"


@pytest.mark.parametrize(
    "response",
    [
        httpx2.Response(404, json={"error": {"errors": [{"reason": "notFound"}], "code": 404}}),
        # Google keeps a deleted event and returns it with status "cancelled".
        httpx2.Response(200, json={"id": "abc", "status": "cancelled"}),
    ],
)
def test_a_missing_or_deleted_event_has_no_start(response: httpx2.Response) -> None:
    google = FakeGoogle(lambda _request: response)
    assert google.client().booked_event_start("abc") is None


# ---- the saved sign-in ----


def test_saved_sign_in_round_trips(tmp_path: Path) -> None:
    token_path = tmp_path / "google_token.json"
    save_google_sign_in(token_path, SIGN_IN)
    loaded = load_google_sign_in(token_path)
    assert (loaded.refresh_token, loaded.client_id, loaded.client_secret, loaded.token_url) == (
        SIGN_IN.refresh_token,
        SIGN_IN.client_id,
        SIGN_IN.client_secret,
        SIGN_IN.token_url,
    )


def test_a_missing_sign_in_file_raises_google_auth_expired(tmp_path: Path) -> None:
    with pytest.raises(GoogleAuthExpired, match="can't read"):
        load_google_sign_in(tmp_path / "google_token.json")


def test_a_sign_in_file_without_a_refresh_token_is_rejected(tmp_path: Path) -> None:
    token_path = tmp_path / "google_token.json"
    token_path.write_text(json.dumps({"client_id": "x", "client_secret": "y"}), encoding="utf-8")
    with pytest.raises(GoogleAuthExpired, match="refresh_token"):
        load_google_sign_in(token_path)


def test_the_secrets_never_appear_in_the_repr() -> None:
    assert "test-refresh-token" not in repr(SIGN_IN)
    assert "test-client-secret" not in repr(SIGN_IN)
