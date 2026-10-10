# lead-bot/tests/test_twilio.py
# Twilio: the two webhooks (signature, saving, redelivery), delivery reports, and TwilioSender
# driven through Twilio's own client with a fake HTTP layer. No request leaves this machine.
from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import pytest
from conftest import DANA_PHONE, MONDAY_10AM, PHOENIX, make_consent, make_lead
from fastapi.testclient import TestClient
from text_fakes import SETTINGS as FAKES_SETTINGS
from text_fakes import Clock, RecordingSender, ScriptedJev, make_services, make_worker, start_conversation
from twilio.http.response import Response as TwilioResponse
from twilio.request_validator import RequestValidator

from lead_bot.client_config import EXAMPLE_CLIENT_CONFIG_PATH, load_client_config
from lead_bot.server import EMPTY_TWIML, create_app
from lead_bot.settings import BusinessSettings
from lead_bot.store import Store
from lead_bot.timing import TextingHours
from lead_bot.twilio_sms import (
    INCOMING_PATH,
    SEND_TIMEOUT_SECONDS,
    STATUS_PATH,
    TwilioSender,
    TwilioSettings,
    request_is_from_twilio,
    status_callback_url,
)
from lead_bot.worker import Sender, SenderCredentialsRejected, SendRefused, Worker

# Made-up values: an account SID is AC + 32 hex characters, a token 32.
ACCOUNT_SID = "AC" + "0" * 32
AUTH_TOKEN = "f" * 32
PUBLIC_URL = "https://lead-bot.example.ts.net"
SETTINGS = BusinessSettings(
    business_name="Desert Ridge Roofing LLC",
    timezone=PHOENIX,
    texting_hours=TextingHours(PHOENIX),
    client=load_client_config(EXAMPLE_CLIENT_CONFIG_PATH),
    public_base_url=PUBLIC_URL,
)


def twilio_settings() -> TwilioSettings:
    # The texting number is built from the test's own phone constant, never typed out.
    return TwilioSettings(ACCOUNT_SID, AUTH_TOKEN, DANA_PHONE.replace("+1602", "+1866"))


def client(store: Store, configured: bool = True) -> TestClient:
    twilio = twilio_settings() if configured else None
    return TestClient(create_app(store, SETTINGS, clock=lambda: MONDAY_10AM, twilio=twilio))


def incoming_form(body: str = "yes", message_sid: str = "SM" + "1" * 32) -> dict[str, str]:
    """The fields Twilio posts for an incoming SMS (twilio.com/docs/messaging/guides/webhook-request)."""
    return {
        "MessageSid": message_sid,
        "SmsSid": message_sid,
        "AccountSid": ACCOUNT_SID,
        "From": DANA_PHONE,
        "To": twilio_settings().phone_number,
        "Body": body,
        "NumMedia": "0",
        "NumSegments": "1",
    }


def post_signed(
    test_client: TestClient, path: str, form: dict[str, str], token: str = AUTH_TOKEN, url: str | None = None
) -> Any:
    """POST `form` the way Twilio does: urlencoded, signed over the public URL and the fields."""
    signed_url = url if url is not None else PUBLIC_URL + path
    signature = RequestValidator(token).compute_signature(signed_url, form)
    return test_client.post(
        path,
        content=urlencode(form),
        headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature},
    )


# ---- the signature ----


def test_twilios_own_documented_example_validates() -> None:
    # The worked example from Twilio's webhook security docs: this token, URL and fields give
    # this signature. It pins the validator to Twilio's real algorithm, not to our own fake.
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    form = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+12349013030",
        "Digits": "1234",
        "From": "+12349013030",
        "To": "+18005551212",
    }
    assert request_is_from_twilio("12345", url, form, "0/KCTR6DLpKmkAf8muzZqo1nDgQ=")
    assert not request_is_from_twilio(
        "12345", url, {**form, "Digits": "9999"}, "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
    )


