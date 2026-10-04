# lead-bot/tests/test_relay.py
# The live-call WebSocket, driven with Twilio's message shapes through the real FastAPI app.
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from call_fakes import GUARD_CHECK, SETTINGS, ScriptedJev, make_services
from conftest import DANA_PHONE, HOURS, MONDAY_10AM, make_consent, make_lead
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession
from test_booking import FakeCalendar

from lead_bot.call_script import CHECKING_LINE, call_twiml, opening_greeting
from lead_bot.relay import RELAY_PATH
from lead_bot.server import create_app
from lead_bot.store import Store
from lead_bot.writer import Turn


@pytest.fixture
def placed_call(store: Store) -> tuple[int, str]:
    """A consenting lead whose first call the dialer has just placed. Returns (call_id, token)."""
    store.add_lead(make_lead(), make_consent(), MONDAY_10AM, HOURS)
    due_call = store.claim_due_call(MONDAY_10AM)
    assert due_call is not None
    store.mark_call_placed(due_call.call_id, "CA-test", MONDAY_10AM)
    return due_call.call_id, due_call.relay_token


def client_for(store: Store, jev: ScriptedJev) -> TestClient:
    app = create_app(
        store, SETTINGS, clock=lambda: MONDAY_10AM, call_services=make_services(jev, FakeCalendar())
    )
    return TestClient(app)


def setup_message(call_id: int | str, relay_token: str) -> dict[str, Any]:
    # The shape Twilio sends first. Our two <Parameter>s arrive in customParameters.
    return {
        "type": "setup",
        "callSid": "CA-test",
        "from": "+14805550100",
        "to": DANA_PHONE,
        "direction": "outbound-api",
        "customParameters": {"call_id": str(call_id), "relay_token": relay_token},
    }


def receive_until_end(socket: WebSocketTestSession) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    while True:
        message = socket.receive_json()
        messages.append(message)
        if message["type"] == "end":
            return messages


def test_pressing_9_over_the_socket_opts_out_and_hangs_up(store: Store, placed_call: tuple[int, str]) -> None:
    call_id, token = placed_call
    with client_for(store, ScriptedJev()).websocket_connect(RELAY_PATH) as socket:
        socket.send_json(setup_message(call_id, token))
        socket.send_json({"type": "dtmf", "digit": "9"})
        [end] = receive_until_end(socket)

    assert json.loads(end["handoffData"]) == {"result": "opted_out"}
    assert store.is_on_do_not_call(DANA_PHONE)


def test_speech_gets_a_spoken_reply(store: Store, placed_call: tuple[int, str]) -> None:
    call_id, token = placed_call
    with client_for(store, ScriptedJev("yes")).websocket_connect(RELAY_PATH) as socket:
        socket.send_json(setup_message(call_id, token))
        socket.send_json({"type": "prompt", "voicePrompt": "Sure, go ahead", "lang": "en-US", "last": True})
        reply = socket.receive_json()
        socket.send_json({"type": "dtmf", "digit": "9"})  # end the call so the test can finish
        receive_until_end(socket)

    expected = "Great. Is the inspection for 12 Saguaro Ln, Mesa AZ?"
    assert reply == {"type": "text", "token": expected, "last": True}


def test_a_wrong_token_is_refused_before_anything_is_said(store: Store, placed_call: tuple[int, str]) -> None:
    call_id, _ = placed_call
    with client_for(store, ScriptedJev()).websocket_connect(RELAY_PATH) as socket:
        socket.send_json(setup_message(call_id, "guessed-token"))
        # If the call were wrongly accepted, this 9 ends it as an opt-out, so the test fails on
        # the assertion below instead of waiting forever for an "end".
        socket.send_json({"type": "dtmf", "digit": "9"})
        [end] = receive_until_end(socket)

    assert json.loads(end["handoffData"]) == {"result": "refused"}
    assert store.call_transcript(call_id) == []  # nothing about the lead was used


def test_the_same_call_cannot_connect_twice(store: Store, placed_call: tuple[int, str]) -> None:
    call_id, token = placed_call
    client = client_for(store, ScriptedJev())
    with client.websocket_connect(RELAY_PATH) as socket:
        socket.send_json(setup_message(call_id, token))
        socket.send_json({"type": "dtmf", "digit": "9"})
        receive_until_end(socket)
    with client.websocket_connect(RELAY_PATH) as second_socket:
        second_socket.send_json(setup_message(call_id, token))
        second_socket.send_json({"type": "dtmf", "digit": "9"})  # see the wrong-token test
        [end] = receive_until_end(second_socket)

    assert json.loads(end["handoffData"]) == {"result": "refused"}


