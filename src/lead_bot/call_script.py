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


def spoken_digits(e164_phone: str) -> str:
    """'+14805550199' -> '4 8 0, 5 5 5, 0 1 9 9'.

    Spaced digits make a text-to-speech voice read them one at a time. '480-555-0199' can come
    out as "four hundred eighty".
    """
    digits = e164_phone.removeprefix("+1")
    return f"{' '.join(digits[:3])}, {' '.join(digits[3:6])}, {' '.join(digits[6:])}"


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
