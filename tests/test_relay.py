# lead-bot/tests/test_relay.py
# The voice webhook and the live-call WebSocket, driven through the real FastAPI app with
# Twilio's request and message shapes. No request leaves this machine.
from __future__ import annotations

import json
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable
from typing import Any

from conftest import MONDAY_10AM
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from test_booking import FakeCalendar
from test_twilio import ACCOUNT_SID, SETTINGS, post_signed, twilio_settings
from text_fakes import ScriptedJev, make_services

from lead_bot.call_script import greeting
from lead_bot.conversation import ConversationServices
from lead_bot.server import create_app
from lead_bot.settings import RELAY_PATH
from lead_bot.store import Store
from lead_bot.twilio_sms import VOICE_PATH

CALLER = "+1" + "480" + "555" + "0166"
CALL_SID = "CA" + "2" * 32


def services_for(jev: ScriptedJev) -> Callable[[], ConversationServices]:
    services = make_services(jev, FakeCalendar())
    return lambda: services


def app_client(store: Store, call_services: Callable[[], ConversationServices] | None) -> TestClient:
    return TestClient(
        create_app(
            store, SETTINGS, clock=lambda: MONDAY_10AM, twilio=twilio_settings(), call_services=call_services
        )
    )


def voice_form(call_sid: str = CALL_SID, caller: str = CALLER) -> dict[str, str]:
    """The fields Twilio posts when a call comes in (the voice webhook request)."""
    return {
        "CallSid": call_sid,
        "AccountSid": ACCOUNT_SID,
        "From": caller,
        "To": twilio_settings().phone_number,
        "CallStatus": "ringing",
        "Direction": "inbound",
    }


def ring(test_client: TestClient) -> tuple[int, str, ElementTree.Element]:
    """Post the signed voice webhook; returns the call id, token and parsed TwiML."""
    response = post_signed(test_client, VOICE_PATH, voice_form())
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/xml")
    twiml = ElementTree.fromstring(response.text)
    parameters = {parameter.get("name"): parameter.get("value") for parameter in twiml.iter("Parameter")}
    return int(str(parameters["call_id"])), str(parameters["relay_token"]), twiml


def setup_message(call_id: int | str, relay_token: str, call_sid: str = CALL_SID) -> dict[str, Any]:
    # The shape Twilio sends first. Our two <Parameter>s arrive in customParameters.
    return {
        "type": "setup",
        "callSid": call_sid,
        "from": CALLER,
        "to": twilio_settings().phone_number,
        "direction": "inbound",
        "customParameters": {"call_id": str(call_id), "relay_token": relay_token},
    }


def prompt(words: str) -> dict[str, Any]:
    return {"type": "prompt", "voicePrompt": words, "lang": "en-US", "last": True}


def receive_until_end(socket: WebSocketTestSession) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    while True:
        message = socket.receive_json()
        messages.append(message)
        if message["type"] == "end":
            return messages


def expect_refusal(socket: WebSocketTestSession, setup: dict[str, Any]) -> dict[str, Any]:
    """Send setup, then words that end any accepted call at once (a keyword opt-out, no Jev).

    A refused call ends with "refused". A call wrongly let through ends as "opted_out" instead,
    so the test fails rather than waiting forever for an end that never comes.
    """
    socket.send_json(setup)
    socket.send_json(prompt("Please take me off your list"))
    return receive_until_end(socket)[-1]


# ---- the voice webhook ----


def test_a_signed_call_gets_twiml_that_connects_it_to_the_relay(store: Store) -> None:
    call_id, token, twiml = ring(app_client(store, services_for(ScriptedJev())))
    relay = next(twiml.iter("ConversationRelay"))
    assert relay.get("url") == "wss://lead-bot.example.ts.net" + RELAY_PATH
    assert relay.get("welcomeGreeting") == greeting(SETTINGS)
    assert token and call_id == 1


