# lead-bot/src/lead_bot/main.py
"""Entry point: builds the FastAPI app that `fastapi dev` / `fastapi run` load.

Run locally with:
    uv run fastapi dev src/lead_bot/main.py

The server takes the form and records leads. Sending is the worker's job, in its own process:
    uv run python -m lead_bot.worker

Settings come from the environment rather than arguments, because fastapi's CLI imports this
module directly and has no way to pass anything in.
"""

from __future__ import annotations

from dotenv import load_dotenv

from .server import create_app
from .settings import BusinessSettings, database_path_from_environment
from .store import Store
from .twilio_sms import TwilioSettings

load_dotenv()

_settings = BusinessSettings.from_environment()  # stops here, naming what's missing
_store = Store(database_path_from_environment())

# Module-level `app`: the name fastapi's CLI looks for by default. Twilio settings are optional
# until the texting webhooks are used (create_app answers them with 503 without).
app = create_app(_store, _settings, twilio=TwilioSettings.from_environment())
