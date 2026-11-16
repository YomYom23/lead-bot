# lead-bot

Answers a roofing company's web leads by text message: an AI assistant texts the homeowner,
asks four questions, and books a free inspection on the company's Google Calendar. It also
answers the calls the office missed, texts a client's old leads in reactivation campaigns,
alerts the owner by email and text, and emails a monthly results report.

The first, call-only version is kept at the git tag `voice-final`; missed-call voice was
rebuilt on top of the texting bot.

## Status

| Part | What | Status |
| --- | --- | --- |
| Texting | conversation, reminders, opt-out, booking, simulator, 64 scenarios | done |
| Twilio texts | incoming-text webhook, sending, delivery status | built; Virtual Phone test next |
| Owner alerts | email, plus text for urgent alerts and new bookings | done |
| Reactivation | old-lead campaigns with per-contact consent proof | done |
| Missed-call voice | ConversationRelay, booking or a message, 22 call scenarios | done (simulator) |
| Monthly report | last month's results emailed on the 1st | done |
| Deployment | Docker image, per-client compose and Caddy, nightly encrypted backups, update with rollback | built and checked locally; not live yet |
| Real phones | after toll-free verification is approved | waiting |

The worker prints each text unless it's started with `--send`.

## How it works

Two processes share one SQLite file:

- **The server** (`main.py`, `server.py`) shows the form, records the lead and the exact
  consent wording the person saw, and schedules the first text. It also takes Twilio's two
  webhooks: incoming texts (`/twilio/incoming`) and delivery reports (`/twilio/status`). Each is
  checked against Twilio's signature, saved, and answered at once. It never sends.
- **The worker** (`worker.py`) answers incoming texts, queues the first text and the
  reminders (+2 hours, +1 day, +3 days), hands quiet conversations to the owner, and sends
  what's queued (`twilio_sms.py`). A text Twilio reports as undelivered alerts the owner with
  Twilio's error code.

Whenever a person has to act (an active leak, a request for a person, a question the bot
couldn't answer, a text that wasn't delivered), the bot raises an owner alert, and the worker
emails it to the owner (`owner_alerts.py`): one email per alert, with the lead's details, their
answers and the whole conversation. Urgent ones say URGENT in the subject. A failed email is
retried, never dropped. `worker --send` won't start without alert email, because the bot tells
homeowners things like "I'll flag this as urgent for our team".

With `OWNER_ALERT_PHONE` set, the alerts that need action today are also texted to the owner:
water coming in, a homeowner asking for a person or to move their booking, every new booking,
and the calendar failing. Only alerts under two hours old are texted, and a text that was
refused or may have gone is never resent; the email carries every alert either way. The owner
texting back ("ok, calling her") is ignored, not turned into a new alert.