@pytest.mark.parametrize("signature", [None, "", "not-a-signature"])
def test_a_missing_or_wrong_signature_is_refused(signature: str | None) -> None:
    assert not request_is_from_twilio(AUTH_TOKEN, PUBLIC_URL + INCOMING_PATH, incoming_form(), signature)


def test_an_empty_auth_token_never_validates() -> None:
    form = incoming_form()
    forged = RequestValidator("").compute_signature(PUBLIC_URL + INCOMING_PATH, form)
    assert not request_is_from_twilio("", PUBLIC_URL + INCOMING_PATH, form, forged)


# ---- POST /twilio/incoming ----


def test_a_signed_incoming_text_is_saved_and_answered_with_empty_twiml(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, SETTINGS.texting_hours)
    response = post_signed(client(store), INCOMING_PATH, incoming_form("Yes, that's the address"))
    assert response.status_code == 200
    assert response.text == EMPTY_TWIML
    assert response.headers["content-type"].startswith("text/xml")
    incoming = store.claim_incoming_text(MONDAY_10AM)
    assert incoming is not None
    assert (incoming.phone, incoming.body) == (DANA_PHONE, "Yes, that's the address")
    assert incoming.conversation is not None  # matched to the lead by phone


def test_an_unsigned_post_is_refused_and_nothing_is_saved(store: Store) -> None:
    response = client(store).post(INCOMING_PATH, data=incoming_form())
    assert response.status_code == 403
    assert store.counts()["texts_in"] == {}


def test_a_post_signed_with_another_token_is_refused(store: Store) -> None:
    response = post_signed(client(store), INCOMING_PATH, incoming_form(), token="0" * 32)
    assert response.status_code == 403
    assert store.counts()["texts_in"] == {}


def test_the_signature_is_checked_against_the_public_url_not_the_local_one(store: Store) -> None:
    # Behind Funnel the server sees http://testserver/...; Twilio signed the https:// address.
    local_url = "http://testserver" + INCOMING_PATH
    response = post_signed(client(store), INCOMING_PATH, incoming_form(), url=local_url)
    assert response.status_code == 403


