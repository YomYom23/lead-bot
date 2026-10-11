# lead-bot/tests/test_server.py
# The form, submitted through the real FastAPI app with a temp database and a fixed clock.
from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from conftest import BUSINESS_NAME, DANA_PHONE, MONDAY_10AM, PHOENIX, phoenix
from fastapi.testclient import TestClient

from lead_bot.client_config import EXAMPLE_CLIENT_CONFIG_PATH, load_client_config
from lead_bot.consent import CONSENT_VERSION
from lead_bot.server import create_app
from lead_bot.settings import BusinessSettings
from lead_bot.store import Store
from lead_bot.timing import TextingHours

SETTINGS = BusinessSettings(
    business_name=BUSINESS_NAME,
    timezone=PHOENIX,
    texting_hours=TextingHours(PHOENIX),
    client=load_client_config(EXAMPLE_CLIENT_CONFIG_PATH),
)

GOOD_FORM = {
    "full_name": "Dana Ruiz",
    "phone": "(602) 555-0142",
    "address": "12 Saguaro Ln, Mesa AZ",
    "problem": "active_leak",
    "text_consent": "yes",
    "consent_version": CONSENT_VERSION,
    "website": "",
}


def client_at(store: Store, moment: datetime) -> TestClient:
    return TestClient(create_app(store, SETTINGS, clock=lambda: moment))


def test_the_form_shows_the_consent_text_with_the_business_name(store: Store) -> None:
    page = client_at(store, MONDAY_10AM).get("/").text
    assert f"I agree that {BUSINESS_NAME} may send automated text messages" in page
    assert "Msg &amp; data rates may apply" in page
    assert 'name="text_consent" value="yes">' in page  # rendered unticked
    assert f'value="{CONSENT_VERSION}"' in page


def test_a_consenting_submission_schedules_a_text_right_away(store: Store) -> None:
    response = client_at(store, MONDAY_10AM).post("/leads", data=GOOD_FORM)
    assert response.status_code == 200
    assert "will text you at (602) 555-0142 in the next minute or so" in response.text
    assert "Reply STOP at any time" in response.text
    assert store.counts()["leads"] == {"following_up": 1}
    assert [due.attempt for due in store.due_openings(MONDAY_10AM)] == [1]
    conversation = store.conversation(1)
    assert conversation is not None and conversation.phone == DANA_PHONE


def test_a_late_night_submission_is_told_the_morning_time(store: Store) -> None:
    response = client_at(store, phoenix(2026, 10, 5, 22, 0)).post("/leads", data=GOOD_FORM)
    assert "tomorrow at 8:00 AM to set up" in response.text


def test_an_early_morning_submission_is_told_today(store: Store) -> None:
    response = client_at(store, phoenix(2026, 10, 5, 6, 30)).post("/leads", data=GOOD_FORM)
    assert "today at 8:00 AM" in response.text


def test_without_the_tick_nothing_is_scheduled_and_the_page_promises_no_texts(store: Store) -> None:
    form = {key: value for key, value in GOOD_FORM.items() if key != "text_consent"}
    response = client_at(store, MONDAY_10AM).post("/leads", data=form)
    assert response.status_code == 200
    assert "automated assistant" not in response.text
    assert f"Someone from {BUSINESS_NAME} will be in touch soon" in response.text
    assert store.due_openings(MONDAY_10AM) == []
    assert [alert.reason for alert in store.owner_alerts()] == ["no_text_consent"]


def test_a_tick_on_an_outdated_form_does_not_count(store: Store) -> None:
    # The voice version's form: its consent covered calls, not texts.
    form = {**GOOD_FORM, "consent_version": "call-consent-2026-09-29"}
    client_at(store, MONDAY_10AM).post("/leads", data=form)
    assert store.due_openings(MONDAY_10AM) == []


def test_a_previously_opted_out_number_gets_the_same_neutral_page(store: Store) -> None:
    # The page must not reveal that the number is on the do-not-call list.
    store.add_to_do_not_call(DANA_PHONE, "test", MONDAY_10AM)
    response = client_at(store, MONDAY_10AM).post("/leads", data=GOOD_FORM)
    assert f"Someone from {BUSINESS_NAME} will be in touch soon" in response.text
    assert "opted" not in response.text.lower()
    assert store.due_openings(MONDAY_10AM) == []


def test_a_duplicate_submission_is_told_its_request_is_already_in(store: Store) -> None:
    client = client_at(store, MONDAY_10AM)
    client.post("/leads", data=GOOD_FORM)
    response = client.post("/leads", data=GOOD_FORM)
    assert "We already have your request" in response.text
    assert len(store.due_openings(MONDAY_10AM)) == 1


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
    assert store.counts() == {"leads": {}, "texts_in": {}, "texts_out": {}}


def test_typed_html_is_shown_as_text_not_run(store: Store) -> None:
    form = {**GOOD_FORM, "full_name": "<script>alert(1)</script>", "phone": "bad"}
    response = client_at(store, MONDAY_10AM).post("/leads", data=form)
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_consent_evidence_records_the_public_address_not_the_internal_one(
    store: Store, tmp_path: Path
) -> None:
    # Behind Caddy the server sees http://lead-bot-<client>-server:8000/leads. The record must
    # name the page the homeowner actually used, which is PUBLIC_BASE_URL plus the path.
    public_settings = replace(SETTINGS, public_base_url="https://demo.bot.example.com/")
    app = create_app(store, public_settings, clock=lambda: MONDAY_10AM)
    internal_client = TestClient(app, base_url="http://lead-bot-demo-server:8000")
    internal_client.post("/leads", data=GOOD_FORM)
    with sqlite3.connect(tmp_path / "leads.db") as connection:
        recorded_sources = [row[0] for row in connection.execute("SELECT source FROM consents")]
    assert recorded_sources == ["https://demo.bot.example.com/leads"]


def test_health_reports_counts(store: Store) -> None:
    client = client_at(store, MONDAY_10AM)
    store.record_worker_heartbeat(MONDAY_10AM)
    client.post("/leads", data=GOOD_FORM)
    assert client.get("/health").json() == {
        "ok": True,
        "leads": {"following_up": 1},
        "texts_in": {},
        "texts_out": {},
    }


def test_health_fails_when_the_worker_has_never_reported(store: Store) -> None:
    response = client_at(store, MONDAY_10AM).get("/health")
    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert response.json()["problem"] == "worker has not reported yet"


def test_health_is_ok_while_the_worker_heartbeat_is_fresh(store: Store) -> None:
    store.record_worker_heartbeat(MONDAY_10AM)
    # 90 seconds old is under the two-minute limit; 150 seconds is the stale case below.
    response = client_at(store, MONDAY_10AM + timedelta(seconds=90)).get("/health")
    assert response.status_code == 200


def test_health_fails_when_the_worker_heartbeat_is_stale(store: Store) -> None:
    store.record_worker_heartbeat(MONDAY_10AM)
    response = client_at(store, MONDAY_10AM + timedelta(seconds=150)).get("/health")
    assert response.status_code == 503
    assert response.json()["ok"] is False
    assert response.json()["problem"] == "worker heartbeat is 150 seconds old"
