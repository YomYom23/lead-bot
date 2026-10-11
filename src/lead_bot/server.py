# lead-bot/src/lead_bot/server.py
"""The web server: the roofing company's inspection-request form, Twilio's webhooks, the live-call
WebSocket, and a health check.

  GET  /                      -> the form
  POST /leads                 -> validate, record the lead and its consent evidence, schedule the first text
  POST /twilio/incoming       -> a homeowner's text, from Twilio: check the signature, save it
  POST /twilio/status         -> Twilio's delivery report for one of our texts: check, save
  POST /twilio/voice/incoming -> a forwarded call nobody picked up: check, record it, answer with
                                 TwiML that connects it to the relay
  WS   /calls/relay           -> the live call itself (relay.py)
  GET  /health                -> counts of leads and texts by status; 503 if the worker stopped

Nothing here sends a text. The text handlers record and return at once, because Twilio retries a
webhook that's slow to answer. Sending and answering texts happen in the worker process
(worker.py), which re-checks consent, the do-not-call list and texting hours right before it
queues each text the bot starts. Calls are live, so the relay answers them in this process.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from datetime import datetime
from string import Template
from typing import Annotated

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData

from .call_script import call_twiml
from .consent import CONSENT_VERSION, ConsentEvidence, consent_text
from .conversation import ConversationServices
from .leads import PROBLEM_LABELS, InvalidLeadForm, parse_lead_form, pretty_phone
from .relay import add_relay_route
from .settings import BusinessSettings
from .store import AddedLead, Store
from .timing import clock_label, utc_now
from .twilio_sms import INCOMING_PATH, STATUS_PATH, VOICE_PATH, TwilioSettings, request_is_from_twilio

logger = logging.getLogger(__name__)

# An empty TwiML reply: "received, send nothing back". The worker sends the answer later.
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'

# /health fails when the worker hasn't written its heartbeat for this long. The worker beats
# every couple of seconds even when idle, so two minutes means it is stopped or stuck.
WORKER_HEARTBEAT_LIMIT_SECONDS = 120

# string.Template rather than str.format: the CSS is full of { } braces, which str.format would
# try to fill in. Template only substitutes $names. Every value put into a page goes through
# html.escape first, so a name like <script> shows as text instead of running.
PAGE_TEMPLATE = Template("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title - $business_name</title>
<style>
  body { font-family: system-ui, sans-serif; line-height: 1.5; max-width: 34rem;
         margin: 2rem auto; padding: 0 1rem; color: #1f2328; }
  label { display: block; margin-top: 1rem; font-weight: 600; }
  input[type=text], input[type=tel], select { width: 100%; padding: 0.5rem; font-size: 1rem;
         box-sizing: border-box; }
  .consent { display: flex; gap: 0.6rem; align-items: flex-start; margin-top: 1.5rem;
             font-weight: normal; font-size: 0.9rem; }
  .consent input { margin-top: 0.3rem; }
  .error { background: #ffebe9; border: 1px solid #ff8182; padding: 0.6rem; border-radius: 6px; }
  .trap { position: absolute; left: -10000px; }
  button { margin-top: 1.5rem; padding: 0.7rem 1.4rem; font-size: 1rem; }
</style>
</head>
<body>
$body
</body>
</html>""")

FORM_TEMPLATE = Template("""<h1>Request a free roof inspection</h1>
<p>Tell us about your roof and we'll set up a time for an inspector to come out.</p>
$error
<form method="post" action="/leads">
  <label for="full_name">Your name</label>
  <input type="text" id="full_name" name="full_name" value="$full_name" maxlength="100" required>
  <label for="phone">Mobile phone</label>
  <input type="tel" id="phone" name="phone" value="$phone" placeholder="(602) 555-0142" required>
  <label for="address">Property address</label>
  <input type="text" id="address" name="address" value="$address" maxlength="200" required>
  <label for="problem">What's going on?</label>
  <select id="problem" name="problem" required>
    <option value="">Choose one</option>
$problem_options
  </select>
  <!-- Bot trap: hidden from people, so only form-filling bots fill it in. -->
  <div class="trap" aria-hidden="true">
    <label for="website">Leave this empty</label>
    <input type="text" id="website" name="website" tabindex="-1" autocomplete="off">
  </div>
  <!-- Unticked by default and not required: consent must be a free choice, and the form works
       without it (the owner then contacts them personally instead of the bot). -->
  <label class="consent">
    <input type="checkbox" name="text_consent" value="yes"$consent_checked>
    <span>$consent_text</span>
  </label>
  <input type="hidden" name="consent_version" value="$consent_version">
  <button type="submit">Request inspection</button>
</form>""")


