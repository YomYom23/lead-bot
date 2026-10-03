# lead-bot

Answers a roofing company's web leads with an AI-voice phone call. A practice build of the
"one-industry inbound response and follow-up" plan in the AI Lead Response research paper.

## Status

Phase 1 of 4 is done: the form, consent records, calling hours and the dialer. The dialer is a
**dry run**: it logs the call it would make and places nothing.

| Phase | What | Status |
| --- | --- | --- |
| 1 | Form, consent evidence, calling hours, dialer | done (dry run) |
| 2 | The call itself, played in a terminal phone simulator | next |
| 3 | Guard, ~50 scripted test calls, weekly report | |
| 4 | Real calls through Twilio (paid account needed) | |

## Run it

```bash
uv sync
# .env (git-ignored):
#   BUSINESS_NAME=<the business's REGISTERED name: it's said first on every call>
#   BUSINESS_TIMEZONE=America/Phoenix        # optional, this is the default
#   LEAD_BOT_DB_PATH=lead_bot.db             # optional, this is the default
uv run fastapi dev src/lead_bot/main.py      # the form at http://127.0.0.1:8000/
uv run python -m lead_bot.dialer             # second terminal; dry run
```

## Checks

```bash
uv run pytest -q && uv run mypy src tests && uv run ruff check src tests
```

## Before a real client

Have a telemarketing lawyer review the consent wording (`src/lead_bot/consent.py`) and the
call opening (phase 2). The comments in `consent.py` and `timing.py` cite the rules they
implement (47 CFR 64.1200).
