# lead-bot/src/lead_bot/call_script.py
"""Everything the bot says that is fixed in advance, and the instructions Twilio runs for a call.

The opening is the legally required part of every call, so it is never generated. Twilio speaks
it itself (the welcomeGreeting in the TwiML below) before our server hears a word.

What 47 CFR 64.1200(b) requires at the start of an artificial-voice call, and where it is:
  (b)(1) the registered business name, first     -> the first sentence of opening_greeting
  (b)(3) how to opt out, within 2 seconds of it  -> the second sentence ("press 9 or say stop")
  (b)(2) a callback number (not the dialing one) -> the fourth sentence
The AI disclosure and "this call is transcribed" aren't federal requirements yet; they're in
because the research paper treats them as the safe default.
"""

from __future__ import annotations

import html
import re
from datetime import time

from .settings import BusinessSettings

# The key that opts out. Said in the opening, and handled the moment it's pressed (call_session.py).
OPT_OUT_DIGIT = "9"

PERMISSION_QUESTION = "Do you have a couple of minutes to set up the inspection?"

# Fixed answers to the questions the bot must not answer itself.
#   Insurance: in roofing, talking coverage, deductibles or "we'll handle your claim" counts as
#   acting as an unlicensed public adjuster in several states. The bot only points to the insurer.
#   Price: no price list exists to quote from, and a made-up number is the worst thing it could say.
INSURANCE_LINE = (
    "I can't help with insurance questions. Your insurance company is the right one to ask, "
    "and our inspector will document what they find."
)
PRICE_LINE = (
    "I can't give prices over the phone. The inspector will give you a written estimate after the "
    "inspection."
)
QUESTION_FOR_TEAM_LINE = "I'll pass that question to our team so the inspector can answer it."

# Said the moment an off-script question is recognised, before the writer drafts an answer. The
# writer and the guard take a second or two, and without this the homeowner hears dead air.
CHECKING_LINE = "Let me check."

# What the writer may say about the business beyond what call_facts() works out from settings.
# PLACEHOLDER, like the inspection hours in settings.py, until a real roofer gives theirs. Every
# sentence here can be spoken to a homeowner, so nothing goes in that the business hasn't approved.
PLACEHOLDER_BUSINESS_FACTS = (
    "The inspection is free, and agreeing to it doesn't commit the homeowner to buying anything.",
    "The inspector documents what they find on the roof.",
    "After the inspection the homeowner gets a written estimate for any work that's needed.",
    "The company does roof repairs and roof replacements for homes.",
)


def _spoken_clock_time(clock_time: time) -> str:
    """time(8, 0) -> '8 AM', time(17, 30) -> '5:30 PM'. Same style as the slot labels."""
    hour_on_12_hour_clock = clock_time.hour % 12 or 12
    minutes = f":{clock_time.minute:02d}" if clock_time.minute else ""
    return f"{hour_on_12_hour_clock}{minutes} {'AM' if clock_time.hour < 12 else 'PM'}"