def test_a_changed_body_breaks_the_signature(store: Store) -> None:
    form = incoming_form("yes")
    signature = RequestValidator(AUTH_TOKEN).compute_signature(PUBLIC_URL + INCOMING_PATH, form)
    response = client(store).post(
        INCOMING_PATH,
        content=urlencode({**form, "Body": "STOP"}),
        headers={"Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature},
    )
    assert response.status_code == 403


def test_a_field_twilio_adds_later_is_still_signed_correctly(store: Store) -> None:
    # Twilio adds webhook fields without notice; the check must sign whatever arrives.
    form = {**incoming_form(), "AddOns": json.dumps({"status": "successful"}), "FromCountry": "US"}
    assert post_signed(client(store), INCOMING_PATH, form).status_code == 200
    assert store.counts()["texts_in"] == {"received": 1}


def test_a_redelivered_text_is_saved_once(store: Store) -> None:
    test_client = client(store)
    for _ in range(2):
        assert post_signed(test_client, INCOMING_PATH, incoming_form()).status_code == 200
    assert store.counts()["texts_in"] == {"received": 1}


def test_unicode_in_a_text_is_signed_and_saved_as_sent(store: Store) -> None:
    body = "Sí, it’s the right address \U0001f44d"
    assert post_signed(client(store), INCOMING_PATH, incoming_form(body)).status_code == 200
    incoming = store.claim_incoming_text(MONDAY_10AM)
    assert incoming is not None and incoming.body == body


def test_a_text_for_another_twilio_account_is_ignored(store: Store) -> None:
    form = {**incoming_form(), "AccountSid": "AC" + "9" * 32}
    response = post_signed(client(store), INCOMING_PATH, form)
    assert response.status_code == 200
    assert store.counts()["texts_in"] == {}


def test_without_twilio_settings_the_webhook_says_so(store: Store) -> None:
    response = post_signed(client(store, configured=False), INCOMING_PATH, incoming_form())
    assert response.status_code == 503
    assert store.counts()["texts_in"] == {}


# ---- POST /twilio/status and delivery reports ----


def sent_text(store: Store) -> tuple[int, str]:
    """A lead whose first text went out through a fake sender. Returns (lead id, MessageSid)."""
    clock = Clock()
    sender = RecordingSender()
    worker, _ = make_worker(store, ScriptedJev(), clock, sender=sender)
    lead_id = start_conversation(store, worker, clock)
    transcript = store.transcript(lead_id)
    assert [line.status for line in transcript] == ["sent"]
    return lead_id, sender.message_ids[0]


def status_form(message_sid: str, status: str, error_code: str | None = None) -> dict[str, str]:
    form = {"MessageSid": message_sid, "AccountSid": ACCOUNT_SID, "MessageStatus": status}
    if error_code is not None:
        form["ErrorCode"] = error_code
    return form


def alerts(store: Store) -> list[str]:
    return [alert.reason for alert in store.owner_alerts()]


def test_a_delivered_report_marks_the_text_delivered(store: Store) -> None:
    lead_id, message_sid = sent_text(store)
    assert post_signed(client(store), STATUS_PATH, status_form(message_sid, "delivered")).status_code == 200
    assert [line.status for line in store.transcript(lead_id)] == ["delivered"]
    assert alerts(store) == []


def test_an_undelivered_text_alerts_the_owner_with_twilios_error_code(store: Store) -> None:
    lead_id, message_sid = sent_text(store)
    post_signed(client(store), STATUS_PATH, status_form(message_sid, "undelivered", "30032"))
    assert [line.status for line in store.transcript(lead_id)] == ["undelivered"]
    assert alerts(store) == ["text_not_delivered:30032"]


def test_reports_out_of_order_never_undo_a_final_status(store: Store) -> None:
    lead_id, message_sid = sent_text(store)
    test_client = client(store)
    post_signed(test_client, STATUS_PATH, status_form(message_sid, "delivered"))
    post_signed(test_client, STATUS_PATH, status_form(message_sid, "sent"))  # late, older news
    assert [line.status for line in store.transcript(lead_id)] == ["delivered"]


def test_out_of_order_reports_before_the_send_is_recorded_keep_the_final_one(store: Store) -> None:
    # Both reports land before the worker writes down the MessageSid. If the late 'sent' replaced
    # the saved 'undelivered', the owner would never hear that the text didn't arrive.
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, SETTINGS.texting_hours)
    store.queue_opening_text(added.lead_id, 1, "first", MONDAY_10AM, SETTINGS.texting_hours)
    outgoing = store.claim_queued_text()
    assert outgoing is not None
    store.record_delivery_report("SM" + "8" * 32, "undelivered", "30032", MONDAY_10AM)
    store.record_delivery_report("SM" + "8" * 32, "sent", None, MONDAY_10AM)
    store.mark_text_sent(outgoing.text_id, "SM" + "8" * 32, MONDAY_10AM)
    assert [line.status for line in store.transcript(added.lead_id)] == ["undelivered"]
    assert alerts(store) == ["text_not_delivered:30032"]


def test_the_same_failure_reported_twice_alerts_once(store: Store) -> None:
    _, message_sid = sent_text(store)
    for _ in range(2):
        store.record_delivery_report(message_sid, "failed", "30007", MONDAY_10AM)
    assert alerts(store) == ["text_not_delivered:30007"]


