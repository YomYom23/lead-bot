# lead-bot

Answers a roofing company's web leads with an AI-voice phone call. A practice build of the
"one-industry inbound response and follow-up" plan in the AI Lead Response research paper.

## Status

Phases 1 and 2 of 4 are done. There are still no real phone calls: the dialer is a dry run,
and the call itself runs in a terminal phone simulator.

| Phase | What | Status |
| --- | --- | --- |
| 1 | Form, consent evidence, calling hours, dialer | done (dry run) |
| 2 | The call itself: opening, opt-out, questions, inspection booking | done (simulator) |
| 3 | Writer + guard for off-script questions, ~50 scripted test calls, report | next |
| 4 | Real calls through Twilio (paid account needed) | |

## Run it

```bash
uv sync
# .env (git-ignored):
#   BUSINESS_NAME=<the business's REGISTERED name: it's said first on every call>
#   BUSINESS_CALLBACK_PHONE=<a number people can call back; not the one calls come from>
#   TYPESAFE_API_KEY=<Jev>                   # needed for calls, not for --offline
#   BUSINESS_TIMEZONE=America/Phoenix        # optional, this is the default
#   LEAD_BOT_DB_PATH=lead_bot.db             # optional, this is the default
#   GOOGLE_CALENDAR_ID=primary               # optional, this is the default
uv run fastapi dev src/lead_bot/main.py      # the form at http://127.0.0.1:8000/
uv run python -m lead_bot.dialer             # second terminal; dry run
```

## Talk to the bot

```bash
# Keyword stand-ins for Jev and an in-memory calendar: no keys, nothing real touched.
uv run python -m lead_bot.simulate --new-lead --offline

# Real Jev and your real Google Calendar (books a real event):
uv run python -m lead_bot.google_login       # once; Testing-mode sign-ins last 7 days
uv run python -m lead_bot.simulate --new-lead
```

Type what the homeowner says. A line that's a single digit is a key press: `9` opts out.
Calls only go out 8 AM to 8:30 PM in the business's timezone. Outside those hours add
`--pretend-now 2026-10-05T10:00-07:00`, which changes only the dialer's hours check.

## Checks

```bash
uv run pytest -q && uv run mypy src tests && uv run ruff check src tests
```

## Before a real client

Have a telemarketing lawyer review the consent wording (`src/lead_bot/consent.py`) and the
call opening (`src/lead_bot/call_script.py`). The comments in `consent.py`, `call_script.py`
and `timing.py` cite the rules they implement (47 CFR 64.1200). The bot never discusses
insurance coverage: in roofing that can count as unlicensed public adjusting.