def call_facts(settings: BusinessSettings, address: str, booked_label: str | None) -> str:
    """Everything the writer is allowed to state on this call, one fact per line.

    The guard's unsupported_fact check compares each draft against exactly this text, so a
    fact missing here gets the draft blocked rather than invented.
    """
    rules = settings.inspection_rules
    weekday_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    open_days = [weekday_names[weekday] for weekday in sorted(rules.open_weekdays)]
    inspection_minutes = int(rules.appointment_length.total_seconds() // 60)
    booking = (
        f"An inspection is booked on this call for {booked_label}."
        if booked_label is not None
        else "No inspection has been booked yet on this call."
    )
    return "\n".join(
        [
            f"Business name: {settings.business_name}.",
            "This assistant is an artificial intelligence, and the call is transcribed.",
            f"Callback number: {spoken_digits(settings.callback_phone)}.",
            f"Inspections take about {inspection_minutes} minutes.",
            f"Inspections can be booked {open_days[0]} to {open_days[-1]}, between "
            f"{_spoken_clock_time(rules.opening_time)} and {_spoken_clock_time(rules.closing_time)}, "
            f"up to {rules.days_ahead} days ahead.",
            f"The inspection address on file: {address}.",
            booking,
            *PLACEHOLDER_BUSINESS_FACTS,
        ]
    )


def spoken_digits(e164_phone: str) -> str:
    """'+14805550199' -> '4 8 0, 5 5 5, 0 1 9 9'.

    Spaced digits make a text-to-speech voice read them one at a time. '480-555-0199' can come
    out as "four hundred eighty".
    """
    digits = e164_phone.removeprefix("+1")
    return f"{' '.join(digits[:3])}, {' '.join(digits[3:6])}, {' '.join(digits[6:])}"


# A US phone number written the usual ways: 480-555-0199, (480) 555-0199, 480.555.0199, and with a
# leading +1 or 1. The writer is given the number already spaced out, but rewrote it as
# 480-555-0199 in the phase 3 scenario run.
WRITTEN_PHONE_NUMBER = re.compile(r"(?:\+?1[-. ]?)?\(?\b(\d{3})\)?[-. ]?(\d{3})[-. ](\d{4})\b")


def speakable_phone_numbers(text: str) -> str:
    """Every written phone number in `text`, rewritten digit by digit as spoken_digits does."""
    return WRITTEN_PHONE_NUMBER.sub(lambda match: spoken_digits("+1" + "".join(match.groups())), text)


def opening_greeting(settings: BusinessSettings) -> str:
    return (
        f"Hello, this call is from {settings.business_name}. "
        f"To stop these calls, press {OPT_OUT_DIGIT} or say stop at any time. "
        "I'm an artificial intelligence assistant, and this call is transcribed. "
        f"You can reach us at {spoken_digits(settings.callback_phone)}. "
        "I'm calling about the roof inspection you asked for. "
        f"{PERMISSION_QUESTION}"
    )


# Twilio's limit for TwiML passed directly when creating a call (its Call resource docs).
MAX_TWIML_LENGTH = 4000


def call_twiml(settings: BusinessSettings, call_id: int, relay_token: str) -> str:
    """The TwiML for one call: speak the opening, then connect the live conversation to us.

    Attribute choices, from Twilio's ConversationRelay docs:
      welcomeGreetingInterruptible="dtmf"  talking over the opening doesn't cut it short, so
                                           the required disclosures are heard in full; a
                                           keypress still does, so 9 works at once
      dtmfDetection="true"                 keypresses are sent to us at all
      reportInputDuringAgentSpeech="any"   speech and keys reach us even while the bot is
                                           talking. The default is "none" (since May 2025),
                                           which would swallow a 9 pressed mid-sentence.
    The call id and its one-time token travel as <Parameter>s and come back in the setup
    message. The lead's own details never go into the TwiML.
    """
    relay_attributes = {
        "url": settings.relay_url,
        "welcomeGreeting": opening_greeting(settings),
        "welcomeGreetingInterruptible": "dtmf",
        "dtmfDetection": "true",
        "interruptible": "any",
        "reportInputDuringAgentSpeech": "any",
        "language": "en-US",
    }
    parameters = {"call_id": str(call_id), "relay_token": relay_token}
    # html.escape with quote=True escapes & < > " and ', which covers XML attribute values. A
    # business name like "Smith & Sons" would otherwise break the document.
    attribute_text = " ".join(
        f'{name}="{html.escape(value, quote=True)}"' for name, value in relay_attributes.items()
    )
    parameter_text = "".join(
        f'<Parameter name="{html.escape(name, quote=True)}" value="{html.escape(value, quote=True)}"/>'
        for name, value in parameters.items()
    )
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Connect><ConversationRelay {attribute_text}>{parameter_text}</ConversationRelay>"
        "</Connect><Hangup/></Response>"
    )
    if len(twiml) > MAX_TWIML_LENGTH:
        raise ValueError(f"call TwiML is {len(twiml)} characters; Twilio accepts at most {MAX_TWIML_LENGTH}")
    return twiml
