# lead-bot/tests/test_server.py
# The form, submitted through the real FastAPI app with a temp database and a fixed clock.
from __future__ import annotations

from datetime import datetime

import pytest
from conftest import BUSINESS_NAME, DANA_PHONE, MONDAY_10AM, PHOENIX, phoenix
from fastapi.testclient import TestClient

from lead_bot.consent import CONSENT_VERSION
from lead_bot.server import create_app
from lead_bot.settings import BusinessSettings
from lead_bot.store import Store
from lead_bot.timing import CallingHours

SETTINGS = BusinessSettings(
    business_name=BUSINESS_NAME, timezone=PHOENIX, calling_hours=CallingHours(PHOENIX)
)

GOOD_FORM = {
    "full_name": "Dana Ruiz",
    "phone": "(602) 555-0142",
    "address": "12 Saguaro Ln, Mesa AZ",
    "problem": "active_leak",
    "call_consent": "yes",
    "consent_version": CONSENT_VERSION,
    "website": "",
}


def client_at(store: Store, moment: datetime) -> TestClient:
    return TestClient(create_app(store, SETTINGS, clock=lambda: moment))


def test_the_form_shows_the_consent_text_with_the_business_name(store: Store) -> None:
    page = client_at(store, MONDAY_10AM).get("/").text
    assert f"I agree that {BUSINESS_NAME} may call me" in page
    assert "artificial (AI-generated) voice" in page
    assert 'name="call_consent" value="yes">' in page  # rendered unticked
    assert f'value="{CONSENT_VERSION}"' in page


def test_a_consenting_submission_schedules_a_call_right_away(store: Store) -> None:
    response = client_at(store, MONDAY_10AM).post("/leads", data=GOOD_FORM)
    assert response.status_code == 200
    assert "will call you at (602) 555-0142 in the next minute or so" in response.text
    assert store.counts() == {"leads": {"following_up": 1}, "calls": {"scheduled": 1}}
    due_call = store.claim_due_call(MONDAY_10AM)
    assert due_call is not None and due_call.phone == DANA_PHONE and due_call.consent_given


def test_a_late_night_submission_is_told_the_morning_time(store: Store) -> None:
    response = client_at(store, phoenix(2026, 10, 5, 22, 0)).post("/leads", data=GOOD_FORM)
    assert "tomorrow at 8:00 AM (the earliest time we're allowed to call) to set up" in response.text


def test_an_early_morning_submission_is_told_today(store: Store) -> None:
    response = client_at(store, phoenix(2026, 10, 5, 6, 30)).post("/leads", data=GOOD_FORM)
    assert "today at 8:00 AM" in response.text


def test_without_the_tick_nothing_is_scheduled_and_the_page_promises_no_ai_call(store: Store) -> None:
    form = {key: value for key, value in GOOD_FORM.items() if key != "call_consent"}
    response = client_at(store, MONDAY_10AM).post("/leads", data=form)
    assert response.status_code == 200
    assert "automated assistant" not in response.text
    assert f"Someone from {BUSINESS_NAME} will be in touch soon" in response.text
    assert store.counts()["calls"] == {}
    assert [alert.reason for alert in store.owner_alerts()] == ["no_call_consent"]


def test_a_tick_on_an_outdated_form_does_not_count(store: Store) -> None:
    form = {**GOOD_FORM, "consent_version": "call-consent-2020-01-01"}
    client_at(store, MONDAY_10AM).post("/leads", data=form)
    assert store.counts()["calls"] == {}


def test_a_previously_opted_out_number_gets_the_same_neutral_page(store: Store) -> None:
    # The page must not reveal that the number is on the do-not-call list.
    store.add_to_do_not_call(DANA_PHONE, "test", MONDAY_10AM)
    response = client_at(store, MONDAY_10AM).post("/leads", data=GOOD_FORM)
    assert f"Someone from {BUSINESS_NAME} will be in touch soon" in response.text
    assert "opt" not in response.text.lower()
    assert store.counts()["calls"] == {}


def test_a_duplicate_submission_is_told_its_request_is_already_in(store: Store) -> None:
    client = client_at(store, MONDAY_10AM)
    client.post("/leads", data=GOOD_FORM)
    response = client.post("/leads", data=GOOD_FORM)
    assert "We already have your request" in response.text
    assert store.counts()["calls"] == {"scheduled": 1}


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("phone", "555-0142", "10-digit US phone number"),
        ("full_name", "", "Please enter your name"),
        ("problem", "gutters", "what&#x27;s going on with the roof"),
    ],
)
def test_a_bad_field_redisplays_the_form_with_entries_kept(
    store: Store, field: str, value: str, message: str
) -> None:
    response = client_at(store, MONDAY_10AM).post("/leads", data={**GOOD_FORM, field: value})
    assert response.status_code == 400
    assert message in response.text
    assert 'value="12 Saguaro Ln, Mesa AZ"' in response.text  # what they typed is still there
    assert 'value="yes" checked' in response.text  # and so is their tick
    assert store.counts()["leads"] == {}


def test_the_bot_trap_stores_nothing_but_looks_normal(store: Store) -> None:
    response = client_at(store, MONDAY_10AM).post("/leads", data={**GOOD_FORM, "website": "spam.example"})
    assert response.status_code == 200
    assert "Thank you" in response.text
    assert store.counts() == {"leads": {}, "calls": {}}


def test_typed_html_is_shown_as_text_not_run(store: Store) -> None:
    form = {**GOOD_FORM, "full_name": "<script>alert(1)</script>", "phone": "bad"}
    response = client_at(store, MONDAY_10AM).post("/leads", data=form)
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_health_reports_counts(store: Store) -> None:
    client = client_at(store, MONDAY_10AM)
    client.post("/leads", data=GOOD_FORM)
    assert client.get("/health").json() == {
        "ok": True,
        "leads": {"following_up": 1},
        "calls": {"scheduled": 1},
    }