def test_a_report_that_beats_the_send_record_is_applied_when_the_send_is_recorded(store: Store) -> None:
    # Twilio can report before the worker has written down the MessageSid it got back.
    added = store.add_lead(make_lead(), make_consent(), MONDAY_10AM, SETTINGS.texting_hours)
    assert (
        store.queue_opening_text(added.lead_id, 1, "first", MONDAY_10AM, SETTINGS.texting_hours) == "queued"
    )
    outgoing = store.claim_queued_text()
    assert outgoing is not None
    store.record_delivery_report("SM" + "7" * 32, "undelivered", "30032", MONDAY_10AM)
    store.mark_text_sent(outgoing.text_id, "SM" + "7" * 32, MONDAY_10AM)
    assert [line.status for line in store.transcript(added.lead_id)] == ["undelivered"]
    assert alerts(store) == ["text_not_delivered:30032"]


def test_an_undelivered_text_is_left_out_of_the_writers_history(store: Store) -> None:
    lead_id, message_sid = sent_text(store)
    store.record_delivery_report(message_sid, "undelivered", "30032", MONDAY_10AM)
    store.record_incoming_text(DANA_PHONE, "hello?", "SM" + "2" * 32, MONDAY_10AM + timedelta(minutes=1))
    incoming = store.claim_incoming_text(MONDAY_10AM + timedelta(minutes=1))
    assert incoming is not None and incoming.history == []


def test_an_unsigned_status_report_is_refused(store: Store) -> None:
    _, message_sid = sent_text(store)
    response = client(store).post(STATUS_PATH, data=status_form(message_sid, "undelivered", "30032"))
    assert response.status_code == 403
    assert alerts(store) == []


# ---- TwilioSettings ----


def test_no_twilio_values_means_the_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"):
        monkeypatch.delenv(name, raising=False)
    assert TwilioSettings.from_environment() is None


def test_a_half_filled_env_names_every_missing_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT_SID)
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_PHONE_NUMBER", raising=False)
    with pytest.raises(RuntimeError, match="TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER"):
        TwilioSettings.from_environment()


def test_an_api_key_sid_in_place_of_the_account_sid_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "SK" + "0" * 32)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("TWILIO_PHONE_NUMBER", DANA_PHONE)
    with pytest.raises(RuntimeError, match="starts with AC"):
        TwilioSettings.from_environment()


def test_delivery_reports_need_a_public_https_address() -> None:
    assert status_callback_url("http://127.0.0.1:8000") is None
    assert status_callback_url(PUBLIC_URL + "/") == PUBLIC_URL + STATUS_PATH


# ---- TwilioSender, through Twilio's real client ----


