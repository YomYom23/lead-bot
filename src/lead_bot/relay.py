# lead-bot/src/lead_bot/relay.py
"""The live-call WebSocket: Twilio ConversationRelay (or the simulator) talks to us here.

Twilio does the speech: it turns the caller's voice into text and our text into a voice. This
endpoint only ever sees JSON messages (Twilio's "Getting and sending WebSocket messages" page):
  in:  setup (once, first), prompt (something said), dtmf (a key pressed), interrupt, error
  out: text (something for the bot to say), end (hang up)

A call reaches us in two steps. Twilio first posts to the voice webhook (server.py), which
records the call with a one-time token and answers with TwiML pointing here. Twilio then opens
this WebSocket and sends the call id and token back in its setup message; store.connect_call
lets the call start only if they match a ringing call, once.

Async, and why the to_thread calls matter: one server process runs every live call on one event
loop. Jev, Google Calendar and SQLite are all blocking calls. Called directly inside `async def`,
each one would freeze every other call on the server until it returned. (Unlike Node, Python
doesn't make I/O non-blocking for you.) asyncio.to_thread runs them on a worker thread instead.

Two tasks per call: the reader receives messages (and notices a hang-up at once), and the talker
handles speech, one prompt at a time, in the order it arrived.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .call_session import Action, CallSession, ContinueTurn, EndCall, Say
from .conversation import ConversationServices
from .settings import RELAY_PATH, BusinessSettings
from .store import Store

logger = logging.getLogger(__name__)

# Twilio sends setup immediately after connecting. Anything slower isn't a real call.
SETUP_TIMEOUT_SECONDS = 10.0


def add_relay_route(
    app: FastAPI,
    store: Store,
    settings: BusinessSettings,
    services: Callable[[], ConversationServices],
    clock: Callable[[], datetime],
) -> None:
    """`services` builds Jev, the calendar and the writer when the first call needs them, so the
    server starts without model keys and only calls fail (with a log line) when they're missing."""

    @app.websocket(RELAY_PATH)
    async def relay(websocket: WebSocket) -> None:
        await websocket.accept()
        session = await _start_session(websocket, store, settings, services, clock)
        if session is None:
            return
        await _run_call(websocket, session)


async def _start_session(
    websocket: WebSocket,
    store: Store,
    settings: BusinessSettings,
    services: Callable[[], ConversationServices],
    clock: Callable[[], datetime],
) -> CallSession | None:
    """Read the setup message and let the call start only if it's one the webhook recorded."""
    try:
        setup = await asyncio.wait_for(websocket.receive_json(), SETUP_TIMEOUT_SECONDS)
    except (TimeoutError, WebSocketDisconnect, ValueError):
        await _close(websocket)
        return None
    parameters = setup.get("customParameters") if isinstance(setup, dict) else None
    if not isinstance(setup, dict) or setup.get("type") != "setup" or not isinstance(parameters, dict):
        logger.warning("relay refused: the first message wasn't a setup message")
        await _hang_up(websocket, "refused")
        return None
    try:
        call_id = int(str(parameters.get("call_id", "")))
    except ValueError:
        await _hang_up(websocket, "refused")
        return None
    relay_token = str(parameters.get("relay_token", ""))
    call_sid = str(setup.get("callSid", "")) or None

    try:
        call_services = await asyncio.to_thread(services)
    except Exception:
        # Most likely TYPESAFE_API_KEY or OPENAI_API_KEY missing. The caller hears only the
        # greeting Twilio already spoke; the log says what to fix.
        logger.exception("call %s refused: the call services couldn't be built", call_id)
        await _hang_up(websocket, "refused")
        return None
    connected = await asyncio.to_thread(store.connect_call, call_id, relay_token, clock(), call_sid)
    if connected is None:
        # Wrong token, not a call the webhook recorded, or already connected once.
        logger.warning("relay refused call %s", call_id)
        await _hang_up(websocket, "refused")
        return None
    logger.info("call %d connected", connected.call_id)
    # The constructor writes the greeting to the transcript, a blocking SQLite write.
    return await asyncio.to_thread(CallSession, connected, settings, store, call_services, clock)


async def _run_call(websocket: WebSocket, session: CallSession) -> None:
    prompts: asyncio.Queue[str] = asyncio.Queue()
    send_lock = asyncio.Lock()  # one message at a time on the socket
    call_over = asyncio.Event()

    async def perform(actions: list[Action]) -> bool:
        """Send the actions in order. Returns True when the turn continues (see ContinueTurn).

        The send lock is held only while sending. The rest of a continued turn (the writer and
        guard, seconds of work) runs outside it.
        """
        async with send_lock:
            if call_over.is_set():
                return False
            for action in actions:
                if isinstance(action, Say):
                    await websocket.send_json({"type": "text", "token": action.text, "last": True})
                elif isinstance(action, EndCall):
                    call_over.set()
                    # Untested against real Twilio: whether a goodbye sent just before "end" is
                    # always heard in full. Check on the first real call.
                    handoff_data = json.dumps({"result": action.reason})
                    await websocket.send_json({"type": "end", "handoffData": handoff_data})
                    return False
                elif isinstance(action, ContinueTurn):
                    return True
        return False

    async def talker() -> None:
        while not call_over.is_set():
            words = await prompts.get()
            actions = await asyncio.to_thread(session.on_prompt, words)
            while await perform(actions):
                actions = await asyncio.to_thread(session.continue_turn)

    async def reader() -> None:
        while not call_over.is_set():
            message: Any = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            message_type = message.get("type")
            if message_type == "prompt":
                # With partial prompts off (the default), every prompt is a complete utterance;
                # last=false would only be a fragment, so it's skipped.
                if message.get("last", True) and str(message.get("voicePrompt", "")).strip():
                    prompts.put_nowait(str(message["voicePrompt"]))
            elif message_type == "error":
                logger.warning("call %s: Twilio reported: %s", session, message.get("description"))
            # "interrupt" (the caller talked over the bot) needs nothing: their words arrive as a
            # prompt anyway. Keypresses mean nothing on these calls.

    reader_task = asyncio.create_task(reader())
    talker_task = asyncio.create_task(talker())
    ended_task = asyncio.create_task(call_over.wait())
    try:
        # Whichever comes first: the call is ended by us, the line drops (the reader raises
        # WebSocketDisconnect), or something fails.
        done, _ = await asyncio.wait(
            {reader_task, talker_task, ended_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            error = task.exception()
            if error is not None and not isinstance(error, WebSocketDisconnect):
                logger.error("call failed", exc_info=error)
    finally:
        for task in (reader_task, talker_task, ended_task):
            task.cancel()
        # Records "hung up" if the call didn't end any other way. Does nothing if it did.
        # Shielded for one case: if this task is cancelled (the server stopping mid-call) while
        # every worker thread is busy, the write is still queued, and cancelling would remove
        # it from the queue. A write that has already started finishes either way.
        await asyncio.shield(asyncio.to_thread(session.on_hangup))
        await _close(websocket)


async def _hang_up(websocket: WebSocket, reason: str) -> None:
    try:
        await websocket.send_json({"type": "end", "handoffData": json.dumps({"result": reason})})
    except (WebSocketDisconnect, RuntimeError):
        pass
    await _close(websocket)


async def _close(websocket: WebSocket) -> None:
    try:
        await websocket.close()
    except (RuntimeError, WebSocketDisconnect):
        pass  # already closed, or the other end hung up first
