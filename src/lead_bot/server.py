# lead-bot/src/lead_bot/server.py
"""The web server: the roofing company's inspection-request form, and a health check.

  GET  /        -> the form
  POST /leads   -> validate, record the lead and its consent evidence, schedule the first text
  GET  /health  -> counts of leads and texts by status

POST /leads only records and schedules; it never sends. Sending happens in the worker process
(worker.py), which re-checks consent, the do-not-call list and texting hours right before it
queues each text the bot starts.

The texting phase adds the endpoint Twilio delivers incoming texts to.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable
from datetime import datetime
from string import Template
from typing import Annotated

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse

from .consent import CONSENT_VERSION, ConsentEvidence, consent_text
from .leads import PROBLEM_LABELS, InvalidLeadForm, parse_lead_form, pretty_phone
from .settings import BusinessSettings
from .store import AddedLead, Store
from .timing import clock_label, utc_now

logger = logging.getLogger(__name__)

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
) -> FastAPI:
    """Build the app. Store, settings and clock are passed in so tests can control them."""
    app = FastAPI(title="lead-bot")

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
            source=str(request.url),
        )
        submitted_at = clock()
        added = store.add_lead(new_lead, evidence, submitted_at, settings.texting_hours)
        logger.info("lead %d added: %s", added.lead_id, added.decision)
        return page(
            "Thank you",
            _thanks_body(added, settings, phone_shown=pretty_phone(new_lead.phone), now=submitted_at),
        )

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"ok": True, **store.counts()}

    @app.get("/favicon.ico")
    def no_favicon() -> Response:
        # Browsers ask for this on every page. A plain 204 keeps it out of the error log.
        return Response(status_code=204)

    return app


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