class FakeTwilioHttp:
    """Stands in for Twilio's HTTP client: records each request, returns a canned response."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, uri: str, **options: Any) -> TwilioResponse:
        self.requests.append({"method": method, "uri": uri, **options})
        return TwilioResponse(self.status, json.dumps(self.body))


def created_message(sid: str) -> dict[str, Any]:
    # Trimmed from the Message resource example in Twilio's API reference.
    return {"sid": sid, "status": "queued", "account_sid": ACCOUNT_SID, "error_code": None}


def twilio_error(code: int, message: str) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "more_info": f"https://www.twilio.com/docs/errors/{code}",
        "status": 400,
    }


def test_the_real_http_client_never_retries_a_send() -> None:
    # A retried POST could deliver the same text twice. The default client must not retry and
    # must give up in seconds, leaving the text 'unknown' for the owner to check.
    sender = TwilioSender(twilio_settings(), None)
    http_client = sender._client.http_client
    assert http_client.timeout == SEND_TIMEOUT_SECONDS
    assert http_client.session.get_adapter("https://api.twilio.com").max_retries.total == 0


def test_a_send_posts_to_the_messages_api_and_returns_the_sid() -> None:
    http = FakeTwilioHttp(201, created_message("SM" + "a" * 32))
    sender = TwilioSender(twilio_settings(), PUBLIC_URL + STATUS_PATH, http_client=http)

    assert sender.send(DANA_PHONE, "Hi Dana") == "SM" + "a" * 32

    (request,) = http.requests
    assert request["method"] == "POST"
    assert request["uri"] == f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Messages.json"
    assert request["data"] == {
        "To": DANA_PHONE,
        "From": twilio_settings().phone_number,
        "Body": "Hi Dana",
        "StatusCallback": PUBLIC_URL + STATUS_PATH,
    }
    assert request["auth"] == (ACCOUNT_SID, AUTH_TOKEN)


def test_without_a_public_address_no_status_callback_is_asked_for() -> None:
    http = FakeTwilioHttp(201, created_message("SM" + "a" * 32))
    TwilioSender(twilio_settings(), None, http_client=http).send(DANA_PHONE, "Hi")
    assert "StatusCallback" not in http.requests[0]["data"]


def test_a_recipient_who_texted_stop_to_twilio_is_a_refusal_that_says_so() -> None:
    http = FakeTwilioHttp(400, twilio_error(21610, "Attempt to send to unsubscribed recipient"))
    with pytest.raises(SendRefused) as raised:
        TwilioSender(twilio_settings(), None, http_client=http).send(DANA_PHONE, "Hi")
    assert raised.value.recipient_opted_out


def test_another_4xx_is_a_plain_refusal() -> None:
    http = FakeTwilioHttp(400, twilio_error(21211, "Invalid 'To' Phone Number"))
    with pytest.raises(SendRefused) as raised:
        TwilioSender(twilio_settings(), None, http_client=http).send(DANA_PHONE, "Hi")
    assert not raised.value.recipient_opted_out


def test_rejected_credentials_stop_the_sender() -> None:
    http = FakeTwilioHttp(401, {**twilio_error(20003, "Authenticate"), "status": 401})
    with pytest.raises(SenderCredentialsRejected):
        TwilioSender(twilio_settings(), None, http_client=http).send(DANA_PHONE, "Hi")


def test_a_twilio_server_error_is_neither_refused_nor_assumed_sent() -> None:
    # A 5xx may come after Twilio created the message. It must reach the worker as an unknown
    # failure, which is never retried, rather than a refusal it might resend after.
    http = FakeTwilioHttp(503, {**twilio_error(20500, "Internal Server Error"), "status": 503})
    with pytest.raises(Exception) as raised:
        TwilioSender(twilio_settings(), None, http_client=http).send(DANA_PHONE, "Hi")
    assert not isinstance(raised.value, (SendRefused, SenderCredentialsRejected))


# ---- the worker with Twilio's failures ----


class OptedOutAtTwilio:
    """A sender for a number that texted STOP to Twilio while our webhook was down."""

    def send(self, phone: str, body: str) -> str:
        raise SendRefused("Twilio error 21610", recipient_opted_out=True)


class BadCredentials:
    def send(self, phone: str, body: str) -> str:
        raise SenderCredentialsRejected("Twilio error 20003")


def worker_with(store: Store, sender: Sender) -> Worker:
    return Worker(store, FAKES_SETTINGS, make_services(ScriptedJev()), sender, clock=Clock())


def test_a_stop_only_twilio_saw_puts_the_number_on_our_list(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, SETTINGS.texting_hours)
    worker_with(store, OptedOutAtTwilio()).run_once()
    assert store.is_on_do_not_call(DANA_PHONE)
    assert store.counts()["texts_out"] == {"failed": 1}


def test_rejected_credentials_put_the_text_back_and_stop_the_worker(store: Store) -> None:
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, SETTINGS.texting_hours)
    with pytest.raises(SenderCredentialsRejected):
        worker_with(store, BadCredentials()).run_once()
    assert store.counts()["texts_out"] == {"queued": 1}
    assert alerts(store) == []
    # Once .env is fixed and the worker restarts, the same text goes out.
    clock = Clock()
    worker, sender = make_worker(store, ScriptedJev(), clock)
    worker.run_once()
    assert len(sender.sent) == 1