def wait_for(condition: Callable[[], bool], seconds: float = 3.0) -> bool:
    """Poll until condition() is true. The server handles the socket on another thread."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


def test_a_hang_up_is_recorded_and_the_owner_told(store: Store, placed_call: tuple[int, str]) -> None:
    call_id, token = placed_call
    with client_for(store, ScriptedJev()).websocket_connect(RELAY_PATH) as socket:
        socket.send_json(setup_message(call_id, token))
        assert wait_for(lambda: bool(store.call_transcript(call_id)))  # the call has started
        socket.close()  # what a hang-up looks like to the server: a disconnect
        assert wait_for(lambda: store.call_result(call_id) is not None)

    assert store.call_result(call_id) == "hung_up"
    assert [alert.reason for alert in store.owner_alerts()] == ["hung_up_before_booking"]


def test_a_call_cut_off_by_the_server_stopping_is_still_recorded(
    store: Store, placed_call: tuple[int, str]
) -> None:
    # Leaving the block without closing makes the test client cancel the server's task, the
    # way stopping the server mid-call would. The hang-up record must still be written.
    call_id, token = placed_call
    with client_for(store, ScriptedJev()).websocket_connect(RELAY_PATH) as socket:
        socket.send_json(setup_message(call_id, token))
        assert wait_for(lambda: bool(store.call_transcript(call_id)))

    assert wait_for(lambda: store.call_result(call_id) is not None)
    assert store.call_result(call_id) == "hung_up"


def test_the_twiml_speaks_the_opening_and_carries_only_ids(tmp_path: Path) -> None:
    twiml = call_twiml(SETTINGS, call_id=7, relay_token="tok-123")

    assert 'url="ws://127.0.0.1:8000/calls/relay"' in twiml
    assert 'welcomeGreetingInterruptible="dtmf"' in twiml
    assert 'dtmfDetection="true"' in twiml
    assert 'reportInputDuringAgentSpeech="any"' in twiml
    assert '<Parameter name="call_id" value="7"/>' in twiml
    assert '<Parameter name="relay_token" value="tok-123"/>' in twiml
    assert "Desert Ridge Roofing LLC" in twiml
    assert DANA_PHONE not in twiml  # the lead's details never travel in the TwiML


def test_the_opening_names_the_business_first_then_how_to_opt_out() -> None:
    sentences = opening_greeting(SETTINGS).split(". ")
    assert sentences[0] == "Hello, this call is from Desert Ridge Roofing LLC"
    assert sentences[1] == "To stop these calls, press 9 or say stop at any time"
    assert "4 8 0, 5 5 5, 0 1 9 9" in opening_greeting(SETTINGS)  # the callback number, digit by digit


def test_a_business_name_with_an_ampersand_keeps_the_twiml_valid() -> None:
    import xml.etree.ElementTree as ElementTree
    from dataclasses import replace

    smith = replace(SETTINGS, business_name='Smith & Sons "Roofing"')
    root = ElementTree.fromstring(call_twiml(smith, call_id=1, relay_token="t"))
    relay = root.find("./Connect/ConversationRelay")
    assert relay is not None
    assert relay.attrib["welcomeGreeting"].startswith('Hello, this call is from Smith & Sons "Roofing".')


class SlowWriter:
    """A writer that waits until released, like a model call taking seconds."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.finished = threading.Event()

    def write(self, *, facts: str, history: Sequence[Turn], reason: str) -> str:
        self.started.set()
        try:
            # Gives up after 5 seconds, so a broken relay fails the test instead of hanging it.
            self.release.wait(timeout=5)
            return "It takes about an hour."
        finally:
            self.finished.set()


def test_let_me_check_is_sent_before_the_writer_finishes(store: Store, placed_call: tuple[int, str]) -> None:
    call_id, token = placed_call
    writer = SlowWriter()
    services = make_services(ScriptedJev("other_question", GUARD_CHECK), FakeCalendar(), writer=writer)
    app = create_app(store, SETTINGS, clock=lambda: MONDAY_10AM, call_services=services)
    with TestClient(app).websocket_connect(RELAY_PATH) as socket:
        socket.send_json(setup_message(call_id, token))
        socket.send_json({"type": "prompt", "voicePrompt": "How long does it take?", "last": True})
        filler = socket.receive_json()  # arrives while the writer is still blocked
        assert writer.started.wait(timeout=5)
        writer.release.set()
        answer = socket.receive_json()
        socket.send_json({"type": "dtmf", "digit": "9"})
        receive_until_end(socket)

    assert filler == {"type": "text", "token": CHECKING_LINE, "last": True}
    assert answer["token"].startswith("It takes about an hour. Do you have a couple of minutes")


def test_a_9_while_the_writer_works_hangs_up_at_once(store: Store, placed_call: tuple[int, str]) -> None:
    call_id, token = placed_call
    writer = SlowWriter()
    # No GUARD_CHECK scripted: the call ends before the draft could be checked.
    services = make_services(ScriptedJev("other_question"), FakeCalendar(), writer=writer)
    app = create_app(store, SETTINGS, clock=lambda: MONDAY_10AM, call_services=services)
    with TestClient(app).websocket_connect(RELAY_PATH) as socket:
        socket.send_json(setup_message(call_id, token))
        socket.send_json({"type": "prompt", "voicePrompt": "How long does it take?", "last": True})
        assert socket.receive_json()["token"] == CHECKING_LINE
        assert writer.started.wait(timeout=5)
        socket.send_json({"type": "dtmf", "digit": "9"})
        # The end arrives while the writer is still blocked: the 9 didn't wait behind it.
        [end] = receive_until_end(socket)
        writer_was_still_working = not writer.finished.is_set()
        writer.release.set()

    assert writer_was_still_working
    assert json.loads(end["handoffData"]) == {"result": "opted_out"}
    assert store.is_on_do_not_call(DANA_PHONE)