Reactivation (`reactivation.py`) texts a client's old leads, asking whether they still want a
free roof inspection. The client's list is a CSV with `full_name, phone, address, consent_date,
consent_source, consent_wording`; a row missing any consent value is refused, because a text the
business can't show consent for is the costly kind. Each contact becomes an ordinary lead, so the
conversation, booking, opt-out and alerts are the same as for a form lead. The worker starts up
to the daily limit (50 by default) spread across the texting window, re-checking the do-not-call
list at each contact's turn. An old lead gets one reminder, two days later. One who never replies
raises no owner alert, and "no, already fixed" ends with one polite text and the do-not-call list.

```bash
uv run python -m lead_bot.reactivation import old-leads.csv --name spring-2026 --dry-run  # check, save nothing
uv run python -m lead_bot.reactivation import old-leads.csv --name spring-2026 --daily-limit 50
uv run python -m lead_bot.reactivation status
```

Read the consent wordings the dry run lists before importing for real: whether a wording covers
automated texts from this business is for a person (ideally the client's lawyer) to judge.

Missed calls (`call_session.py`, `relay.py`): the business's line forwards calls nobody picked up
to the Twilio number, whose "A call comes in" webhook is `<PUBLIC_BASE_URL>/twilio/voice/incoming`
(POST). The server records the call with a one-time token and connects it to the live WebSocket
at `/calls/relay` through Twilio ConversationRelay, which does the speech in both directions. The
bot says it's an automated assistant and that the call is transcribed, then either books an
inspection (name, address read back, the usual questions, a time) or takes a message for a call
back. A caller already on file hears their address only after their name matched. After booking,
a new caller is asked whether they may be texted the details; a yes is stored as consent with the
exact question. Every call ends with an owner alert and the transcript; a message or a booking is
also texted to the owner with `OWNER_ALERT_PHONE`. Calls need the model keys in the server's
`.env` too, and a Twilio account that isn't a trial (trials strip ConversationRelay).

```bash
uv run python -m lead_bot.simulate --call --offline           # you are the caller, in the terminal
uv run python -m lead_bot.simulate --call --known --offline   # ... calling from a number on file
uv run python -m lead_bot.call_scenarios --offline            # ~20 scripted calls; scenarios/call-report.md
```

Monthly report (`report.py`): on or after 8 AM on the 1st, the worker emails the owner last
month's results: inspections booked and where they came from (web form, phone calls, old-lead
campaigns), form leads texted, replied and booked, missed calls answered, campaign replies,
urgent leaks, hand-offs, opt-outs, and the three most recent bookings. Months follow the
business's timezone. Each month goes once (`monthly_reports`), and a new install sends nothing
for the month before its first lead or call.

```bash
uv run python -m lead_bot.report                          # preview last month's report
uv run python -m lead_bot.report --month 2026-10 --send   # email a month now
```

Every bot text is fixed wording from `text_script.py`. Jev only classifies what the homeowner
wrote. The writer (OpenAI) drafts an answer only to an off-script question, and the guard
(Jev checks plus regex backstops) must pass the draft before it is sent; otherwise the
homeowner gets the fixed "I'll pass that question to our team" text and the owner is alerted.

Opt-out: a bare STOP is left to Twilio, which blocks the number and confirms. "Please stop
texting me" in any words is caught before Jev and gets exactly one confirmation. Either way
the number goes on the bot's do-not-call list, and nothing is ever sent to it again.

## Run it

```bash
uv sync
# .env (git-ignored):
#   BUSINESS_NAME=<the business's REGISTERED name: it starts the first text to every lead>
#   BUSINESS_CALLBACK_PHONE=<a number people can call; the last reminder and HELP give it>
#   TYPESAFE_API_KEY=<Jev>                   # not needed with --offline
#   OPENAI_API_KEY=<the writer>              # not needed with --offline
#   WRITER_MODEL=gpt-6-luna                  # optional, this is the default
#   BUSINESS_TIMEZONE=America/Phoenix        # optional, this is the default
#   LEAD_BOT_DB_PATH=lead_bot.db             # optional, this is the default
#   CLIENT_CONFIG_PATH=client.toml           # optional, this is the default
#   GOOGLE_CALENDAR_ID=primary               # optional, this is the default
#   TWILIO_ACCOUNT_SID=<starts with AC>      # all three Twilio values, or none (dry run)
#   TWILIO_AUTH_TOKEN=<Console, Account info>
#   TWILIO_PHONE_NUMBER=<the texting number>
#   PUBLIC_BASE_URL=https://<machine>.<tailnet>.ts.net   # the address Twilio calls
#   OWNER_ALERT_EMAIL=<where owner alerts go>            # all three alert values, or none
#   SMTP_USERNAME=<the Gmail address that sends them>
#   SMTP_PASSWORD=<a Gmail app password, not the account password>
#   OWNER_ALERT_PHONE=<the owner's mobile>   # optional: urgent alerts also by text
uv run fastapi dev src/lead_bot/main.py      # the form at http://127.0.0.1:8000/
uv run python -m lead_bot.worker             # second terminal; dry run, texts are printed
uv run python -m lead_bot.worker --send      # or: real texts through Twilio
uv run python -m lead_bot.owner_alerts --test  # one test email, to check the alert settings
uv run python -m lead_bot.google_login       # once, before the worker books real events
```

### Setting up owner alert email (Gmail)

1. Turn on 2-Step Verification for the sending Google account.
2. Create an app password at myaccount.google.com/apppasswords and put it in `SMTP_PASSWORD`
   (the spaces Google shows don't matter).
3. Run `uv run python -m lead_bot.owner_alerts --test` and check the inbox.

Without these, the dry-run worker prints each alert email instead, and none is marked as sent,
so they all go out once email is set up.

### Connecting Twilio

1. `tailscale funnel 8000` makes the server reachable at `https://<machine>.<tailnet>.ts.net`.
   Put exactly that in `PUBLIC_BASE_URL`: Twilio signs the URL it calls, and the server checks
   the signature against this address, so a mismatch refuses every text with 403.
2. In the Twilio Console, open the number's messaging settings and set "A message comes in" to
   `<PUBLIC_BASE_URL>/twilio/incoming`, HTTP POST. Delivery reports need no setting: each send
   asks for them at `/twilio/status`.
3. Test with the Console's Virtual Phone (Messaging > Virtual Phone) until toll-free
   verification is approved; real US numbers get error 30032 before then.

If Twilio rejects the account SID or auth token, the worker stops with exit code 78 and puts the
text it was sending back in the queue.

## Text the bot

```bash
# Keyword stand-ins for Jev and an in-memory calendar: no keys, nothing real touched.
uv run python -m lead_bot.simulate --offline

# Real Jev and the real writer (billed), still an in-memory calendar:
uv run python -m lead_bot.simulate
```

Type what the homeowner texts. `/wait 2h` (or `30m`, `1d`) lets time pass so reminders and
hand-offs happen, `/state` shows the saved answers and owner alerts, and `/quit` stops.
The simulator starts its clock inside texting hours (8 AM to 8:30 PM in the business's
timezone), so it works at any time of day.

## Checks

```bash
uv run pytest -q && uv run mypy src tests && uv run ruff check src tests

# ~50 scripted conversations through the real worker; report in scenarios/report.md.
uv run python -m lead_bot.scenarios --offline   # keyword stand-ins, free
uv run python -m lead_bot.scenarios             # real Jev and writer (billed)
```

`--offline` skips the scenarios marked `needs_real_jev`, whose wording only a real model can
follow. `--only <category or name>` runs a subset.

## Before a real client

- Have a telemarketing lawyer review the consent wording (`src/lead_bot/consent.py`), the
  first text and reminders (`src/lead_bot/text_script.py`), and the texting window
  (`src/lead_bot/timing.py`). Their comments cite the rules they implement (47 CFR 64.1200).
- Toll-free verification needs a real business (an EIN, or a sole proprietor), this opt-in
  page on a site it owns, and sample messages. Until it's approved, texts to US and Canadian
  numbers are blocked (Twilio error 30032).
- The business facts the writer may use and the inspection hours come from `client.toml`
  (git-ignored; copy `client.example.toml`, whose values are placeholders). Fill it in with the
  roofer's own. `CLIENT_CONFIG_PATH` points elsewhere if needed. Tests, scenarios and the
  simulator always use `client.example.toml`.
- The bot never discusses insurance coverage: in roofing that can count as unlicensed public
  adjusting.
