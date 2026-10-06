# lead-bot

Answers a roofing company's web leads by text message: an AI assistant texts the homeowner,
asks four questions, and books a free inspection on the company's Google Calendar. A practice
build of the "one-industry inbound response and follow-up" plan in the AI Lead Response
research paper.

The voice-call version (phases 1-3) is kept at the git tag `voice-final`.

## Status

| Phase | What | Status |
| --- | --- | --- |
| 1 | Texting without Twilio: conversation, reminders, opt-out, simulator, ~50 scenarios | done (dry run) |
| 2 | Twilio: incoming-text webhook, sending, delivery status, Virtual Phone test | built; Virtual Phone test next |
| 3 | Real phones, after toll-free verification is approved | |

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
#   GOOGLE_CALENDAR_ID=primary               # optional, this is the default
#   TWILIO_ACCOUNT_SID=<starts with AC>      # all three Twilio values, or none (dry run)
#   TWILIO_AUTH_TOKEN=<Console, Account info>
#   TWILIO_PHONE_NUMBER=<the texting number>
#   PUBLIC_BASE_URL=https://<machine>.<tailnet>.ts.net   # the address Twilio calls
#   OWNER_ALERT_EMAIL=<where owner alerts go>            # all three alert values, or none
#   SMTP_USERNAME=<the Gmail address that sends them>
#   SMTP_PASSWORD=<a Gmail app password, not the account password>
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
- The business facts the writer may use (`PLACEHOLDER_BUSINESS_FACTS` in `text_script.py`)
  and the inspection hours in `settings.py` are placeholders. Replace them with the
  roofer's own.
- The bot never discusses insurance coverage: in roofing that can count as unlicensed public
  adjusting.