def test_an_unsigned_call_is_refused(store: Store) -> None:
    test_client = app_client(store, services_for(ScriptedJev()))
    response = test_client.post(VOICE_PATH, data=voice_form())
    assert response.status_code == 403


def test_a_server_not_set_up_for_calls_answers_503(store: Store) -> None:
    response = post_signed(app_client(store, None), VOICE_PATH, voice_form())
    assert response.status_code == 503


def test_a_retried_webhook_is_the_same_call(store: Store) -> None:
    test_client = app_client(store, services_for(ScriptedJev()))
    first = ring(test_client)
    second = ring(test_client)
    assert (first[0], first[1]) == (second[0], second[1])


# ---- the live call ----


def test_a_caller_leaves_a_message_over_the_socket(store: Store) -> None:
    test_client = app_client(store, services_for(ScriptedJev("message", "gave_name")))
    call_id, token, _ = ring(test_client)
    with test_client.websocket_connect(RELAY_PATH) as socket:
        socket.send_json(setup_message(call_id, token))
        socket.send_json(prompt("I'm calling about my invoice"))
        asked = socket.receive_json()
        socket.send_json(prompt("Sam Lee"))
        goodbye, end = receive_until_end(socket)

    assert asked == {"type": "text", "token": "Sure, I'll pass that on. Can I get your name?", "last": True}
    assert goodbye["token"].startswith("Thanks, Sam.")
    assert json.loads(end["handoffData"]) == {"result": "message"}
    assert store.call_result(call_id) == "message"


def test_a_wrong_token_is_hung_up_on(store: Store) -> None:
    test_client = app_client(store, services_for(ScriptedJev()))
    call_id, _, _ = ring(test_client)
    with test_client.websocket_connect(RELAY_PATH) as socket:
        end = expect_refusal(socket, setup_message(call_id, "not-the-token"))
    assert json.loads(end["handoffData"]) == {"result": "refused"}


def test_a_token_from_another_twilio_call_is_hung_up_on(store: Store) -> None:
    test_client = app_client(store, services_for(ScriptedJev()))
    call_id, token, _ = ring(test_client)
    with test_client.websocket_connect(RELAY_PATH) as socket:
        end = expect_refusal(socket, setup_message(call_id, token, call_sid="CA" + "9" * 32))
    assert json.loads(end["handoffData"]) == {"result": "refused"}


def test_a_token_works_only_once(store: Store) -> None:
    test_client = app_client(store, services_for(ScriptedJev()))
    call_id, token, _ = ring(test_client)
    with test_client.websocket_connect(RELAY_PATH) as socket:
        socket.send_json(setup_message(call_id, token))
    with test_client.websocket_connect(RELAY_PATH) as socket:
        end = expect_refusal(socket, setup_message(call_id, token))
    assert json.loads(end["handoffData"]) == {"result": "refused"}


def test_missing_model_keys_hang_up_the_call_with_a_log_line(store: Store) -> None:
    def no_keys() -> ConversationServices:
        raise RuntimeError("TYPESAFE_API_KEY and OPENAI_API_KEY must be set in .env")

    test_client = app_client(store, no_keys)
    call_id, token, _ = ring(test_client)
    with test_client.websocket_connect(RELAY_PATH) as socket:
        end = expect_refusal(socket, setup_message(call_id, token))
    assert json.loads(end["handoffData"]) == {"result": "refused"}


def test_a_caller_who_hangs_up_is_recorded_and_the_owner_alerted(store: Store) -> None:
    test_client = app_client(store, services_for(ScriptedJev("roof")))
    call_id, token, _ = ring(test_client)
    with test_client.websocket_connect(RELAY_PATH) as socket:
        socket.send_json(setup_message(call_id, token))
        socket.send_json(prompt("my roof is leaking"))
        socket.receive_json()  # "Sure, I can help with that. Can I get your name?"
    # Leaving the block closes the socket, as Twilio does when the caller hangs up.
    assert store.call_result(call_id) == "hung_up"
    assert [alert.reason for alert in store.owner_alerts()] == ["caller_hung_up"]