def create_app(
    store: Store,
    settings: BusinessSettings,
    clock: Callable[[], datetime] = utc_now,
    twilio: TwilioSettings | None = None,
    call_services: Callable[[], ConversationServices] | None = None,
) -> FastAPI:
    """Build the app. Everything is passed in so tests can control it.

    Without Twilio settings the webhooks answer 503, so a misconfigured server is obvious in
    Twilio's error log instead of silently dropping texts. Without `call_services` the bot
    doesn't answer calls at all: the voice webhook answers 503 and there's no relay.
    """
    app = FastAPI(title="lead-bot")
    if call_services is not None:
        add_relay_route(app, store, settings, call_services, clock)

    def page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
        content = PAGE_TEMPLATE.substitute(
            title=html.escape(title), business_name=html.escape(settings.business_name), body=body
        )
        return HTMLResponse(content=content, status_code=status_code)

    def form_page(
        error: str = "",
        full_name: str = "",
        phone: str = "",
        address: str = "",
        problem: str = "",
        consent_ticked: bool = False,
        status_code: int = 200,
    ) -> HTMLResponse:
        problem_options = "\n".join(
            f'    <option value="{value}"{" selected" if value == problem else ""}>'
            f"{html.escape(label)}</option>"
            for value, label in PROBLEM_LABELS.items()
        )
        body = FORM_TEMPLATE.substitute(
            error=f'<p class="error">{html.escape(error)}</p>' if error else "",
            full_name=html.escape(full_name),
            phone=html.escape(phone),
            address=html.escape(address),
            problem_options=problem_options,
            consent_checked=" checked" if consent_ticked else "",
            consent_text=html.escape(consent_text(settings.business_name)),
            consent_version=CONSENT_VERSION,
        )
        return page("Request a roof inspection", body, status_code)

    @app.get("/")
    def show_form() -> HTMLResponse:
        return form_page()

    @app.post("/leads")
    def submit_lead(
        request: Request,
        # Optional with "" defaults so a missing field is answered with the form and a message,
        # not with FastAPI's JSON validation error, which a homeowner couldn't make sense of.
        full_name: Annotated[str, Form()] = "",
        phone: Annotated[str, Form()] = "",
        address: Annotated[str, Form()] = "",
        problem: Annotated[str, Form()] = "",
        # A browser sends an unticked checkbox as nothing at all, hence None.
        text_consent: Annotated[str | None, Form()] = None,
        consent_version: Annotated[str, Form()] = "",
        website: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        if website:
            # The bot trap was filled in. Show the normal thank-you page so the bot learns
            # nothing, but store nothing and text no one.
            logger.info("form submission dropped: bot trap filled")
            return page("Thank you", _thanks_body(None, settings, phone_shown="", now=clock()))

        consent_ticked = text_consent == "yes"
        try:
            new_lead = parse_lead_form(full_name, phone, address, problem)
        except InvalidLeadForm as error:
            return form_page(str(error), full_name, phone, address, problem, consent_ticked, status_code=400)

        # The tick only counts if it was given against the wording this server shows now. A
        # page opened before the wording changed showed different text, and the record must
        # match what the person actually saw.
        consent_is_current = consent_version == CONSENT_VERSION
        if consent_ticked and not consent_is_current:
            logger.warning("consent ticked on an outdated form (%r); treating as not given", consent_version)
        evidence = ConsentEvidence(
            given=consent_ticked and consent_is_current,
            version=CONSENT_VERSION,
            text_shown=consent_text(settings.business_name),
            ip_address=request.client.host if request.client else "unknown",
            forwarded_for=request.headers.get("x-forwarded-for"),
            user_agent=request.headers.get("user-agent", "")[:500],
            # The public address, not request.url: behind a proxy request.url is the internal
            # one (http://lead-bot-<client>-server:8000/leads), which proves nothing about the
            # page the homeowner saw. Built the same way as the Twilio signed URL below.
            source=settings.public_base_url.rstrip("/") + request.url.path,
        )
        submitted_at = clock()
        added = store.add_lead(new_lead, evidence, submitted_at, settings.texting_hours)
        logger.info("lead %d added: %s", added.lead_id, added.decision)
        return page(
            "Thank you",
            _thanks_body(added, settings, phone_shown=pretty_phone(new_lead.phone), now=submitted_at),
        )

    @app.get("/health")
    def health() -> Response:
        # The server can read the database whether or not the worker is running, so counts
        # alone would say "ok" for a dead worker. The worker's heartbeat is what proves it lives.
        last_beat = store.last_worker_heartbeat()
        if last_beat is None:
            return _health_response(503, {"ok": False, "problem": "worker has not reported yet"})
        seconds_old = int((clock() - last_beat).total_seconds())
        if seconds_old > WORKER_HEARTBEAT_LIMIT_SECONDS:
            return _health_response(
                503, {"ok": False, "problem": f"worker heartbeat is {seconds_old} seconds old"}
            )
        return _health_response(200, {"ok": True, **store.counts()})

    async def checked_twilio_form(request: Request, path: str) -> FormData | Response:
        """The posted form if the request really came from Twilio, otherwise the error to return."""
        if twilio is None:
            logger.error("Twilio webhook called but TWILIO_* isn't set in .env")
            return Response("Twilio isn't configured on this server", status_code=503)
        form = await request.form()
        # Twilio signs the URL it called. Behind Funnel or a proxy this server sees a different
        # one (http://127.0.0.1:8000/...), so the public address comes from settings.
        signed_url = settings.public_base_url.rstrip("/") + path
        if request.url.query:
            signed_url += "?" + request.url.query
        signature = request.headers.get("x-twilio-signature")
        if not request_is_from_twilio(twilio.auth_token, signed_url, form, signature):
            logger.warning("Twilio webhook %s refused: bad signature (signed URL %s)", path, signed_url)
            return Response("bad signature", status_code=403)
        if form.get("AccountSid") != twilio.account_sid:
            # Signed with our token but for another account: a copied webhook URL. Ignore it.
            logger.warning("Twilio webhook %s from another account ignored", path)
            return twiml_response()
        return form

    @app.post(INCOMING_PATH)
    async def incoming_text(request: Request) -> Response:
        checked = await checked_twilio_form(request, INCOMING_PATH)
        if isinstance(checked, Response):
            return checked
        message_id = str(checked.get("MessageSid") or "")
        phone = str(checked.get("From") or "")
        body = str(checked.get("Body") or "")
        if not message_id or not phone:
            # Nothing to save and nothing Twilio can fix by retrying, so 200.
            logger.warning("incoming text without MessageSid or From ignored")
            return twiml_response()
        # The sqlite write runs off the event loop, so a slow disk can't hold up other requests.
        saved = await run_in_threadpool(store.record_incoming_text, phone, body, message_id, clock())
        logger.info("incoming text %s %s", message_id, "saved" if saved is not None else "already saved")
        return twiml_response()

    @app.post(STATUS_PATH)
    async def delivery_report(request: Request) -> Response:
        checked = await checked_twilio_form(request, STATUS_PATH)
        if isinstance(checked, Response):
            return checked
        message_id = str(checked.get("MessageSid") or "")
        status = str(checked.get("MessageStatus") or "")
        error_code = str(checked.get("ErrorCode") or "") or None
        if message_id and status:
            await run_in_threadpool(store.record_delivery_report, message_id, status, error_code, clock())
            if error_code is not None:
                logger.warning("text %s: %s, Twilio error %s", message_id, status, error_code)
        return twiml_response()

    @app.post(VOICE_PATH)
    async def incoming_call(request: Request) -> Response:
        if call_services is None:
            logger.error("a call came in, but this server isn't set up to answer calls")
            return Response("calls aren't answered on this server", status_code=503)
        checked = await checked_twilio_form(request, VOICE_PATH)
        if isinstance(checked, Response):
            return checked
        call_sid = str(checked.get("CallSid") or "")
        # For a forwarded call, From is normally the person who rang the business (carriers
        # differ; check it on the first real call). A hidden number arrives as 'anonymous'.
        caller = str(checked.get("From") or "") or "anonymous"
        if not call_sid:
            logger.warning("incoming call without CallSid ignored")
            return Response(EMPTY_TWIML, media_type="text/xml")
        incoming = await run_in_threadpool(store.start_incoming_call, call_sid, caller, clock())
        logger.info("call %d ringing (%s)", incoming.call_id, call_sid)
        return Response(call_twiml(settings, incoming.call_id, incoming.relay_token), media_type="text/xml")

    @app.get("/favicon.ico")
    def no_favicon() -> Response:
        # Browsers ask for this on every page. A plain 204 keeps it out of the error log.
        return Response(status_code=204)

    return app


def twiml_response() -> Response:
    return Response(EMPTY_TWIML, media_type="text/xml")


def _health_response(status_code: int, body: dict[str, object]) -> JSONResponse:
    return JSONResponse(body, status_code=status_code)


def _thanks_body(added: AddedLead | None, settings: BusinessSettings, phone_shown: str, now: datetime) -> str:
    """The thank-you page. It only promises the text that will really be sent.

    A lead the bot won't text (no consent, or a number that opted out before) gets the same
    neutral wording as a dropped bot submission. The page never says why, so it can't reveal
    who is on the do-not-call list.
    """
    business = html.escape(settings.business_name)
    if added is None or added.first_text_due is None:
        if added is not None and added.decision == "already_in_follow_up":
            return (
                "<h1>Thank you!</h1><p>We already have your request and will be in touch at "
                f"{html.escape(phone_shown)}. There's no need to send it again.</p>"
            )
        return f"<h1>Thank you!</h1><p>Someone from {business} will be in touch soon.</p>"
    # Inside texting hours the first text is due the moment the form arrives. Outside them it's
    # due at the next opening, and the page says when.
    if added.first_text_due <= now:
        when = "in the next minute or so"
    else:
        due_day = added.first_text_due.astimezone(settings.timezone).date()
        today = now.astimezone(settings.timezone).date()
        day_word = "today" if due_day == today else "tomorrow"  # never later: hours open daily
        when = f"{day_word} at {clock_label(added.first_text_due, settings.timezone)}"
    return (
        f"<h1>Thank you!</h1><p>{business}'s automated assistant will text you at "
        f"{html.escape(phone_shown)} {when} to set up your inspection. "
        "Reply STOP at any time to stop the texts.</p>"
    )
